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
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

AGGREGATE_PATH = Path.home() / ".codexbar" / "agents.json"

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

# In-process cache so we don't re-parse the same transcript every tick when
# nothing has changed. Keyed by absolute path → (mtime, (title, prompt)).
_TRANSCRIPT_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}


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


def _pid_alive(pid: int) -> bool:
    return pid > 0 and Path(f"/proc/{pid}").is_dir()


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


def _scan_cmdline(needle: str, comm_must_be: str | None = None) -> list[int]:
    """Find pids whose cmdline contains `needle`. Set `comm_must_be` to limit
    to a specific binary comm (avoids matching arbitrary shells/editors that
    happen to mention the path on their command line)."""
    out: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if comm_must_be and _comm_of(pid) != comm_must_be:
            continue
        if needle in _cmdline_of(pid):
            out.append(pid)
    return out


def _pids_for(provider: str) -> list[int]:
    # pi sets its proctitle to "pi" via @oh-my-pi/pi-utils' procmgr — `pgrep
    # -x pi` finds it. The bun wrapper script is only visible during the
    # brief startup before the rename, so we don't bother scanning cmdlines.
    if provider == "pi":
        return _pgrep(provider)
    # omp sets its proctitle to "omp" via procmgr (same as pi). Older versions
    # ran as comm="bun" with the script path in cmdline, but that's obsolete.
    if provider == "omp":
        return _pgrep(provider)
    return _pgrep(provider)


# ---------------------------------------------------------------------------
# Transcript parsing (Claude + pi/omp use JSONL)
# ---------------------------------------------------------------------------

def _ts_ms(ts) -> int:
    """Best-effort epoch-ms from whatever timestamp shape a transcript uses
    (ISO string, ms int, or seconds float). 0 when unusable."""
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts if ts > 1e12 else ts * 1000)
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return 0


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
        if c.get("type") == "text" and c.get("text"):
            texts.append(c["text"])
        elif not tool and c.get("name"):
            tool = c.get("name")
    if texts:
        return " ".join(texts)
    return "→ " + tool if tool else ""


def _peek_add(buf: list, role: str, text: str, ts, kind: str = "text") -> None:
    """kind "tools" marks a tool-only turn emitted by the provider walks
    (name only, no arrow prefix). Tagging here — rather than sniffing a "→ "
    prefix later — keeps a user prompt that literally starts with an arrow
    from being merged into a tool run."""
    if role in ("user", "assistant") and text:
        buf.append((role, kind if kind in ("text", "tools") else "text", _peek_squash(text), _ts_ms(ts)))


def _peek_finalize(buf: list) -> list[dict]:
    """Turn the collected (role, kind, text, ts) tuples into the per-session
    peek list. User entries go through the same injected-noise filter as
    lastPrompt, so harness blocks like <system-reminder> never surface.

    Runs of consecutive tool-only turns collapse into one `kind:"tools"`
    entry ("write, edit, bash +2 more") so the panel stays a conversation
    summary instead of a wall of one-word lines."""
    kept = []
    for role, kind, text, ts in buf:
        if not text:
            continue
        if role == "user" and not _is_real_user_prompt(text):
            continue
        kept.append((role, kind, text, ts))
    collapsed: list = []  # [role, kind, payload(list), ts]
    for role, kind, text, ts in kept:
        if kind == "tools" and collapsed and collapsed[-1][1] == "tools":
            collapsed[-1][2].append(text)
            collapsed[-1][3] = ts
        else:
            collapsed.append([role, kind, [text], ts])
    out = []
    for role, kind, payload, ts in collapsed:
        if kind != "tools":
            out.append({"role": role, "kind": "text", "text": payload[0], "ts": ts})
            continue
        shown = payload[:10]
        more = len(payload) - len(shown)
        label = ", ".join(shown) + (f" +{more} more" if more > 0 else "")
        out.append({"role": role, "kind": "tools", "text": label, "ts": ts})
    return out[-_PEEK_MSGS:]


