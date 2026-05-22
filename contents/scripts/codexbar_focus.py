#!/usr/bin/env python3
"""Bring the terminal window hosting an agent session to the front.

Invoked as a URL handler via Qt.openUrlExternally() from the plasmoid:
  codexbar://focus/<sessionId>

It looks up the matching sentinel in ~/.codexbar/agents/, walks the agent's
process ancestry, and tries (in order):
  1. kitty's remote control if kitty is somewhere up the tree
  2. KWin scripting to activate any window owned by an ancestor pid

The KWin step works for any window kwin manages — VS Code, plain kitty,
Konsole, Yakuake, Wezterm, etc.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AGENTS_DIR = Path.home() / ".codexbar" / "agents"
AGGREGATE_PATH = Path.home() / ".codexbar" / "agents.json"


def _find_sentinel(session_id: str) -> dict | None:
    # Per-session sentinel files (Claude with hooks installed).
    if AGENTS_DIR.is_dir():
        for entry in AGENTS_DIR.glob("*.json"):
            if entry.name.endswith(".tmp.json"):
                continue
            try:
                rec = json.loads(entry.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId") == session_id:
                return rec
    # Virtual records (Codex, untracked Claude) only live in the aggregate file.
    if AGGREGATE_PATH.is_file():
        try:
            agg = json.loads(AGGREGATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        for rec in (agg.get("agents") or []):
            if isinstance(rec, dict) and rec.get("sessionId") == session_id:
                return rec
    return None


def _ancestor_pids(start_pid: int, max_depth: int = 16) -> list[int]:
    """Walk /proc up from start_pid; return [start_pid, parent, grandparent, …]."""
    pids: list[int] = []
    cur = int(start_pid or 0)
    while cur > 1 and len(pids) < max_depth:
        if cur in pids:
            break
        pids.append(cur)
        try:
            status = Path(f"/proc/{cur}/status").read_text()
        except OSError:
            break
        ppid = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split(":", 1)[1].strip())
                except ValueError:
                    ppid = 0
                break
        if not ppid:
            break
        cur = ppid
    return pids


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _kitty_focus(candidate_pids: list[int]) -> bool:
    """Try every kitty socket we can find for a window matching one of the pids.

    Works only when kitty has `allow_remote_control yes` and a `listen_on`
    socket. We try abstract sockets first (newer kitty default) then anything
    under /tmp.
    """
    sockets: list[str] = []
    for path in Path("/tmp").glob("mykitty-*"):
        sockets.append(f"unix:{path}")
    for path in Path("/tmp").glob("kitty-*"):
        sockets.append(f"unix:{path}")
    # Abstract sockets — kitty's default `listen_on` template.
    for pid in candidate_pids:
        sockets.append(f"unix:@kitty-{pid}")
        sockets.append(f"unix:@mykitty-{pid}")

    seen = set()
    for sock in sockets:
        if sock in seen:
            continue
        seen.add(sock)
        for pid in candidate_pids:
            try:
                proc = subprocess.run(
                    ["kitty", "@", "--to", sock, "focus-window", "--match", f"pid:{pid}"],
                    capture_output=True, timeout=2.0, check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False  # kitty not installed → no point retrying
            if proc.returncode == 0:
                return True
    # Last attempt: default socket (no --to).
    for pid in candidate_pids:
        try:
            proc = subprocess.run(
                ["kitty", "@", "focus-window", "--match", f"pid:{pid}"],
                capture_output=True, timeout=2.0, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        if proc.returncode == 0:
            return True
    return False


KWIN_SCRIPT_TEMPLATE = """
const targets = new Set([%PIDS%]);
const captionHint = "%HINT%".toLowerCase();
const wins = workspace.windowList ? workspace.windowList() : workspace.clientList();
// Multi-window apps like VS Code share one main pid across all their
// windows, so a pure pid match can grab the wrong window. Prefer one whose
// caption contains the agent's cwd basename (workspace folder name); fall
// back to any pid match.
let preferred = null;
let fallback = null;
for (const w of wins) {
    if (!w || typeof w.pid === "undefined") continue;
    if (!targets.has(w.pid)) continue;
    if (!fallback) fallback = w;
    if (captionHint && w.caption &&
        w.caption.toLowerCase().indexOf(captionHint) >= 0) {
        preferred = w;
        break;
    }
}
const target = preferred || fallback;
if (target) {
    try { target.minimized = false; } catch (e) {}
    // If the window lives on another virtual desktop, switch to it first;
    // KWin won't focus a window that isn't on the current desktop. Plasma 6
    // exposes `desktops` as an array; Plasma 5 used a numeric `desktop`.
    try {
        if (target.desktops && target.desktops.length > 0) {
            workspace.currentDesktop = target.desktops[0];
        } else if (typeof target.desktop === "number" && target.desktop > 0) {
            workspace.currentDesktop = target.desktop;
        }
    } catch (e) {}
    // Same idea for Activities.
    try {
        if (target.activities && target.activities.length > 0 &&
            workspace.currentActivity &&
            target.activities.indexOf(workspace.currentActivity) < 0) {
            workspace.currentActivity = target.activities[0];
        }
    } catch (e) {}
    try { workspace.activeWindow = target; } catch (e) {}
    try { if (workspace.raiseWindow) workspace.raiseWindow(target); } catch (e) {}
}
"""


def _caption_hint(record: dict) -> str:
    """The basename of cwd, escaped for embedding in a JS string literal."""
    cwd = (record or {}).get("cwd") or ""
    base = ""
    for part in reversed(cwd.split("/")):
        if part:
            base = part
            break
    # Strip anything that could break the JS string. Cwd basenames don't
    # normally contain these but be paranoid.
    return "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_", ".", " "))


def _kwin_activate(candidate_pids: list[int], record: dict | None = None) -> bool:
    """Use KWin scripting to activate the right window. Pid-only match isn't
    enough for Electron apps where many windows share one main pid; we also
    pass a cwd-basename caption hint and prefer matches containing it."""
    if not candidate_pids:
        return False
    script = (KWIN_SCRIPT_TEMPLATE
              .replace("%PIDS%", ",".join(str(p) for p in candidate_pids))
              .replace("%HINT%", _caption_hint(record or {})))
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()
    try:
        # Load (returns the script id)
        load = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.loadScript", tmp.name],
            capture_output=True, text=True, timeout=4.0, check=False,
        )
        sid = (load.stdout or "").strip()
        if not sid.lstrip("-").isdigit():
            return False
        # Run + immediately stop so kwin doesn't keep the script registered.
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.start"],
            capture_output=True, timeout=4.0, check=False,
        )
        subprocess.run(
            ["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}",
             "stop"],
            capture_output=True, timeout=2.0, check=False,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def focus(session_id: str) -> int:
    record = _find_sentinel(session_id)
    if not record:
        sys.stderr.write(f"codexbar_focus: no sentinel for session {session_id}\n")
        return 2

    pid = int(record.get("pid") or 0)
    candidates = _ancestor_pids(pid)
    if not candidates:
        sys.stderr.write("codexbar_focus: no ancestry — claude pid already gone\n")
        return 3

    # Skip kitty branch if no kitty in the tree.
    has_kitty = any(_comm(p) == "kitty" for p in candidates)
    if has_kitty and _kitty_focus(candidates):
        return 0

    if _kwin_activate(candidates, record):
        return 0

    sys.stderr.write(
        f"codexbar_focus: couldn't focus pids={candidates} host={record.get('host')}\n"
    )
    return 4


def main(argv: list[str]) -> int:
    if not argv:
        return 1
    arg = argv[0]
    prefix = "codexbar://focus/"
    if arg.startswith(prefix):
        session_id = arg[len(prefix):]
    else:
        session_id = arg
    # Strip trailing slash, query, fragment.
    for sep in ("/", "?", "#"):
        if sep in session_id:
            session_id = session_id.split(sep, 1)[0]
    return focus(session_id.strip())


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        sys.stderr.write(f"codexbar_focus: {exc}\n")
        sys.exit(99)
