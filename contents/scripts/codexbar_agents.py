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
import contextlib
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
    "yakuake", "tilix", "ptyhost", "t3code", "tern",
}

# Hosts whose history rows codexbar_focus.py --launch can reopen.
LAUNCH_HOSTS = {"kitty", "tern"}

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


def _argv_of(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _environ_value(pid: int, name: str) -> str:
    """One variable from a process's environment, or ""."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return ""
    prefix = name.encode() + b"="
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix):].decode("utf-8", "replace")
    return ""


def _start_ms(pid: int) -> int:
    """Process start as epoch milliseconds, or 0."""
    stat = _read_text(f"/proc/{pid}/stat")
    try:
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(
            int(line.split()[1])
            for line in _read_text("/proc/stat").splitlines()
            if line.startswith("btime ")
        )
    except (IndexError, ValueError, StopIteration):
        return 0
    return int((btime + ticks / os.sysconf("SC_CLK_TCK")) * 1000)


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
        # Match the executable only; arguments can name any path, such as
        # ~/code/<project> passed to the agent.
        exe = (_argv_of(cur) or [""])[0]
        if "vscode-server" in exe or "/code/" in exe or "code-insiders" in exe:
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
    if host == "tern":
        # Tern's session daemon keeps panes alive between windows, so the
        # window showing a pane may not be its ancestor.
        chain.extend(p for p in tern_window_pids() if p not in seen)
    return host, host_pid, chain


def tern_window_pids() -> list[int]:
    """Pids of running Tern windows: `tern` processes other than its
    session daemon."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _comm_of(pid) == "tern" and _argv_of(pid)[1:2] != ["daemon"]:
            pids.append(pid)
    return pids


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


def _under_t3(pid: int) -> bool:
    """True when T3 Code started the process. T3 drives Claude through
    stream-json and Codex through one `app-server` per thread, so these
    look like services but are its interactive sessions."""
    seen: set[int] = set()
    cur = _ppid_of(pid)
    while cur > 1 and cur not in seen and len(seen) < 12:
        if _comm_of(cur) == "t3code":
            return True
        seen.add(cur)
        cur = _ppid_of(cur)
    return False


def _pgrep(name: str, services: bool = False) -> list[int]:
    """Interactive processes named `name`, or with `services` only the
    background ones (app servers, daemons) that the scan otherwise skips."""
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
        service = _is_service_argv(_argv_of(pid)) and not _under_t3(pid)
        if service != services:
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


def _codex_home(pid: int) -> str:
    """The CODEX_HOME a codex process runs with; ~/.codex by default."""
    home = _environ_value(pid, "CODEX_HOME").rstrip("/")
    return home if home.startswith("/") else str(Path.home() / ".codex")


def _codex_thread_name(home: str, session_id: str) -> str:
    """The thread name Codex shows for a session. session_index.jsonl is
    append-only, so the last line for the id reflects any rename."""
    name = ""
    try:
        with open(f"{home}/session_index.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                if session_id not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id") == session_id:
                    name = _text(entry.get("thread_name")).strip() or name
    except OSError:
        return ""
    return name[:200]


def _codex_meta(path: str) -> tuple[str, str, int]:
    """(id, cwd, creation ms) from a Codex rollout's session_meta line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rec = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return "", "", 0
    payload = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return "", "", 0
    return _text(payload.get("id")), _text(payload.get("cwd")), _ts_ms(payload.get("timestamp"))


def _held_rollouts(pid: int, needle: str) -> set[str]:
    """Every rollout under `needle` that `pid` holds open."""
    fd_dir = f"/proc/{pid}/fd"
    out: set[str] = set()
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return out
    for entry in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, entry))
        except OSError:
            continue
        if needle in target and target.endswith(".jsonl"):
            out.add(target)
    return out


def _codex_rollout_named(home: str, session_id: str) -> str:
    for path in Path(home, "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl"):
        return str(path)
    return ""


def _codex_fresh_rollout(pid: int, home: str, pool: set[str]) -> tuple[str, bool]:
    """The loaded thread a fresh TUI started, and whether that is certain.

    Threads are created on the first prompt, in the TUI's folder. It is
    certain when exactly one loaded thread in that folder was created after
    the TUI started and no other fresh TUI runs there. The app server keeps
    a closed thread loaded for about a minute, but that thread predates any
    TUI started after it."""
    cwd, start = _cwd_of(pid), _start_ms(pid)
    if not cwd or not start:
        return "", False
    mine = []
    for path in pool:
        _sid, thread_cwd, created = _codex_meta(path)
        if thread_cwd == cwd and created >= start - 2000:
            mine.append(path)
    if not mine:
        return "", False
    try:
        newest = max(mine, key=os.path.getmtime)
    except OSError:
        return "", False
    peers = [
        other for other in _pgrep("codex")
        if other != pid and _codex_home(other) == home
        and _cwd_of(other) == cwd and not _resume_arg(other, "resume")
        and not _under_t3(other)
    ]
    return newest, len(mine) == 1 and not peers


def _codex_info(pid: int) -> dict:
    """Older Codex holds its rollout in the TUI. Newer Codex loads threads
    in one `codex app-server` per CODEX_HOME that every TUI shares, so a TUI
    is matched to a loaded thread by its `resume <id>` argv, or by folder
    and creation time."""
    info = {
        "sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "",
        "state": "working", "identityExact": False, "model": "",
    }
    home = _codex_home(pid)
    needle = home + "/sessions/"
    rollout, exact = _open_jsonl_under(pid, needle)
    resume_id = _resume_arg(pid, "resume")
    if not rollout:
        pool: set[str] = set()
        for server in _pgrep("codex", services=True):
            if _codex_home(server) == home:
                pool |= _held_rollouts(server, needle)
        if resume_id:
            rollout = next(
                (p for p in pool if p.endswith(f"-{resume_id}.jsonl")), ""
            ) or _codex_rollout_named(home, resume_id)
            exact = bool(rollout)
        else:
            rollout, exact = _codex_fresh_rollout(pid, home, pool)
    if not rollout:
        return info
    info = _transcript_info(rollout, "codex")
    info["identityExact"] = exact and bool(info["sessionId"]) and (
        not resume_id or info["sessionId"] == resume_id
    )
    info["cwd"] = info["cwd"] or _cwd_of(pid)
    if info["sessionId"] and not info.get("windowTitle"):
        info["windowTitle"] = _codex_thread_name(home, info["sessionId"])
    if home != str(Path.home() / ".codex"):
        info["codexHome"] = home
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


def _flag_value(argv: list[str], flag: str) -> str:
    """The session id given to `flag` in `argv`, or ""."""
    value = ""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            value = argv[i + 1]
        elif a.startswith(flag + "="):
            value = a.split("=", 1)[1]
    ok = (
        value and len(value) <= 256 and not value.startswith("-")
        and all(c.isalnum() or c == "-" for c in value)
    )
    return value if ok else ""


def _resume_arg(pid: int, flag: str) -> str:
    """The session id a process was started to resume, or ""."""
    return _flag_value(_argv_of(pid), flag)


def _shell_resume_arg(pid: int, program: str, flag: str) -> str:
    """pi overwrites its process title, which hides its flags. When a shell
    ran it through `-c`, as CodexBar's Launch does, they are still in that
    shell's command string: the last `program` command there names them."""
    argv = _argv_of(_ppid_of(pid))
    script = next(
        (argv[i + 1] for i, a in enumerate(argv[:-1])
         if a.startswith("-") and not a.startswith("--") and "c" in a),
        "",
    )
    if not script:
        return ""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return ""
    command: list[str] = []
    current: list[str] = []
    for word in words + [";"]:
        if word and set(word) <= set(";&|()"):
            if current and os.path.basename(current[0]) == program:
                command = current
            current = []
        else:
            current.append(word)
    return _flag_value(command, flag)


def _rollout_named(session_id: str) -> str:
    """The root rollout whose file name ends in `_<session_id>.jsonl`."""
    for base in (Path.home() / ".pi" / "agent" / "sessions",
                 Path.home() / ".omp" / "agent" / "sessions"):
        for path in base.glob(f"*/*_{session_id}.jsonl"):
            return str(path)
    return ""


# pi writes a new session's header right after startup and never keeps the
# rollout open, so the header time is what ties a rollout to its process.
_FRESH_SESSION_MS = 30_000


def _header_ms(path: str) -> int:
    """Creation time from a pi/omp rollout's session header, or 0."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(head, dict) or head.get("type") != "session":
        return 0
    return _ts_ms(head.get("timestamp"))


def _created_by(pid: int, rollout: str) -> bool:
    """True when `rollout` is the only session in its folder created during
    the first seconds of `pid`. Two runs started together in one folder,
    `/new`, and `--continue` all fail this and stay unproven."""
    start = _start_ms(pid)
    if not start:
        return False

    def fresh(path: str) -> bool:
        return start - 2000 <= _header_ms(path) <= start + _FRESH_SESSION_MS

    if not fresh(rollout):
        return False
    try:
        siblings = [
            str(p) for p in Path(rollout).parent.glob("*.jsonl")
            if str(p) != rollout and p.stat().st_mtime * 1000 >= start - 2000
        ]
    except OSError:
        return False
    return not any(fresh(p) for p in siblings)


def _pi_info(pid: int, resume_flags: tuple[str, ...] = ("--resume",)) -> dict:
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
    resume_id = next(
        (value for value in (_resume_arg(pid, flag) for flag in resume_flags) if value),
        "",
    ) or next(
        (value for value in (
            _shell_resume_arg(pid, _comm_of(pid), flag) for flag in resume_flags
        ) if value),
        "",
    )
    if not direct and resume_id:
        # A resumed session opens its rollout only after startup; until then
        # the argv names it, where the slug lookup would pick another session.
        rollout = _rollout_named(resume_id) or rollout
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
    info["identityExact"] = bool(info["sessionId"]) and (
        direct
        or info["sessionId"] == resume_id
        or (not resume_id and _created_by(pid, rollout))
    )
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
    "pi": lambda pid: _pi_info(pid, ("--session", "--session-id")),
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

def _codex_home_value(value: object) -> str:
    """A saved non-default CODEX_HOME, or "" when absent or unusable."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(c in value for c in "\0\n")
    ):
        return ""
    return value