def _is_real_user_prompt(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip().lower()
    return bool(head) and not any(head.startswith(p) for p in _PROMPT_SKIP_PREFIXES)



def _tail_claude_transcript(path: str) -> tuple[str, str, list]:
    """Returns (ai_title, last_user_prompt, recent_messages) for a Claude
    transcript. Cached by mtime so re-reads are free when nothing changed."""
    if not path or not os.path.isfile(path):
        return "", "", []
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "", "", []
    cached = _TRANSCRIPT_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    title = ""
    last_real, last_any = "", ""
    peek: list = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                # Cheap filter so we don't json-parse every long assistant
                # row: only rows that can contribute survive.
                if '"ai-title"' not in raw and '"role":"user"' not in raw \
                        and '"type":"user"' not in raw \
                        and '"role":"assistant"' not in raw \
                        and '"type":"assistant"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t == "ai-title":
                    tt = (rec.get("aiTitle") or "").strip()
                    if tt:
                        title = tt
                    continue
                if t not in ("user", "assistant") and rec.get("role") not in ("user", "assistant"):
                    continue
                inner = rec.get("message") if isinstance(rec.get("message"), dict) else rec
                content = inner.get("content") if isinstance(inner, dict) else None
                text = ""
                is_tool = False
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    tool = ""
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text" and c.get("text"):
                            text = c.get("text") or text
                        elif c.get("type") == "tool_use" and c.get("name") and not tool:
                            tool = c["name"]
                    if not text and tool:
                        text = tool
                        is_tool = True
                role = t if t in ("user", "assistant") else rec.get("role")
                if text and not rec.get("isSidechain"):
                    _peek_add(peek, role, text, rec.get("timestamp"),
                              "tools" if is_tool else "text")
                if role != "user" or not text:
                    continue
                last_any = text
                if _is_real_user_prompt(text):
                    last_real = text
    except OSError:
        return "", "", []
    chosen = last_real or last_any
    prompt = " ".join(chosen.split())[:200]
    result = (title, prompt, _peek_finalize(peek))
    _TRANSCRIPT_CACHE[path] = (mtime, result)
    return result


# ---------------------------------------------------------------------------
# Per-provider info extraction
# ---------------------------------------------------------------------------

def _claude_slug(cwd: str) -> str:
    return "".join(("-" if c in "/_." else c) for c in cwd) if cwd else ""


def _claude_info(pid: int) -> dict:
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    state_file = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    if not state_file.is_file():
        return info
    try:
        rec = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return info
    sid = rec.get("sessionId") or ""
    cwd = rec.get("cwd") or _cwd_of(pid)
    info["sessionId"] = sid
    info["cwd"] = cwd

    status = (rec.get("status") or "").lower()
    waiting = (rec.get("waitingFor") or "").strip()
    if status == "waiting" and waiting:
        info["state"] = "blocked"
    elif status == "busy":
        info["state"] = "working"
    elif status in ("idle", "shell"):
        info["state"] = "idle"

    if sid and cwd:
        transcript = Path.home() / ".claude" / "projects" / _claude_slug(cwd) / f"{sid}.jsonl"
        title, prompt, recent = _tail_claude_transcript(str(transcript))
        if title:
            info["windowTitle"] = title
        if prompt:
            info["lastPrompt"] = prompt
        if recent:
            info["recent"] = recent
    return info


def _codex_info(pid: int) -> dict:
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    rollout = _open_jsonl_under(pid, "/.codex/sessions/")
    if not rollout:
        return info
    last_real, last_any, last_event = "", "", ""
    peek: list = []
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                p = rec.get("payload") or {}
                if t == "session_meta":
                    info["sessionId"] = p.get("id") or info["sessionId"]
                    info["cwd"] = p.get("cwd") or info["cwd"]
                elif t == "response_item":
                    if p.get("type") == "message":
                        role = p.get("role")
                        text = ""
                        for c in (p.get("content") or []):
                            if isinstance(c, dict) and c.get("type") in ("input_text", "output_text"):
                                text = c.get("text") or text
                        _peek_add(peek, role, text, rec.get("timestamp"))
                        if role == "user" and text:
                            last_any = text
                            if _is_real_user_prompt(text):
                                last_real = text
                    elif p.get("type") == "function_call" and p.get("name"):
                        _peek_add(peek, "assistant", p["name"], rec.get("timestamp"), "tools")
                elif t == "event_msg":
                    sub = p.get("type")
                    if sub in ("task_started", "user_message", "task_complete"):
                        last_event = sub
    except OSError:
        return info
    if last_event == "task_complete":
        info["state"] = "idle"
    elif last_event in ("task_started", "user_message"):
        info["state"] = "working"
    if not info["cwd"]:
        info["cwd"] = _cwd_of(pid)
    chosen = last_real or last_any
    info["lastPrompt"] = " ".join(chosen.split())[:200]
    info["recent"] = _peek_finalize(peek)
    return info


def _opencode_info(pid: int) -> dict:
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
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
                    entry = joined.get(mid)
                    if entry is None:
                        role = ""
                        try:
                            role = (json.loads(mdata) or {}).get("role") or ""
                        except (TypeError, json.JSONDecodeError):
                            pass
                        entry = joined[mid] = [role, "", mtime]
                        order.append(mid)
                    if not pdata:
                        continue
                    try:
                        pd = json.loads(pdata) or {}
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if pd.get("type") == "text" and pd.get("text"):
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


def _newest_jsonl(base: Path) -> str:
    """Newest session rollout anywhere under `base`, or "" if there is none.
    Advisor sidecars don't count."""
    try:
        candidates = sorted(
            (p for p in base.rglob("*.jsonl") if not _is_sidecar_jsonl(str(p))),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
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
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    rollout = _open_jsonl_under(pid, "/.pi/agent/sessions/", "/.omp/agent/sessions/")
    if not rollout:
        # A run started with an explicit --session-dir doesn't live under the
        # cwd slug, and the slug lookup would hand it a *different* session's
        # rollout — duplicating that session's row under a wrong pid.
        sess_dir = _session_dir_of(pid)
        rollout = _newest_jsonl(Path(sess_dir)) if sess_dir else _find_pi_rollout(_cwd_of(pid))
    if not rollout:
        info["cwd"] = _cwd_of(pid)
        return info

    last_real, last_any = "", ""
    peek: list = []
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t in ("title", "title_change"):
                    # omp renames a session mid-run and appends the new name
                    # as its own record; the header keeps whatever the name
                    # was at session start. Last one in the file wins, which
                    # is what the terminal tab shows. `title_change.id` is the
                    # message that triggered the rename, never a session id.
                    title = (rec.get("title") or "").strip()
                    if title:
                        info["windowTitle"] = title
                    continue
                if t in ("session", "session-meta", "session-start", "meta"):
                    info["sessionId"] = rec.get("id") or info["sessionId"]
                    info["cwd"] = rec.get("cwd") or info["cwd"]
                    title = (rec.get("title") or "").strip()
                    if title:
                        info["windowTitle"] = title
                    continue
                msg_obj = rec.get("message") if isinstance(rec.get("message"), dict) else rec
                role = msg_obj.get("role") or rec.get("role")
                if role == "assistant":
                    info["state"] = (
                        "working" if msg_obj.get("stopReason") == "toolUse" else "idle"
                    )
                    preview = _content_preview(msg_obj.get("content"))
                    # The "→ " marker here is _content_preview's own output
                    # for tool-only assistant messages, never user input.
                    if preview.startswith("→ "):
                        _peek_add(peek, "assistant", preview[2:], rec.get("timestamp"), "tools")
                    else:
                        _peek_add(peek, "assistant", preview, rec.get("timestamp"))
                    continue
                if role in ("user", "toolResult"):
                    info["state"] = "working"
                if role != "user":
                    continue
                content = msg_obj.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            x = c.get("text") or ""
                            if x:
                                text = x
                if text:
                    _peek_add(peek, "user", text, rec.get("timestamp"))
                    last_any = text
                    if _is_real_user_prompt(text):
                        last_real = text
    except OSError:
        return info
    if not info["cwd"]:
        info["cwd"] = _cwd_of(pid)
    chosen = last_real or last_any
    info["lastPrompt"] = " ".join(chosen.split())[:200]
    info["recent"] = _peek_finalize(peek)
    return info


def _open_jsonl_under(pid: int, *needles: str) -> str:
    """Return the session rollout the pid holds open whose path contains one
    of the given needles. Returns "" if none.

    A rollout the pid has open itself always wins over one reached through an
    advisor sidecar: a process can hold sidecar fds for a session another
    process owns, and picking that would attribute a live session to the wrong
    terminal. Sidecars still count as a fallback, because omp keeps them open
    for sessions whose own rollout fd it has already closed. Within a tier the
    newest mtime wins, since a resumed session leaves the earlier rollout open
    alongside the current one."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return ""
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
            direct.add(target)
            continue
        rollout = _rollout_for_sidecar(target)
        if rollout:
            via_sidecar.add(rollout)
    newest, newest_mtime = "", -1.0
    for path in direct or via_sidecar:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


_INFO_FN = {
    "claude": _claude_info,
    "codex": _codex_info,
    "opencode": _opencode_info,
    "pi": _pi_info,
    "omp": _pi_info,
}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def _build_records() -> list[dict]:
    """Sweep all known providers, return the per-session records."""
    records: list[dict] = []
    seen_sids: set[str] = set()
    now_ms = int(time.time() * 1000)
    for provider, info_fn in _INFO_FN.items():
        for pid in _pids_for(provider):
            host, host_pid, ancestors = _parent_walk_for_host(pid)
            # No terminal ancestor → a daemon, a detached run or a piped call.
            # Nothing the user can focus, so it isn't a session row.
            if not host:
                continue
            info = info_fn(pid) or {}
            sid = info.get("sessionId") or f"untracked-{provider}-{pid}"
            if sid in seen_sids:
                continue
            seen_sids.add(sid)
            records.append({
                "provider": provider,
                "sessionId": sid,
                "cwd": info.get("cwd") or _cwd_of(pid),
                "pid": pid,
                "hostPid": host_pid,
                "ancestorPids": ancestors,
                "host": host,
                "tty": "",
                "state": info.get("state") or "working",
                "lastPrompt": info.get("lastPrompt") or "",
                "recent": info.get("recent") or [],
                "windowTitle": info.get("windowTitle") or "",
                "lastEvent": "",
                "startedAt": 0,
                "stateChangedAt": now_ms,
                "updatedAt": now_ms,
            })
    return records


def _load_previous() -> dict[str, dict]:
    """Read the last-written aggregate so we can carry forward stateChangedAt
    / startedAt across sweeps. Without this, every 5s tick would reset the
    "blocked 12s" age label back to 0."""
    if not AGGREGATE_PATH.is_file():
        return {}
    try:
        prev = json.loads(AGGREGATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for r in (prev.get("agents") or []):
        sid = r.get("sessionId")
        if sid:
            out[sid] = r
    return out


def _aggregate() -> dict:
    records = _build_records()
    prev_by_id = _load_previous()
    for r in records:
        prev = prev_by_id.get(r.get("sessionId"))
        if not prev:
            continue
        # Preserve when we first saw the session, regardless of state changes.
        if prev.get("startedAt"):
            r["startedAt"] = prev["startedAt"]
        # Only roll forward stateChangedAt when state is unchanged. A real
        # transition (working → blocked, blocked → idle, etc.) resets the
        # timer so the popup shows "blocked just now".
        if prev.get("state") == r.get("state") and prev.get("stateChangedAt"):
            r["stateChangedAt"] = prev["stateChangedAt"]

    counts = {"working": 0, "blocked": 0, "idle": 0, "untracked": 0, "total": 0}
    for r in records:
        st = r.get("state") or "idle"
        if st not in counts:
            st = "idle"
        counts[st] += 1
    counts["total"] = counts["working"] + counts["blocked"] + counts["idle"] + counts["untracked"]

    bucket = {"blocked": 0, "working": 1, "idle": 2, "untracked": 3}
    records.sort(key=lambda r: (bucket.get(r.get("state"), 9), r.get("cwd") or ""))

    return {
        "updatedAt": int(time.time() * 1000),
        "counts": counts,
        "agents": records,
    }


def _write_aggregate(payload: dict) -> None:
    AGGREGATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGGREGATE_PATH.with_suffix(AGGREGATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(AGGREGATE_PATH)


def _watch(interval: float) -> int:
    """Run forever, sweeping at the requested interval. Robust to errors —
    we never want to crash the systemd service over a parse glitch."""
    while True:
        try:
            _write_aggregate(_aggregate())
        except Exception as exc:
            sys.stderr.write(f"codexbar_agents: sweep failed: {exc}\n")
        time.sleep(interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run forever, sweeping every --interval seconds.")
    parser.add_argument("-i", "--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="Sweep once and write the aggregate file.")
    args = parser.parse_args(argv)

    if args.watch:
        return _watch(args.interval)

    payload = _aggregate()
    if args.once:
        _write_aggregate(payload)
        return 0
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
