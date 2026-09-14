#!/usr/bin/env python3
"""CodexBar agent state aggregator.

Polls running Claude / Codex / OpenCode / pi / omp processes and writes the
aggregate state to ~/.codexbar/agents.json. The plasmoid widget reads that
file on its own poll cycle (XHR).

Designed to run continuously as a systemd `--user` service. Hooks are no
longer required — all state is derived from on-disk session files each
process keeps open:
  * claude  : ~/.claude/sessions/<pid>.json + ~/.claude/projects/<slug>/<sid>.jsonl
  * codex   : the rollout JSONL the codex pid keeps open via /proc/<pid>/fd
  * opencode: SQLite at ~/.local/share/opencode/opencode.db (session.title)
  * pi/omp  : the JSONL the pid keeps open via /proc/<pid>/fd

Usage:
  codexbar_agents.py                  one-shot, prints aggregate to stdout
  codexbar_agents.py --once           one-shot, writes ~/.codexbar/agents.json
  codexbar_agents.py --watch [-i N]   daemon; sweep + write every N seconds
"""
from __future__ import annotations

import argparse
import hashlib
import math
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

AGGREGATE_PATH = Path.home() / ".codexbar" / "agents.json"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
LOCK_PATH = AGGREGATE_PATH.with_suffix(".lock")

# Hosts we treat as terminal emulators when walking the proc tree.
KNOWN_HOSTS = {
    "kitty", "konsole", "code", "code-insiders", "code-flatpak",
    "tmux", "tmux: server", "wezterm", "alacritty", "ghostty",
    "gnome-terminal", "gnome-terminal-", "xterm", "foot",
    "yakuake", "tilix", "ptyhost",
}

# argv[1] verbs that mean a background service rather than an interactive
# session: `claude daemon run`, `omp browser-relay`, `codex app-server`, mcp
# servers, and friends. Matched on the first real argument, not as a substring,
# so a prompt that happens to mention "daemon" doesn't hide a real session.
_SERVICE_VERBS = {
    "daemon", "browser-relay", "remote-control", "mcp", "mcp-server",
    "app-server", "app-server-protocol", "serve", "doctor", "agents", "lsp",
}

# Headless/piped invocations (SDK calls from claudecodeui, `-p` one-shots).
# Real processes, but no terminal session behind them.
_HEADLESS_FLAGS = {"--output-format", "--input-format", "--print", "-p"}

# Fork helpers (embeddings, js eval, lsp mux, ...) keep comm="omp"/"pi" and so
# show up in `pgrep -x`; their cmdline carries a `__*_worker_` verb.
_WORKER_PREFIXES = ("__omp_worker", "__pi_worker")

# Tags Claude/Codex/etc inject into the user-message stream that aren't
# actual user prompts. Used to filter `lastPrompt`.
_PROMPT_SKIP_PREFIXES = (
    "<command-name>", "<command-message>", "<command-stdout>",
    "<command-stderr>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<task-notification>", "<system-reminder>", "<user-prompt-submit-hook>",
    "<file-system-error>", "<tool_use_error>", "<request_interrupted>",
    "<environment_context>", "<user_instructions>",
    "[request interrupted", "caveat:",
)


# Peek preview: how many recent messages to keep per session and the cap on
# each message's text. Both bound the size of ~/.codexbar/agents.json, which
# the plasmoid re-reads every poll tick.
_PEEK_MSGS = 8
_PEEK_CHARS = 320
_MODEL_MAX_CHARS = 160

