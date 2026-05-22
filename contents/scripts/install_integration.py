#!/usr/bin/env python3
"""Set up CodexBar's agent integration.

Hookless — the widget itself runs the aggregator every poll tick. This
script handles the bits the widget can't:

  * QML XHR env: drops env files so plasmashell gets QML_XHR_ALLOW_FILE_READ=1
    on every login, no matter how it's restarted.
  * codexbar:// URL scheme: registers a desktop entry so the widget's
    click-to-focus action can launch codexbar_focus.py.
  * Legacy hook cleanup: strips any old Claude Code hooks left over from
    pre-hookless versions of the plasmoid.

Usage:
  install_integration.py              install everything
  install_integration.py --uninstall  remove everything we installed
  install_integration.py --status     report what's currently installed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
AGGREGATOR_PATH = SCRIPT_DIR / "codexbar_agents.py"
FOCUS_SCRIPT_PATH = SCRIPT_DIR / "codexbar_focus.py"

# Legacy: earlier versions added hooks here. We strip them on install/uninstall.
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_MARKER = "codexbar_agent_hook"

# Sourced by plasmashell at session start (KDE-specific path).
PLASMA_ENV_DIR = Path.home() / ".config" / "plasma-workspace" / "env"
PLASMA_ENV_FILE = PLASMA_ENV_DIR / "codexbar-xhr.sh"
PLASMA_ENV_CONTENTS = (
    "#!/bin/sh\n"
    "# Installed by codexbar-kde — enables file:// XHR reads in plasmashell\n"
    "# so the widget can read ~/.codexbar/agents.json directly.\n"
    "export QML_XHR_ALLOW_FILE_READ=1\n"
)

# Read by systemd-user at login via pam_systemd → propagates to every shell
# and user service. Covers manual restart paths like `kstart plasmashell`
# or `plasmashell --replace` from an existing terminal that the plasma-
# workspace/env script doesn't reach.
SYSTEMD_ENV_DIR = Path.home() / ".config" / "environment.d"
SYSTEMD_ENV_FILE = SYSTEMD_ENV_DIR / "codexbar-xhr.conf"
SYSTEMD_ENV_CONTENTS = (
    "# Installed by codexbar-kde — keeps QML_XHR_ALLOW_FILE_READ in the\n"
    "# systemd-user environment so plasmashell picks it up no matter how\n"
    "# it's launched.\n"
    "QML_XHR_ALLOW_FILE_READ=1\n"
)

# URL handler for the focus-on-click action.
DESKTOP_DIR = Path.home() / ".local" / "share" / "applications"
DESKTOP_FILE = DESKTOP_DIR / "org.codexbar.focus.desktop"
MIMEAPPS_FILE = Path.home() / ".config" / "mimeapps.list"
SCHEME_KEY = "x-scheme-handler/codexbar"

# Legacy: earlier versions shipped a systemd user service that ran the
# aggregator. The widget now drives the refresh itself, so we just clean
# up the old unit on install if it's still around.
LEGACY_SYSTEMD_FILE = Path.home() / ".config" / "systemd" / "user" / "codexbar-agents.service"
LEGACY_SYSTEMD_NAME = "codexbar-agents.service"


# ---------------------------------------------------------------------------
# Legacy hook cleanup (removes the entries earlier versions wrote)
# ---------------------------------------------------------------------------

def _is_our_hook_entry(entry: dict) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(hooks, list):
        return False
    return any(HOOK_MARKER in str(h.get("command") or "") for h in hooks if isinstance(h, dict))


def _strip_our_hooks(events: dict) -> dict:
    cleaned: dict = {}
    for ev, lst in (events or {}).items():
        if not isinstance(lst, list):
            cleaned[ev] = lst
            continue
        kept = [e for e in lst if not _is_our_hook_entry(e)]
        if kept:
            cleaned[ev] = kept
    return cleaned


def remove_legacy_hooks() -> str:
    if not CLAUDE_SETTINGS.is_file():
        return "no claude settings file — nothing to remove"
    try:
        settings = json.loads(CLAUDE_SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return f"couldn't parse {CLAUDE_SETTINGS} — left it alone"
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else None
    if not hooks:
        return "no hooks block in claude settings"
    cleaned = _strip_our_hooks(hooks)
    if cleaned == hooks:
        return "no legacy codexbar hooks to remove"
    # Back up then write.
    backup = CLAUDE_SETTINGS.with_suffix(CLAUDE_SETTINGS.suffix + f".bak-{int(time.time())}")
    shutil.copy2(CLAUDE_SETTINGS, backup)
    if cleaned:
        settings["hooks"] = cleaned
    else:
        settings.pop("hooks", None)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2))
    return f"removed legacy hooks (backup: {backup.name})"


# ---------------------------------------------------------------------------
# Env script
# ---------------------------------------------------------------------------

def install_env_script() -> str:
    msgs: list[str] = []

    # 1. Plasma-workspace env (sourced by startplasma).
    PLASMA_ENV_DIR.mkdir(parents=True, exist_ok=True)
    if not (PLASMA_ENV_FILE.is_file()
            and _try_read(PLASMA_ENV_FILE) == PLASMA_ENV_CONTENTS):
        PLASMA_ENV_FILE.write_text(PLASMA_ENV_CONTENTS)
        try:
            os.chmod(PLASMA_ENV_FILE, 0o755)
        except OSError:
            pass
        msgs.append(f"installed {PLASMA_ENV_FILE.name}")

    # 2. systemd-user environment (read by pam_systemd at login).
    SYSTEMD_ENV_DIR.mkdir(parents=True, exist_ok=True)
    if not (SYSTEMD_ENV_FILE.is_file()
            and _try_read(SYSTEMD_ENV_FILE) == SYSTEMD_ENV_CONTENTS):
        SYSTEMD_ENV_FILE.write_text(SYSTEMD_ENV_CONTENTS)
        msgs.append(f"installed {SYSTEMD_ENV_FILE.name}")

    # 3. Push into the running systemd-user manager so subsequent restarts
    # (kquitapp6/kstart, plasmashell --replace, etc) from the current session
    # see the var without waiting for next login.
    _systemctl("set-environment", "QML_XHR_ALLOW_FILE_READ=1")
    msgs.append("set in current systemd-user environment")

    return "env: " + ("; ".join(msgs) if msgs else "already current")


def _try_read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def remove_env_script() -> str:
    removed: list[str] = []
    for f in (PLASMA_ENV_FILE, SYSTEMD_ENV_FILE):
        if f.is_file():
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
    _systemctl("unset-environment", "QML_XHR_ALLOW_FILE_READ")
    return "env: removed " + (", ".join(removed) if removed else "nothing")


# ---------------------------------------------------------------------------
# URL scheme handler
# ---------------------------------------------------------------------------

def install_focus_url_handler() -> str:
    if not FOCUS_SCRIPT_PATH.is_file():
        return f"skipped url handler (focus script missing: {FOCUS_SCRIPT_PATH})"
    try:
        os.chmod(FOCUS_SCRIPT_PATH, 0o755)
    except OSError:
        pass
    desktop_contents = (
        "[Desktop Entry]\n"
        "Name=CodexBar Focus Agent\n"
        f"Exec={FOCUS_SCRIPT_PATH} %u\n"
        "Type=Application\n"
        "NoDisplay=true\n"
        f"MimeType={SCHEME_KEY};\n"
        "X-KDE-Protocols=codexbar\n"
    )
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(desktop_contents)
    _set_mimeapps_default(SCHEME_KEY, DESKTOP_FILE.name)
    try:
        subprocess.run(
            ["update-desktop-database", str(DESKTOP_DIR)],
            capture_output=True, timeout=5.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"registered codexbar:// → {DESKTOP_FILE.name}"


def remove_focus_url_handler() -> str:
    if DESKTOP_FILE.is_file():
        DESKTOP_FILE.unlink()
    if MIMEAPPS_FILE.is_file():
        try:
            text = MIMEAPPS_FILE.read_text()
        except OSError:
            return f"removed {DESKTOP_FILE.name}"
        kept = [ln for ln in text.splitlines() if ln.split("=", 1)[0].strip() != SCHEME_KEY]
        MIMEAPPS_FILE.write_text("\n".join(kept) + "\n")
    return f"removed {DESKTOP_FILE.name} and unregistered codexbar://"


def _set_mimeapps_default(scheme: str, desktop_name: str) -> None:
    MIMEAPPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = MIMEAPPS_FILE.read_text() if MIMEAPPS_FILE.is_file() else ""
    out: list[str] = []
    in_section = False
    inserted = False
    has_section = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not inserted:
                out.append(f"{scheme}={desktop_name};")
                inserted = True
            in_section = (stripped == "[Default Applications]")
            if in_section:
                has_section = True
            out.append(ln)
            continue
        if in_section and "=" in ln and ln.split("=", 1)[0].strip() == scheme:
            out.append(f"{scheme}={desktop_name};")
            inserted = True
            continue
        out.append(ln)
    if in_section and not inserted:
        out.append(f"{scheme}={desktop_name};")
        inserted = True
    if not has_section:
        if out and out[-1].strip():
            out.append("")
        out.append("[Default Applications]")
        out.append(f"{scheme}={desktop_name};")
    MIMEAPPS_FILE.write_text("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# systemctl helper (used for set-environment / unset-environment only)
# ---------------------------------------------------------------------------

def _systemctl(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=8.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out


def remove_legacy_systemd_service() -> str:
    """Earlier versions shipped a systemd --user service to run the
    aggregator. The widget drives the refresh itself now, so the service is
    dead weight — scrub it on install/uninstall if it's still around."""
    if not LEGACY_SYSTEMD_FILE.is_file():
        return ""
    _systemctl("disable", "--now", LEGACY_SYSTEMD_NAME)
    try:
        LEGACY_SYSTEMD_FILE.unlink()
    except OSError:
        return f"failed to remove {LEGACY_SYSTEMD_NAME}"
    _systemctl("daemon-reload")
    return f"removed legacy {LEGACY_SYSTEMD_NAME}"


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def install_all() -> str:
    """Install: env scripts so plasmashell gets QML_XHR_ALLOW_FILE_READ on
    every launch, URL scheme handler for click-to-focus, and legacy cleanup
    (Claude Code hooks + the systemd service we used to ship)."""
    parts = [
        install_env_script(),
        install_focus_url_handler(),
        remove_legacy_hooks(),
        remove_legacy_systemd_service(),
    ]
    # Seed the aggregate so the widget has something to read on first open.
    try:
        subprocess.run(
            [sys.executable or "python3", str(AGGREGATOR_PATH), "--once"],
            capture_output=True, timeout=10.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "\n".join(p for p in parts if p)


def uninstall_all() -> str:
    parts = [
        remove_legacy_systemd_service(),
        remove_legacy_hooks(),
        remove_focus_url_handler(),
        remove_env_script(),
    ]
    return "\n".join(parts)


def status_all() -> str:
    plasma_env = "yes" if PLASMA_ENV_FILE.is_file() else "no"
    systemd_env = "yes" if SYSTEMD_ENV_FILE.is_file() else "no"
    url_ok = "yes" if DESKTOP_FILE.is_file() else "no"
    return (
        f"plasma-workspace env script: {plasma_env}\n"
        f"systemd-user env file:       {systemd_env}\n"
        f"focus url handler:           {url_ok}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        print(status_all())
        return 0
    print(uninstall_all() if args.uninstall else install_all())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