def _resume_command(
    provider: str, session_id: str, cwd: str, identity_exact: bool,
    codex_home: object = "",
) -> str:
    """Build a copyable provider resume command for a proven identity.

    A Codex session stored under a non-default CODEX_HOME only resumes with
    that home set, so the command carries it."""
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
    home = _codex_home_value(codex_home) if provider == "codex" else ""
    if home:
        command = f"CODEX_HOME={shlex.quote(home)} {command}"
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
                "codexHome": _codex_home_value(info.get("codexHome")),
                "resumeCommand": _resume_command(
                    provider, sid, cwd, identity_exact, info.get("codexHome")
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
    for field in ("agents", "history"):
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


# Sessions that ended while this boot kept running. Sessions cut off by a
# reboot stay until they run again, however many there are.
HISTORY_LIMIT = 100
_CLOSED_BY = ("reboot", "exit")


def _history_turns(value: object) -> list[dict]:
    """Keep the peek turns a live record carried, bounded like the peek."""
    if not isinstance(value, list):
        return []
    turns: list[dict] = []
    for entry in value[-_PEEK_MSGS:]:
        if (
            isinstance(entry, dict)
            and entry.get("role") in ("user", "assistant")
            and entry.get("kind") in ("text", "tools")
            and isinstance(entry.get("text"), str)
        ):
            turns.append({
                "role": entry["role"],
                "kind": entry["kind"],
                "text": entry["text"][:_PEEK_CHARS],
            })
    return turns


def _history_record(record: dict, active: bool, closed_by: str = "") -> dict:
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
    codex_home = _codex_home_value(record.get("codexHome"))
    expected_command = _resume_command(provider, session_id, cwd, True, codex_home)
    if active:
        command = (
            expected_command if record.get("identityExact") is True else ""
        )
    else:
        saved_command = text("resumeCommand")
        command = saved_command if saved_command == expected_command else ""
    desktop = record.get("desktop")
    if not active:
        closed_by = record.get("closedBy")
    if closed_by not in _CLOSED_BY:
        closed_by = "reboot"
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
        "codexHome": codex_home,
        "closedBy": closed_by,
        "recent": _history_turns(record.get("recent")),
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
            record.get("codexHome"),
        )

    history_by_key: dict[tuple[str, str], dict] = {}
    previous_history = previous.get("history", [])
    if isinstance(previous_history, list):
        for record in previous_history:
            if not isinstance(record, dict):
                continue
            key = _record_key(record)
            if key is not None:
                history_by_key[key] = _history_record(record, active=False)
    current_keys = {
        key for key in (_record_key(record) for record in current)
        if key is not None
    }
    # A process still running under another session id changed identity
    # (for example, the first scan of a resume); it did not exit.
    current_pids = {
        (record.get("provider"), record.get("pid")) for record in current
        if isinstance(record.get("pid"), int)
    }
    # A reboot ends every previous session. On the same boot, a session
    # missing from this scan has exited; untracked processes carry no
    # session identity, so their exits are not history.
    if boot_changed or same_boot:
        for record in previous_agents:
            if not isinstance(record, dict):
                continue
            key = _record_key(record)
            if key is None or key in current_keys:
                continue
            if same_boot and (
                record.get("state") == "untracked"
                or (record.get("provider"), record.get("pid")) in current_pids
            ):
                continue
            history_by_key[key] = _history_record(
                record, active=True,
                closed_by="reboot" if boot_changed else "exit",
            )
    for record in current:
        key = _record_key(record)
        ended = history_by_key.pop(key, None) if key is not None else None
        # A resumed session opens where its history row was placed until
        # the window lookup reports its desktop; a short run keeps it.
        if ended and ended["desktop"] and "desktop" not in record:
            record["desktop"] = ended["desktop"]
    rebooted = [r for r in history_by_key.values() if r["closedBy"] == "reboot"]
    exited = sorted(
        (r for r in history_by_key.values() if r["closedBy"] == "exit"),
        key=lambda r: r["lastSeenAt"], reverse=True,
    )[:HISTORY_LIMIT]

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
        "history": rebooted + exited,
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