# Loaded and saved only while the aggregate writer owns its flock.
_TRANSCRIPT_CACHE: dict = {}
_CACHE_ACTIVE: set[str] | None = None
_CACHE_VERSION = 1


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _ppid_of(pid: int) -> int:
    for ln in _read_text(f"/proc/{pid}/status").splitlines():
        if ln.startswith("PPid:"):
            try:
                return int(ln.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _cwd_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd") or ""
    except OSError:
        return ""


def _comm_of(pid: int) -> str:
    return _read_text(f"/proc/{pid}/comm").strip()


def _cmdline_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return ""


def _argv_of(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _parent_walk_for_host(start_pid: int) -> tuple[str, int, list[int]]:
    """Walk up the proc tree from start_pid; return (host_name, host_pid,
    ancestor_chain) where the chain runs [start_pid, ..., host_pid]. The
    chain mirrors codexbar_focus._ancestor_pids and lets the popup match the
    session to its KWin window by any pid in the family. Returns ("", 0, [])
    if no known terminal emulator found."""
    cur = start_pid
    chain: list[int] = []
    seen: set[int] = set()
    depth = 12
    host, host_pid = "", 0
    while cur > 1 and cur not in seen and depth > 0:
        seen.add(cur)
        chain.append(cur)
        depth -= 1
        comm = _comm_of(cur)
        if comm in KNOWN_HOSTS:
            host, host_pid = comm, cur
            break
        cmdline = _cmdline_of(cur)
        if "vscode-server" in cmdline or "/code/" in cmdline or "code-insiders" in cmdline:
            host, host_pid = "code", cur
            break
        nxt = _ppid_of(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    if not host:
        return "", 0, []

    # Electron hosts run several same-named helper processes (pty host,
    # extension host); the window-owning main process may sit further up.
    # Extend the chain through the contiguous run of same-comm hosts so the
    # window pid is part of the family. Plain terminals stop at systemd or
    # the shell, so this only ever adds pids for multi-process hosts.
    while depth > 0:
        nxt = _ppid_of(host_pid)
        if nxt <= 1 or nxt in seen or _comm_of(nxt) != host:
            break
        seen.add(nxt)
        chain.append(nxt)
        host_pid = nxt
        depth -= 1
    return host, host_pid, chain


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def _is_service_argv(argv: list[str]) -> bool:
    """True when the argv belongs to a daemon, a fork helper or a piped
    one-shot — anything that isn't a session sitting in a terminal."""
    if not argv:
        return True
    if "claude-desktop" in argv[0]:
        return True
    rest = argv[1:]
    for a in rest:
        if a.startswith(_WORKER_PREFIXES) or a.startswith("--type="):
            return True
    if rest and rest[0] in _SERVICE_VERBS:
        return True
    return any(a in _HEADLESS_FLAGS for a in rest)


def _pgrep(name: str) -> list[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out: list[int] = []
    for ln in (proc.stdout or "").splitlines():
        pid_s = ln.strip()
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if _is_service_argv(_argv_of(pid)):
            continue
        out.append(pid)
    return out


# ---------------------------------------------------------------------------
# Transcript parsing (Claude + pi/omp use JSONL)
# ---------------------------------------------------------------------------

def _ts_ms(ts) -> int:
    """Best-effort epoch-ms from whatever timestamp shape a transcript uses
    (ISO string, ms int, or seconds float). 0 when unusable."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 < ts < 1e18 and math.isfinite(ts):
        return int(ts if ts > 1e12 else ts * 1000)
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            pass
    return 0


def _model_text(value) -> str:
    """Return a bounded model identifier from an untrusted transcript value."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MODEL_MAX_CHARS]


def _record_model(record: dict, payload: dict | None = None) -> str:
    """Extract a model identifier from the provider record shapes we read."""
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    for value in (
        message.get("model"),
        message.get("modelId"),
        message.get("modelID"),
        record.get("model"),
        record.get("modelId"),
        record.get("modelID"),
        payload.get("model") if isinstance(payload, dict) else None,
        payload.get("modelId") if isinstance(payload, dict) else None,
        payload.get("modelID") if isinstance(payload, dict) else None,
    ):
        model = _model_text(value)
        if model:
            return model
    return ""


def _opencode_model(message: dict) -> str:
    """Extract OpenCode's model ID from an assistant message payload."""
    for field in ("model_id", "modelID", "modelId", "model"):
        model = _model_text(message.get(field))
        if model:
            return model
    return ""


def _peek_squash(text: str) -> str:
    return " ".join(text.split())[:_PEEK_CHARS]


def _content_preview(content) -> str:
    """Readable text of a message content field: either a plain string or a
    list of parts. Text parts are joined; a message that only ran a tool
    collapses to "→ toolname" so tool activity still shows up in the peek."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts, tool = [], ""
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and isinstance(c.get("text"), str):
            texts.append(c["text"])
        elif not tool and isinstance(c.get("name"), str):
            tool = c.get("name")
    if texts:
        return " ".join(texts)
    return "→ " + tool if tool else ""


def _peek_add(buf: list, role: str, text: str, ts, kind: str = "text") -> None:
    if role not in ("user", "assistant") or not isinstance(text, str) or not text:
        return
    text = _peek_squash(text)
    if not text or (role == "user" and not _is_real_user_prompt(text)):
        return
    kind = kind if kind == "tools" else "text"
    timestamp = _ts_ms(ts)
    if kind == "tools" and buf and buf[-1]["kind"] == "tools":
        entry = buf[-1]
        entry["count"] += 1
        if len(entry["names"]) < 10:
            entry["names"].append(text)
        entry["ts"] = timestamp
    else:
        buf.append({"role": role, "kind": kind, "text": text, "ts": timestamp,
                    "names": [text] if kind == "tools" else [], "count": 1})
        del buf[:-_PEEK_MSGS]


def _peek_finalize(buf: list) -> list[dict]:
    out = []
    for entry in buf:
        text = entry["text"]
        if entry["kind"] == "tools":
            more = entry["count"] - len(entry["names"])
            text = (", ".join(entry["names"]) + (f" +{more} more" if more else ""))[:_PEEK_CHARS]
        out.append({key: entry[key] for key in ("role", "kind", "ts")} | {"text": text})
    return out


def _is_real_user_prompt(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip().lower()
    return bool(head) and not any(head.startswith(p) for p in _PROMPT_SKIP_PREFIXES)



def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _session_id(value) -> str:
    value = _text(value)
    return value if value and len(value) <= 256 and "\0" not in value else ""


def _parser_state() -> dict:
    return {"sessionId": "", "cwd": "", "windowTitle": "", "model": "",
            "state": "working", "last_real": "", "last_any": "", "peek": []}


def _parse_record(provider: str, state: dict, rec: dict) -> None:
    t = rec.get("type")
    payload = rec.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    inner = rec.get("message")
    inner = inner if isinstance(inner, dict) else rec
    role, text, tool = "", "", ""
    if provider == "claude":
        if t == "ai-title":
            state["windowTitle"] = _text(rec.get("aiTitle")).strip()[:4096] or state["windowTitle"]
            return
        role = t if t in ("user", "assistant") else rec.get("role")
        if role not in ("user", "assistant"):
            return
        content = inner.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text = _text(part.get("text")) or text
                elif part.get("type") == "tool_use":
                    tool = tool or _text(part.get("name"))
        if role == "assistant":
            state["model"] = _record_model(rec) or state["model"]
    elif provider == "codex":
        if t in ("session_meta", "turn_context"):
            state["model"] = _record_model(rec, payload) or state["model"]
        if t == "session_meta":
            state["sessionId"] = _session_id(payload.get("id")) or state["sessionId"]
            state["cwd"] = _text(payload.get("cwd"))[:4096] or state["cwd"]
        elif t == "event_msg":
            event = payload.get("type")
            if event in ("task_started", "user_message", "task_complete"):
                state["state"] = "idle" if event == "task_complete" else "working"
        elif t == "response_item":
            if payload.get("type") == "message":
                role = payload.get("role")
                content = payload.get("content")
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") in ("input_text", "output_text"):
                        text = _text(part.get("text")) or text
            elif payload.get("type") == "function_call":
                role, tool = "assistant", _text(payload.get("name"))
    else:  # pi and omp share a transcript format.
        if t == "model_change":
            state["model"] = _record_model(rec) or state["model"]
            return
        if t in ("title", "title_change", "session", "session-meta", "session-start", "meta"):
            state["windowTitle"] = _text(rec.get("title")).strip()[:4096] or state["windowTitle"]
            if t not in ("title", "title_change"):
                state["sessionId"] = _session_id(rec.get("id")) or state["sessionId"]
                state["cwd"] = _text(rec.get("cwd"))[:4096] or state["cwd"]
            return
        role = inner.get("role") or rec.get("role")
        content = inner.get("content")
        if role == "assistant":
            state["state"] = "working" if inner.get("stopReason") == "toolUse" else "idle"
            state["model"] = _record_model(rec) or state["model"]
            preview = _content_preview(content)
            if preview.startswith("→ "):
                tool = preview[2:]
            else:
                text = preview
        elif role in ("user", "toolResult"):
            state["state"] = "working"
            if role == "user":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text = _text(part.get("text")) or text
    if not (provider == "claude" and rec.get("isSidechain")):
        _peek_add(state["peek"], role, text or tool, rec.get("timestamp"),
                  "text" if text else "tools")
    if role == "user" and (text or tool):
        prompt = text or tool
        state["last_any"] = " ".join(prompt.split())[:200]
        if _is_real_user_prompt(prompt):
            state["last_real"] = state["last_any"]


def _valid_parser_state(state) -> bool:
    if not isinstance(state, dict):
        return False
    if any(not isinstance(state.get(key), str) or len(state[key]) > 4096
           for key in _parser_state() if key != "peek"):
        return False
    peek = state.get("peek")
    if not isinstance(peek, list) or len(peek) > _PEEK_MSGS:
        return False
    for entry in peek:
        if not isinstance(entry, dict):
            return False
        names = entry.get("names")
        if (entry.get("role") not in ("user", "assistant")
                or entry.get("kind") not in ("text", "tools")
                or not isinstance(entry.get("text"), str)
                or len(entry["text"]) > _PEEK_CHARS
                or type(entry.get("ts")) is not int
                or type(entry.get("count")) is not int or entry["count"] < 1
                or not isinstance(names, list) or len(names) > 10
                or any(not isinstance(name, str) or len(name) > _PEEK_CHARS for name in names)
                or entry["count"] < len(names)):
            return False
    return True


def _read_transcript(path: str, provider: str) -> dict:
    """Resume at the last complete line; retain normalized state, never rows.

    A prefix digest detects even an in-place rewrite in the middle of a file.
    Prefix verification streams bytes without decoding or parsing old JSON.
    This deliberately favors rewrite correctness over sampled fingerprints.
    """
    key = str(Path(path).absolute())
    if _CACHE_ACTIVE is not None:
        _CACHE_ACTIVE.add(key)
    cached = _TRANSCRIPT_CACHE.get(key, {})
    cached = cached if isinstance(cached, dict) else {}
    state = _parser_state()
    try:
        with open(path, "rb") as source:
            stat = os.fstat(source.fileno())
            identity = [stat.st_dev, stat.st_ino]
            signature = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            if (cached.get("provider") == provider and cached.get("identity") == identity
                    and cached.get("signature") == signature
                    and _valid_parser_state(cached.get("state"))):
                return cached["state"]
            offset = cached.get("offset", 0)
            digest = hashlib.sha256()
            valid = (cached.get("provider") == provider and cached.get("identity") == identity
                     and type(offset) is int and 0 <= offset <= stat.st_size)
            if valid:
                remaining = offset
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        valid = False
                        break
                    digest.update(chunk)
                    remaining -= len(chunk)
                valid = valid and digest.hexdigest() == cached.get("digest")
            if valid:
                saved_state = cached.get("state")
                valid = _valid_parser_state(saved_state)
            if valid:
                state = json.loads(json.dumps(saved_state))
            else:
                offset = 0
                source.seek(0)
                digest = hashlib.sha256()
            while source.tell() < stat.st_size:
                raw = source.readline(stat.st_size - source.tell())
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                digest.update(raw)
                try:
                    rec = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(rec, dict):
                    _parse_record(provider, state, rec)
            _TRANSCRIPT_CACHE[key] = {
                "provider": provider, "identity": identity, "signature": signature, "offset": offset,
                "digest": digest.hexdigest(), "state": state,
            }
    except (OSError, ValueError, TypeError, KeyError):
        _TRANSCRIPT_CACHE.pop(key, None)
        return _parser_state()
    return state


def _transcript_info(path: str, provider: str) -> dict:
    state = _read_transcript(path, provider)
    return {key: state[key] for key in ("sessionId", "cwd", "windowTitle", "model", "state")} | {
        "lastPrompt": state["last_real"] or state["last_any"],
        "recent": _peek_finalize(state["peek"]), "identityExact": False,
    }


def _tail_claude_transcript(path: str) -> tuple[str, str, list, str]:
    info = _transcript_info(path, "claude")
    return info["windowTitle"], info["lastPrompt"], info["recent"], info["model"]


# ---------------------------------------------------------------------------
# Per-provider info extraction
# ---------------------------------------------------------------------------

def _claude_slug(cwd: str) -> str:
    return "".join(("-" if c in "/_." else c) for c in cwd) if cwd else ""


def _claude_info(pid: int) -> dict:
    info = {
        "sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "",
        "state": "working", "identityExact": False, "model": "",
    }
    state_file = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    if not state_file.is_file():
        return info
    try:
        rec = json.loads(state_file.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return info
    if not isinstance(rec, dict):
        return info
    sid = _session_id(rec.get("sessionId"))
    cwd = _text(rec.get("cwd")) or _cwd_of(pid)
    info["sessionId"] = sid
    info["identityExact"] = isinstance(sid, str) and bool(sid)
    info["cwd"] = cwd

    status = _text(rec.get("status")).lower()
    waiting = _text(rec.get("waitingFor")).strip()
    if status == "waiting" and waiting:
        info["state"] = "blocked"
    elif status == "busy":
        info["state"] = "working"
    elif status in ("idle", "shell"):
        info["state"] = "idle"

    if sid and cwd:
        transcript = Path.home() / ".claude" / "projects" / _claude_slug(cwd) / f"{sid}.jsonl"
        title, prompt, recent, model = _tail_claude_transcript(str(transcript))
        if title:
            info["windowTitle"] = title
        if prompt:
            info["lastPrompt"] = prompt
        if model:
            info["model"] = model
        if recent:
            info["recent"] = recent
    return info


def _codex_info(pid: int) -> dict:
    info = {
        "sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "",
        "state": "working", "identityExact": False, "model": "",
    }
    rollout, direct = _open_jsonl_under(pid, "/.codex/sessions/")
    if not rollout:
        return info
    info = _transcript_info(rollout, "codex")
    info["identityExact"] = direct and bool(info["sessionId"])
    info["cwd"] = info["cwd"] or _cwd_of(pid)
    return info


def _opencode_info(pid: int) -> dict:
    info = {
        "sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "",
        "state": "working", "identityExact": False, "model": "",
    }
    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db.is_file():
        return info
    cwd = _cwd_of(pid)
    peek: list = []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT id, directory, title, time_updated FROM session "
                "WHERE directory = ? "
                "ORDER BY time_updated DESC LIMIT 1",
                (cwd,),
            ).fetchone()
            if not row:
                row = con.execute(
                    "SELECT id, directory, title, time_updated FROM session "
                    "ORDER BY time_updated DESC LIMIT 1"
                ).fetchone()
            if row:
                # Last messages with their parts, oldest first, for the peek
                # panel. Parts arrive one row per part; text parts of one
                # message are joined before entering the peek buffer.
                cur = con.execute(
                    "SELECT m.id, m.data, p.data, m.time_created FROM ("
                    "  SELECT id, data, time_created FROM message"
                    "  WHERE session_id = ?"
                    "  ORDER BY time_created DESC LIMIT 40"
                    ") m LEFT JOIN part p ON p.message_id = m.id"
                    " ORDER BY m.time_created ASC, p.time_created ASC",
                    (row[0],),
                )
                joined: dict[str, list] = {}
                order: list[str] = []
                for mid, mdata, pdata, mtime in cur:
                    message = {}
                    try:
                        message = json.loads(mdata) or {}
                    except (TypeError, json.JSONDecodeError):
                        pass
                    if (
                        isinstance(message, dict)
                        and message.get("role") == "assistant"
                    ):
                        candidate = _opencode_model(message)
                        if candidate:
                            info["model"] = candidate
                    entry = joined.get(mid)
                    if entry is None:
                        role = message.get("role") if isinstance(message, dict) else ""
                        entry = joined[mid] = [role or "", "", mtime]
                        order.append(mid)
                    if not pdata:
                        continue
                    try:
                        pd = json.loads(pdata) or {}
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(pd, dict) and pd.get("type") == "text" and isinstance(pd.get("text"), str):
                        entry[1] = (entry[1] + " " + pd["text"]).strip()
                for mid in order:
                    role, text, mtime = joined[mid]
                    _peek_add(peek, role, text, mtime)
        finally:
            con.close()
    except Exception:
        return info
    if not row:
        return info
    sid, directory, title, time_updated = row
    info["sessionId"] = sid or ""
    info["cwd"] = directory or cwd
    info["windowTitle"] = (title or "").strip()
    info["recent"] = _peek_finalize(peek)
    # Opencode bumps session.time_updated every few seconds while the
    # assistant is streaming tokens. If nothing has touched the row in 30s,
    # the session isn't doing anything — call it idle.
    if isinstance(time_updated, (int, float)) and time_updated > 0:
        age_ms = int(time.time() * 1000) - int(time_updated)
        if age_ms > 30_000:
            info["state"] = "idle"
    return info


def _pi_slug(cwd: str) -> str:
    """pi/omp slug for the session dir: `--` + cwd-without-leading-slash with
    `/` → `-` + `--`. e.g. /home/user/projects/myapp → --home-user-projects-myapp--"""
    if not cwd:
        return ""
    return "--" + cwd.lstrip("/").replace("/", "-") + "--"


def _find_pi_rollout(cwd: str) -> str:
    """Find the latest-modified JSONL under ~/.{pi,omp}/agent/sessions/<slug>/
    matching the agent's cwd. pi doesn't keep the file open as an fd so the
    /proc/<pid>/fd trick we use for codex/omp doesn't apply."""
    slug = _pi_slug(cwd)
    if not slug:
        return ""
    for base in (
        Path.home() / ".pi" / "agent" / "sessions" / slug,
        Path.home() / ".omp" / "agent" / "sessions" / slug,
    ):
        if not base.is_dir():
            continue
        try:
            candidates = sorted(
                base.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        if candidates:
            return str(candidates[0])
    return ""


def _is_sidecar_jsonl(path: str) -> bool:
    """omp writes advisor transcripts as `__advisor.<name>.jsonl` inside a
    `<rollout-stem>/` directory sitting next to the rollout itself. A sidecar
    carries the advisor's own session id and its "### Session update
    **agent**: ..." messages, so reading one as the session rollout labels the
    row with the advisor's chatter and points click-to-focus at a session id
    that no terminal owns."""
    return os.path.basename(path).startswith("__")


def _rollout_for_sidecar(path: str) -> str:
    """`.../<stem>/__advisor.luna.jsonl` -> `.../<stem>.jsonl`, or "" when
    that rollout is gone."""
    main = os.path.dirname(path) + ".jsonl"
    return main if os.path.isfile(main) else ""


def _root_rollout(path: str) -> str:
    """Return the top-level rollout that owns a nested OMP session artifact."""
    root = path
    while True:
        parent = os.path.dirname(root) + ".jsonl"
        if not os.path.isfile(parent):
            return root
        root = parent


def _newest_jsonl(base: Path) -> str:
    """Newest session rollout anywhere under `base`, or "" if there is none.
    Advisor sidecars don't count; nested subagents rank through their root."""
    try:
        roots = {
            Path(_root_rollout(str(p)))
            for p in base.rglob("*.jsonl")
            if not _is_sidecar_jsonl(str(p))
        }
        candidates = sorted(roots, key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    return str(candidates[0]) if candidates else ""


def _session_dir_of(pid: int) -> str:
    argv = _argv_of(pid)
    for i, a in enumerate(argv):
        if a == "--session-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--session-dir="):
            return a.split("=", 1)[1]
    return ""



def _pi_info(pid: int) -> dict:
    """pi and omp share the same JSONL layout. omp keeps the active file open
    on an fd (so we can see it via /proc/<pid>/fd); pi closes it between
    writes, so we fall back to the cwd → slug lookup.

    State follows the last transcript message: a terminal assistant response
    is idle; a user message, tool request or tool result is still working."""
    info = {
        "sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "",
        "state": "working", "identityExact": False, "model": "",
    }
    rollout, direct = _open_jsonl_under(pid, "/.pi/agent/sessions/", "/.omp/agent/sessions/")
    if not rollout:
        # A run started with an explicit --session-dir doesn't live under the
        # cwd slug, and the slug lookup would hand it a *different* session's
        # rollout — duplicating that session's row under a wrong pid.
        sess_dir = _session_dir_of(pid)
        rollout = _newest_jsonl(Path(sess_dir)) if sess_dir else _find_pi_rollout(_cwd_of(pid))
        direct = False
    if not rollout:
        info["cwd"] = _cwd_of(pid)
        return info

    info = _transcript_info(rollout, "pi")
    info["identityExact"] = direct and bool(info["sessionId"])
    info["cwd"] = info["cwd"] or _cwd_of(pid)
    return info


def _open_jsonl_under(pid: int, *needles: str) -> tuple[str, bool]:
    """Return the selected rollout and whether the process holds it directly.

    Returns ("", False) when no rollout can be selected.

    A root rollout the pid holds directly wins over one reached through an
    advisor sidecar: a process can hold sidecar fds for a session another
    process owns, and picking that would attribute a live session to the wrong
    terminal. Sidecars still count as a fallback, because omp keeps them open
    for sessions whose own rollout fd it has already closed. Nested subagents
    normalize to their owning root before each tier is ranked by mtime."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return "", False
    direct: set[str] = set()
    via_sidecar: set[str] = set()
    for entry in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, entry))
        except OSError:
            continue
        if not target.endswith(".jsonl"):
            continue
        if not any(n in target for n in needles):
            continue
        if not _is_sidecar_jsonl(target):
            direct.add(_root_rollout(target))
            continue
        rollout = _rollout_for_sidecar(target)
        if rollout:
            via_sidecar.add(_root_rollout(rollout))
    newest, newest_mtime = "", -1.0
    for path in direct or via_sidecar:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest, bool(newest and direct)


_INFO_FN = {
    "claude": _claude_info,
    "codex": _codex_info,
    "opencode": _opencode_info,
    "pi": _pi_info,
    "omp": _pi_info,
}

_RESUME_PREFIXES = {
    "claude": ("claude", "--resume"),
    "codex": ("codex", "resume"),
    "opencode": ("opencode", "--session"),
    "pi": ("pi", "--session"),
    "omp": ("omp", "--resume"),
}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def _resume_command(
    provider: str, session_id: str, cwd: str, identity_exact: bool
) -> str:
    """Build a copyable provider resume command for a proven identity."""
    if (
        identity_exact is not True
        or not isinstance(provider, str)
        or provider not in _RESUME_PREFIXES
        or not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or "\0" in session_id
        or not isinstance(cwd, str)
        or "\0" in cwd
    ):
        return ""
    command = shlex.join([*_RESUME_PREFIXES[provider], session_id])
    if cwd:
        command = f"{shlex.join(['cd', '--', cwd])} && {command}"
    return command


def _build_records() -> list[dict]:
    """Sweep all known providers, return the per-session records."""
    records: list[dict] = []
    seen_sids: set[str] = set()
    now_ms = int(time.time() * 1000)
    for provider, info_fn in _INFO_FN.items():
        for pid in _pgrep(provider):
            host, host_pid, ancestors = _parent_walk_for_host(pid)
            # No terminal ancestor means there is no focusable session row.
            if not host:
                continue
            try:
                info = info_fn(pid)
            except (OSError, ValueError, TypeError, AttributeError, OverflowError):
                info = {}
            info = info if isinstance(info, dict) else {}
            known_sid = _session_id(info.get("sessionId"))
            sid = known_sid or f"untracked-{provider}-{pid}"
            if sid in seen_sids:
                continue
            seen_sids.add(sid)
            cwd = _text(info.get("cwd")) or _cwd_of(pid)
            identity_exact = bool(known_sid) and info.get("identityExact") is True
            records.append({
                "provider": provider,
                "sessionId": sid,
                "cwd": cwd,
                "pid": pid,
                "hostPid": host_pid,
                "ancestorPids": ancestors,
                "host": host,
                "tty": "",
                "state": (info.get("state") or "working") if known_sid else "untracked",
                "model": _model_text(info.get("model")),
                "lastPrompt": info.get("lastPrompt") or "",
                "recent": info.get("recent") or [],
                "windowTitle": info.get("windowTitle") or "",
                "lastEvent": "",
                "startedAt": 0,
                "stateChangedAt": now_ms,
                "updatedAt": now_ms,
                "identityExact": identity_exact,
                "resumeCommand": _resume_command(
                    provider, sid, cwd, identity_exact
                ),
            })
    return records


def _read_boot_id() -> str:
    try:
        return BOOT_ID_PATH.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def _load_payload(path: Path = AGGREGATE_PATH) -> dict:
    """Load a saved snapshot and narrow its two record collections."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    else:
        payload = dict(payload)
    for field in ("agents", "recovery"):
        value = payload.get(field)
        payload[field] = (
            [record for record in value if isinstance(record, dict)]
            if isinstance(value, list)
            else []
        )
    return payload


def _record_key(record: dict) -> tuple[str, str] | None:
    provider = record.get("provider")
    session_id = record.get("sessionId")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(session_id, str)
        or not session_id
    ):
        return None
    return provider, session_id


def _valid_desktop(value: object) -> bool:
    return (
        value == "all"
        or (
            isinstance(value, str)
            and len(value) <= 9
            and value.isascii()
            and value.isdecimal()
            and bool(value.strip("0"))
        )
    )


def _parse_desktop_map(value: str | None) -> dict[tuple[str, str], str]:
    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        return {}
    try:
        records = json.loads(unquote(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(records, list):
        return {}
    desktop_map: dict[tuple[str, str], str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        provider = record.get("provider")
        session_id = record.get("sessionId")
        desktop = record.get("desktop")
        if (
            not isinstance(provider, str)
            or provider not in _INFO_FN
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 256
            or not _valid_desktop(desktop)
        ):
            continue
        desktop_map[(provider, session_id)] = desktop
    return desktop_map


def _parse_requested_at(value: str | None) -> int | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    return int(value)


def _apply_desktop_map(
    records: list[dict], desktop_map: dict[tuple[str, str], str]
) -> list[dict]:
    mapped: list[dict] = []
    for source in records:
        record = dict(source)
        key = _record_key(record)
        if key in desktop_map:
            record["desktop"] = desktop_map[key]
        mapped.append(record)
    return mapped


def _recovery_record(record: dict, active: bool) -> dict:
    key = _record_key(record)
    if key is None:
        return {}
    provider, session_id = key

    def text(field: str) -> str:
        value = record.get(field)
        return value if isinstance(value, str) else ""

    timestamp = record.get("updatedAt" if active else "lastSeenAt")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        timestamp = 0
    cwd = text("cwd")
    expected_command = _resume_command(provider, session_id, cwd, True)
    if active:
        command = (
            expected_command if record.get("identityExact") is True else ""
        )
    else:
        saved_command = text("resumeCommand")
        command = saved_command if saved_command == expected_command else ""
    desktop = record.get("desktop")
    return {
        "provider": provider,
        "sessionId": session_id,
        "cwd": cwd,
        "windowTitle": text("windowTitle"),
        "lastPrompt": text("lastPrompt"),
        "host": text("host"),
        "desktop": desktop if _valid_desktop(desktop) else "",
        "lastState": text("state" if active else "lastState"),
        "lastSeenAt": timestamp,
        "model": _model_text(record.get("model")),
        "resumeCommand": command,
    }


def _merge_snapshot(
    records: list[dict], previous: dict, boot_id: str, now_ms: int
) -> dict:
    """Reduce one live scan and one saved snapshot into the next snapshot."""
    previous = previous if isinstance(previous, dict) else {}
    current = [dict(record) for record in records if isinstance(record, dict)]
    saved_boot_id = previous.get("bootId")
    saved_boot_id = saved_boot_id if isinstance(saved_boot_id, str) else ""
    boot_id = boot_id if isinstance(boot_id, str) else ""
    boot_changed = bool(saved_boot_id and boot_id and saved_boot_id != boot_id)
    same_boot = bool(saved_boot_id and boot_id and saved_boot_id == boot_id)

    previous_agents = previous.get("agents")
    if not isinstance(previous_agents, list):
        previous_agents = []
    previous_by_key: dict[tuple[str, str], dict] = {}
    for record in previous_agents:
        if not isinstance(record, dict):
            continue
        key = _record_key(record)
        if key is not None:
            previous_by_key[key] = record
    for record in current:
        key = _record_key(record)
        old = previous_by_key.get(key) if key is not None else None
        if old is not None and not boot_changed:
            if old.get("startedAt"):
                record["startedAt"] = old["startedAt"]
            if (
                old.get("state") == record.get("state")
                and old.get("stateChangedAt")
            ):
                record["stateChangedAt"] = old["stateChangedAt"]
        if (
            old is not None
            and same_boot
            and "desktop" not in record
            and _valid_desktop(old.get("desktop"))
        ):
            record["desktop"] = old["desktop"]
        if old is not None and same_boot and not record.get("model"):
            model = _model_text(old.get("model"))
            if model:
                record["model"] = model
        identity_exact = record.get("identityExact") is True
        record["identityExact"] = identity_exact
        record["resumeCommand"] = _resume_command(
            record.get("provider"),
            record.get("sessionId"),
            record.get("cwd") if isinstance(record.get("cwd"), str) else "",
            identity_exact,
        )

    recovery_by_key: dict[tuple[str, str], dict] = {}
    previous_recovery = previous.get("recovery", [])
    if isinstance(previous_recovery, list):
        for record in previous_recovery:
            if not isinstance(record, dict):
                continue
            key = _record_key(record)
            if key is not None:
                recovery_by_key[key] = _recovery_record(record, active=False)
    if boot_changed:
        previous_agents = previous.get("agents", [])
        if isinstance(previous_agents, list):
            for record in previous_agents:
                if not isinstance(record, dict):
                    continue
                key = _record_key(record)
                if key is not None:
                    recovery_by_key[key] = _recovery_record(record, active=True)
    for record in current:
        key = _record_key(record)
        if key is not None:
            recovery_by_key.pop(key, None)

    counts = {
        "working": 0, "blocked": 0, "idle": 0, "untracked": 0, "total": 0
    }
    for record in current:
        state = record.get("state") or "idle"
        if state not in counts:
            state = "idle"
        counts[state] += 1
    counts["total"] = sum(counts[state] for state in (
        "working", "blocked", "idle", "untracked"
    ))

    bucket = {"blocked": 0, "working": 1, "idle": 2, "untracked": 3}
    current.sort(key=lambda record: (
        bucket.get(record.get("state"), 9), str(record.get("cwd") or "")
    ))
    return {
        "bootId": boot_id or saved_boot_id,
        "updatedAt": now_ms,
        "counts": counts,
        "agents": current,
        "recovery": list(recovery_by_key.values()),
    }


def _aggregate(
    desktop_map: dict[tuple[str, str], str] | None = None,
    path: Path = AGGREGATE_PATH,
) -> dict:
    previous = _load_payload(path)
    boot_id = _read_boot_id()
    records = _build_records()
    if not _confirmed_boot_change(previous, boot_id):
        records = _apply_desktop_map(records, desktop_map or {})
    return _merge_snapshot(
        records, previous, boot_id, int(time.time() * 1000)
    )


def _write_aggregate(payload: dict, path: Path = AGGREGATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _confirmed_boot_change(previous: dict, boot_id: str) -> bool:
    saved_boot_id = previous.get("bootId")
    return bool(
        isinstance(saved_boot_id, str)
        and saved_boot_id
        and boot_id
        and saved_boot_id != boot_id
    )


def _saved_write_is_newer(
    previous: dict, boot_id: str, requested_at: int | None, now_ms: int
) -> bool:
    updated_at = previous.get("updatedAt")
    return (
        requested_at is not None
        and bool(boot_id)
        and previous.get("bootId") == boot_id
        and isinstance(updated_at, (int, float))
        and not isinstance(updated_at, bool)
        and updated_at <= now_ms
        and updated_at > requested_at
    )


def _locked_sweep(
    desktop_map: dict[tuple[str, str], str] | None = None,
    requested_at: int | None = None,
    aggregate_path: Path = AGGREGATE_PATH,
    lock_path: Path | None = None,
) -> tuple[dict, bool]:
    aggregate_path = Path(aggregate_path)
    if lock_path is None:
        lock_path = (
            LOCK_PATH
            if aggregate_path == AGGREGATE_PATH
            else aggregate_path.with_suffix(".lock")
        )
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        lock_file = os.fdopen(lock_fd, "a+")
    except Exception:
        os.close(lock_fd)
        raise
    with lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        boot_id = _read_boot_id()
        previous = _load_payload(aggregate_path)
        now_ms = int(time.time() * 1000)
        if _saved_write_is_newer(previous, boot_id, requested_at, now_ms):
            return previous, False
        global _TRANSCRIPT_CACHE, _CACHE_ACTIVE
        cache_path = aggregate_path.with_suffix(".parsers.json")
        try:
            saved = json.loads(cache_path.read_text())
            entries = saved.get("entries") if isinstance(saved, dict) and saved.get("version") == _CACHE_VERSION else None
            _TRANSCRIPT_CACHE = entries if isinstance(entries, dict) else {}
        except (OSError, ValueError, UnicodeError):
            _TRANSCRIPT_CACHE = {}
        _CACHE_ACTIVE = set()
        try:
            records = _build_records()
            _TRANSCRIPT_CACHE = {key: value for key, value in _TRANSCRIPT_CACHE.items()
                                 if key in _CACHE_ACTIVE}
            _write_aggregate({"version": _CACHE_VERSION, "entries": _TRANSCRIPT_CACHE}, cache_path)
        finally:
            _CACHE_ACTIVE = None
        if not _confirmed_boot_change(previous, boot_id):
            records = _apply_desktop_map(records, desktop_map or {})
        payload = _merge_snapshot(records, previous, boot_id, int(time.time() * 1000))
        _write_aggregate(payload, aggregate_path)
        return payload, True


def _watch(
    interval: float,
    desktop_map: dict[tuple[str, str], str] | None = None,
    requested_at: int | None = None,
) -> int:
    """Run forever and retain the prior complete snapshot after failures."""
    while True:
        try:
            _locked_sweep(desktop_map, requested_at)
        except Exception as exc:
            sys.stderr.write(f"codexbar_agents: sweep failed: {exc}\n")
        time.sleep(interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--watch", action="store_true",
        help="Run forever, sweeping every --interval seconds.",
    )
    parser.add_argument("-i", "--interval", type=float, default=5.0)
    parser.add_argument(
        "--once", action="store_true",
        help="Sweep once and write the aggregate file.",
    )
    parser.add_argument("--desktop-map")
    parser.add_argument("--requested-at")
    args = parser.parse_args(argv)
    desktop_map = _parse_desktop_map(args.desktop_map)
    requested_at = _parse_requested_at(args.requested_at)

    if args.watch:
        return _watch(args.interval, desktop_map, requested_at)
    if args.once:
        _locked_sweep(desktop_map, requested_at)
        return 0

    payload = _aggregate(desktop_map)
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
