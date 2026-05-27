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
from pathlib import Path

AGGREGATE_PATH = Path.home() / ".codexbar" / "agents.json"

# Hosts we treat as terminal emulators when walking the proc tree.
KNOWN_HOSTS = {
    "kitty", "konsole", "code", "code-insiders", "code-flatpak",
    "tmux", "tmux: server", "wezterm", "alacritty",
    "gnome-terminal", "gnome-terminal-", "xterm", "foot",
    "yakuake", "tilix", "ptyhost",
}

# Cmdline tokens that mean "not an interactive agent session" — claude
# desktop helper procs, mcp servers, etc.
_PGREP_CMD_SKIP = (
    "remote-control", "mcp", "--print", "doctor", "agents",
    "--type=", "/claude-desktop-bin/", "claude-desktop",
    "app-server-protocol",
)

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


def _parent_walk_for_host(start_pid: int) -> tuple[str, int]:
    """Walk up the proc tree from start_pid, return (host_name, host_pid).
    Returns ("", 0) if no known terminal emulator found."""
    cur = start_pid
    seen: set[int] = set()
    depth = 12
    while cur > 1 and cur not in seen and depth > 0:
        seen.add(cur)
        depth -= 1
        comm = _comm_of(cur)
        if comm in KNOWN_HOSTS:
            return comm, cur
        cmdline = _cmdline_of(cur)
        if "vscode-server" in cmdline or "/code/" in cmdline or "code-insiders" in cmdline:
            return "code", cur
        nxt = _ppid_of(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return "", 0


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def _pgrep(name: str) -> list[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "-a", name],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out: list[int] = []
    for ln in (proc.stdout or "").splitlines():
        parts = ln.strip().split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            continue
        cmdline = parts[1] if len(parts) > 1 else ""
        if any(tok in cmdline for tok in _PGREP_CMD_SKIP):
            continue
        if name == "codex" and " app-server" in (" " + cmdline):
            continue
        out.append(int(parts[0]))
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

def _is_real_user_prompt(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip().lower()
    return bool(head) and not any(head.startswith(p) for p in _PROMPT_SKIP_PREFIXES)


def _tail_claude_transcript(path: str) -> tuple[str, str]:
    """Returns (ai_title, last_user_prompt) for a Claude transcript. Cached
    by mtime so re-reads are free when nothing has changed."""
    if not path or not os.path.isfile(path):
        return "", ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "", ""
    cached = _TRANSCRIPT_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    title = ""
    last_real, last_any = "", ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                # Cheap filter so we don't json-parse the long assistant rows.
                if '"ai-title"' not in raw and '"role":"user"' not in raw \
                        and '"type":"user"' not in raw:
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
                if t != "user" and rec.get("role") != "user":
                    continue
                inner = rec.get("message") if isinstance(rec.get("message"), dict) else rec
                content = inner.get("content") if isinstance(inner, dict) else None
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            x = c.get("text") or ""
                            if x:
                                text = x
                if text:
                    last_any = text
                    if _is_real_user_prompt(text):
                        last_real = text
    except OSError:
        return "", ""
    chosen = last_real or last_any
    prompt = " ".join(chosen.split())[:200]
    result = (title, prompt)
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
        title, prompt = _tail_claude_transcript(str(transcript))
        if title:
            info["windowTitle"] = title
        if prompt:
            info["lastPrompt"] = prompt
    return info


def _codex_info(pid: int) -> dict:
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    rollout = _open_jsonl_under(pid, "/.codex/sessions/")
    if not rollout:
        return info
    last_real, last_any, last_event = "", "", ""
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
                    if p.get("type") == "message" and p.get("role") == "user":
                        text = ""
                        for c in (p.get("content") or []):
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                text = c.get("text") or text
                        if text:
                            last_any = text
                            if _is_real_user_prompt(text):
                                last_real = text
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
    return info


def _opencode_info(pid: int) -> dict:
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db.is_file():
        return info
    cwd = _cwd_of(pid)
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


def _pi_info(pid: int) -> dict:
    """pi and omp share the same JSONL layout. omp keeps the active file open
    on an fd (so we can see it via /proc/<pid>/fd); pi closes it between
    writes, so we fall back to the cwd → slug lookup.

    State: working while the rollout JSONL was recently written, idle when
    it's been quiet for >30s (matches opencode's threshold)."""
    info = {"sessionId": "", "cwd": "", "windowTitle": "", "lastPrompt": "", "state": "working"}
    rollout = _open_jsonl_under(pid, "/.pi/agent/sessions/", "/.omp/agent/sessions/")
    if not rollout:
        rollout = _find_pi_rollout(_cwd_of(pid))
    if not rollout:
        info["cwd"] = _cwd_of(pid)
        return info

    try:
        mtime_ms = int(os.path.getmtime(rollout) * 1000)
        if int(time.time() * 1000) - mtime_ms > 30_000:
            info["state"] = "idle"
    except OSError:
        pass
    last_real, last_any = "", ""
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t in ("session", "session-meta", "session-start", "meta"):
                    info["sessionId"] = rec.get("id") or info["sessionId"]
                    info["cwd"] = rec.get("cwd") or info["cwd"]
                    title = (rec.get("title") or "").strip()
                    if title:
                        info["windowTitle"] = title
                    continue
                msg_obj = rec.get("message") if isinstance(rec.get("message"), dict) else rec
                if (msg_obj.get("role") or rec.get("role")) != "user":
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
                    last_any = text
                    if _is_real_user_prompt(text):
                        last_real = text
    except OSError:
        return info
    if not info["cwd"]:
        info["cwd"] = _cwd_of(pid)
    chosen = last_real or last_any
    info["lastPrompt"] = " ".join(chosen.split())[:200]
    return info


def _open_jsonl_under(pid: int, *needles: str) -> str:
    """Return the first open JSONL file the pid has whose path contains one
    of the given needles. Returns "" if none."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return ""
    for entry in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, entry))
        except OSError:
            continue
        if not target.endswith(".jsonl"):
            continue
        if any(n in target for n in needles):
            return target
    return ""


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
    now_ms = int(time.time() * 1000)
    for provider, info_fn in _INFO_FN.items():
        for pid in _pids_for(provider):
            host, host_pid = _parent_walk_for_host(pid)
            info = info_fn(pid) or {}
            sid = info.get("sessionId") or f"untracked-{provider}-{pid}"
            records.append({
                "provider": provider,
                "sessionId": sid,
                "cwd": info.get("cwd") or _cwd_of(pid),
                "pid": pid,
                "hostPid": host_pid,
                "host": host,
                "tty": "",
                "state": info.get("state") or "working",
                "lastPrompt": info.get("lastPrompt") or "",
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