@contextlib.contextmanager
def _aggregate_lock(aggregate_path: Path, lock_path: Path | None = None):
    """Hold the writer lock that serializes every aggregate update."""
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
        yield


def _dismiss_history(
    keys: set[tuple[str, str]], aggregate_path: Path = AGGREGATE_PATH
) -> int:
    """Drop the given ended sessions from history; return how many went."""
    aggregate_path = Path(aggregate_path)
    with _aggregate_lock(aggregate_path):
        payload = _load_payload(aggregate_path)
        kept = [r for r in payload["history"] if _record_key(r) not in keys]
        removed = len(payload["history"]) - len(kept)
        if removed:
            payload["history"] = kept
            _write_aggregate(payload, aggregate_path)
        return removed


def _parse_session_keys(value: str | None) -> set[tuple[str, str]]:
    """URI-encoded JSON [[provider, sessionId], ...] from a command line."""
    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        return set()
    try:
        items = json.loads(unquote(value))
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(items, list):
        return set()
    return {
        (item[0], item[1]) for item in items
        if isinstance(item, list) and len(item) == 2
        and isinstance(item[0], str) and item[0] in _INFO_FN
        and isinstance(item[1], str) and 0 < len(item[1]) <= 256
    }


def _locked_sweep(
    desktop_map: dict[tuple[str, str], str] | None = None,
    requested_at: int | None = None,
    aggregate_path: Path = AGGREGATE_PATH,
    lock_path: Path | None = None,
) -> tuple[dict, bool]:
    aggregate_path = Path(aggregate_path)
    with _aggregate_lock(aggregate_path, lock_path):
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
    parser.add_argument("--dismiss")
    args = parser.parse_args(argv)
    desktop_map = _parse_desktop_map(args.desktop_map)
    requested_at = _parse_requested_at(args.requested_at)

    if args.dismiss is not None:
        _dismiss_history(_parse_session_keys(args.dismiss))
        return 0
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
