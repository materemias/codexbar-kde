#!/usr/bin/env python3
"""Bring the terminal window hosting an agent session to the front, or
relaunch a history record in a new kitty window or Tern tab.

Invoked as a URL handler via Qt.openUrlExternally() from the plasmoid:
  codexbar://focus/<sessionId>

It looks up the matching sentinel in ~/.codexbar/agents/, walks the agent's
process ancestry, and tries (in order):
  1. kitty's remote control if kitty is somewhere up the tree, or Tern's
     `tern focus` for the session's pane
  2. KWin scripting to activate any window owned by an ancestor pid

The KWin step works for any window kwin manages — VS Code, plain kitty,
Konsole, Yakuake, Wezterm, etc.

  codexbar_focus.py --launch <provider> <sessionId>

starts the saved resume command of a history record. A kitty record gets a
new kitty window in its own systemd scope, so a plasmashell restart does not
kill it, moved to the record's saved virtual desktop. A Tern record gets a
new tab in the running Tern window, whose session daemon owns the process.
"""
from __future__ import annotations

import json
import os
import pwd
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import codexbar_agents as agents

AGENTS_DIR = Path.home() / ".codexbar" / "agents"
AGGREGATE_PATH = Path.home() / ".codexbar" / "agents.json"
T3_DIR = Path.home() / ".t3" / "userdata"


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


def _tern(args: list[str], socket: str = "") -> subprocess.CompletedProcess | None:
    """Run one `tern` scripting command; None when tern is missing or hangs."""
    env = dict(os.environ, TERN_DAEMON_SOCKET=socket) if socket else None
    try:
        return subprocess.run(
            ["tern", *args], capture_output=True, text=True, timeout=5.0,
            check=False, env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _tern_focus(pid: int) -> bool:
    """Show the Tern pane running pid in every Tern window. Agents inherit
    TERN_PANE and TERN_PANE_SOCKET from the pane's shell."""
    pane = agents._environ_value(pid, "TERN_PANE")
    if not pane.isdigit():
        return False
    proc = _tern(["focus", pane], agents._environ_value(pid, "TERN_PANE_SOCKET"))
    return proc is not None and proc.returncode == 0


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


def _load_kwin_script(script: str, name: str | None = None) -> str:
    """Load and start a KWin script; return its id, or "" on failure."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()
    try:
        load = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.loadScript", tmp.name,
             *([name] if name else [])],
            capture_output=True, text=True, timeout=4.0, check=False,
        )
        sid = (load.stdout or "").strip()
        if not sid.isdigit():
            return ""
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.start"],
            capture_output=True, timeout=4.0, check=False,
        )
        return sid
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _stop_kwin_script(sid: str, name: str | None = None) -> None:
    try:
        subprocess.run(
            ["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "stop"],
            capture_output=True, timeout=2.0, check=False,
        )
        if name:
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting",
                 "org.kde.kwin.Scripting.unloadScript", name],
                capture_output=True, timeout=2.0, check=False,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _kwin_activate(candidate_pids: list[int], record: dict | None = None) -> bool:
    """Use KWin scripting to activate the right window. Pid-only match isn't
    enough for Electron apps where many windows share one main pid; we also
    pass a cwd-basename caption hint and prefer matches containing it."""
    if not candidate_pids:
        return False
    script = (KWIN_SCRIPT_TEMPLATE
              .replace("%PIDS%", ",".join(str(p) for p in candidate_pids))
              .replace("%HINT%", _caption_hint(record or {})))
    sid = _load_kwin_script(script)
    if not sid:
        return False
    # Run + immediately stop so kwin doesn't keep the script registered.
    _stop_kwin_script(sid)
    return True


# Keeps the window of one new pid on a saved desktop and activates it there,
# even if the user switched away while kitty started. The script unloads
# itself once it placed the window; the launcher unloads it after
# PLACE_TIMEOUT if the window never appeared.
KWIN_PLACE_TEMPLATE = """
const pid = %PID%;
const want = "%DESKTOP%";
const name = "%NAME%";
let placed = false;
function place(w) {
    if (placed || !w || w.pid !== pid) return;
    placed = true;
    if (want === "all") {
        w.onAllDesktops = true;
    } else {
        const d = workspace.desktops[Number(want) - 1];
        if (d) {
            w.desktops = [d];
            workspace.currentDesktop = d;
        }
    }
    try { workspace.activeWindow = w; } catch (e) {}
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
             "unloadScript", name);
}
workspace.windowAdded.connect(place);
for (const w of workspace.windowList()) place(w);
"""
PLACE_TIMEOUT = 15.0
# A Tern window opened by Launch must have its session daemon up by then.
TERN_START_TIMEOUT = 10.0


def _desktop_count() -> int:
    try:
        out = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/VirtualDesktopManager",
             "org.kde.KWin.VirtualDesktopManager.count"],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    return int(out) if out.isdigit() else 0


def _set_current_desktop(number: int) -> None:
    try:
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/KWin",
             "org.kde.KWin.setCurrentDesktop", str(number)],
            capture_output=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _kwin_place(pid: int, desktop: str) -> bool:
    name = f"codexbar-restore-{pid}"
    script = (KWIN_PLACE_TEMPLATE
              .replace("%PID%", str(pid))
              .replace("%DESKTOP%", desktop)
              .replace("%NAME%", name))
    sid = _load_kwin_script(script, name)
    if not sid:
        return False
    deadline = time.monotonic() + PLACE_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            loaded = subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting",
                 "org.kde.kwin.Scripting.isScriptLoaded", name],
                capture_output=True, text=True, timeout=2.0, check=False,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if loaded == "false":
            return True
    _stop_kwin_script(sid, name)
    return False


def launch(provider: str, session_id: str) -> int:
    """Resume one history record in a new kitty window or Tern tab."""
    payload = agents._load_payload(AGGREGATE_PATH)
    key = (provider, session_id)
    if any(agents._record_key(r) == key for r in payload["agents"]):
        sys.stderr.write("codexbar_focus: session is already running\n")
        return 5
    record = next(
        (r for r in payload["history"] if agents._record_key(r) == key), None
    )
    if record is None:
        sys.stderr.write("codexbar_focus: no history record for session\n")
        return 2
    cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else ""
    command = record.get("resumeCommand")
    if not command or command != agents._resume_command(
        provider, session_id, cwd, True, record.get("codexHome")
    ):
        sys.stderr.write("codexbar_focus: no verified resume command\n")
        return 6
    host = record.get("host")
    if host not in agents.LAUNCH_HOSTS:
        sys.stderr.write("codexbar_focus: only kitty and Tern history records launch\n")
        return 7
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
    shell = shell or "/bin/sh"
    if host == "tern":
        return _launch_tern(cwd, shell, command)
    # An interactive shell loads the user's PATH; the trailing shell keeps
    # the window open after the agent exits, like the original terminal.
    argv = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", "--",
        "kitty", "--directory", cwd or str(Path.home()), "--",
        shell, "-ic", f"{command}; exec {shlex.quote(shell)} -i",
    ]
    desktop = record.get("desktop")
    if not agents._valid_desktop(desktop):
        desktop = ""
    elif desktop != "all" and int(desktop) > _desktop_count():
        sys.stderr.write(
            f"codexbar_focus: desktop {desktop} no longer exists; "
            "opened on the current desktop\n"
        )
        desktop = ""
    elif desktop != "all":
        # Switch first, so the user watches the window open where it lands.
        _set_current_desktop(int(desktop))
    try:
        # systemd-run --scope execs kitty, so this pid is the window's pid.
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        sys.stderr.write(f"codexbar_focus: launch failed: {exc}\n")
        return 8
    if desktop and not _kwin_place(proc.pid, desktop):
        sys.stderr.write("codexbar_focus: launched, but could not move window\n")
    return 0


def _tern_ready() -> bool:
    """A Tern window shows the daemon's sessions. The daemon outlives its
    windows, so an answering daemon alone would take a hidden tab."""
    if not agents.tern_window_pids():
        return False
    proc = _tern(["ls"])
    return proc is not None and proc.returncode == 0


def _start_tern() -> bool:
    """Open a Tern window, which starts its session daemon, in its own
    systemd scope so a plasmashell stop does not kill it."""
    try:
        subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "tern"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        return False
    deadline = time.monotonic() + TERN_START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if _tern_ready():
            return True
    return False


def _launch_tern(cwd: str, shell: str, command: str) -> int:
    """Open the resume command in a new tab of the running Tern window and
    raise that window. Tern's session daemon starts the program, so it
    outlives plasmashell without a scope."""
    if not _tern_ready() and not _start_tern():
        sys.stderr.write("codexbar_focus: launch failed: Tern did not start\n")
        return 8
    proc = _tern([
        "new", "tab", "--json", "--cwd", cwd or str(Path.home()), "--",
        shell, "-ic", f"{command}; exec {shlex.quote(shell)} -i",
    ])
    if proc is None or proc.returncode != 0:
        detail = (proc.stderr.strip() if proc else "") or "tern is not available"
        sys.stderr.write(f"codexbar_focus: launch failed: {detail}\n")
        return 8
    # A tab created from the command line stays in the background.
    try:
        block = json.loads(proc.stdout).get("block")
    except (ValueError, AttributeError):
        block = None
    if isinstance(block, int):
        _tern(["focus", str(block)])
    windows = agents.tern_window_pids()
    if not windows or not _kwin_activate(windows):
        sys.stderr.write("codexbar_focus: launched, but could not raise Tern\n")
    return 0


def _desktop_rank(record: dict) -> tuple[int, int]:
    """Numbered desktops first, then "all", then unknown, like the UI."""
    desktop = record.get("desktop")
    if agents._valid_desktop(desktop) and desktop != "all":
        return 0, int(desktop)
    return (1, 0) if desktop == "all" else (2, 0)


def launch_all(keys: set[tuple[str, str]]) -> int:
    """Resume the given launchable restart rows, one desktop after another.

    Each launch waits for its window to be placed, so desktop switches run
    in order and the user ends on the last desktop restored. Stdout carries
    the JSON list of [provider, sessionId] pairs that actually launched."""
    payload = agents._load_payload(AGGREGATE_PATH)
    live = {agents._record_key(r) for r in payload["agents"]}
    records = sorted(
        (r for r in payload["history"]
         if agents._record_key(r) in keys and agents._record_key(r) not in live
         and r.get("closedBy") == "reboot" and r.get("host") in agents.LAUNCH_HOSTS
         and r.get("resumeCommand")),
        key=_desktop_rank,
    )
    launched: list[list[str]] = []
    for record in records:
        provider, session_id = agents._record_key(record)
        if launch(provider, session_id) == 0:
            launched.append([provider, session_id])
    sys.stdout.write(json.dumps(launched) + "\n")
    if not launched:
        sys.stderr.write("codexbar_focus: nothing to restore\n")
        return 2
    return 0


def _t3_thread_link(session_id: str) -> str:
    """The t3code:// link of the T3 Code thread running a provider session.
    T3 keeps the Claude session id as `resume` and the Codex thread id as
    `threadId` in each thread's resume cursor."""
    try:
        env = (T3_DIR / "environment-id").read_text().strip()
        with sqlite3.connect(f"file:{T3_DIR / 'state.sqlite'}?mode=ro", uri=True, timeout=1) as db:
            row = db.execute(
                "SELECT thread_id FROM provider_session_runtime"
                " WHERE ? IN (json_extract(resume_cursor_json, '$.resume'),"
                " json_extract(resume_cursor_json, '$.threadId'))"
                " ORDER BY last_seen_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    if not env or not row:
        return ""
    return f"t3code://threads/{quote(env, safe='')}/{quote(row[0], safe='')}"


def _t3_open(session_id: str) -> None:
    """Ask T3 Code to show the thread. T3 Code 0.0.44 only raises its window
    for this link; pingdotgg/t3code#9745 tracks opening the thread itself."""
    link = _t3_thread_link(session_id)
    if not link:
        return
    try:
        subprocess.Popen(
            ["xdg-open", link], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
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

    if record.get("host") == "t3code":
        _t3_open(session_id)
        candidates += [p for p in agents.t3_window_pids() if p not in candidates]

    if record.get("host") == "tern":
        _tern_focus(pid)
        candidates += [p for p in agents.tern_window_pids() if p not in candidates]
    # Skip kitty branch if no kitty in the tree.
    elif any(_comm(p) == "kitty" for p in candidates) and _kitty_focus(candidates):
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
    if argv[0] == "--launch":
        if len(argv) != 3:
            return 1
        return launch(argv[1], argv[2])
    if argv[0] == "--launch-all":
        if len(argv) != 2:
            return 1
        return launch_all(agents._parse_session_keys(argv[1]))
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
