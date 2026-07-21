"""OS autostart integration: Windows / launchd (macOS) / systemd (Linux).

Kept dependency-light: on Windows a scheduled-task style approach via the
registry Run key is used (no admin required); macOS writes a LaunchAgent plist;
Linux writes a user systemd unit. All are best-effort and print next steps.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path


def _launch_command() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):  # PyInstaller one-file
        return f'"{exe}" start'
    return f'"{exe}" -m nanobot_connector start'


def install_service() -> None:
    system = platform.system()
    if system == "Windows":
        _install_windows()
    elif system == "Darwin":
        _install_launchd()
    else:
        _install_systemd()


def uninstall_service() -> None:
    system = platform.system()
    if system == "Windows":
        _uninstall_windows()
    elif system == "Darwin":
        _uninstall_launchd()
    else:
        _uninstall_systemd()


# -- Windows (per-user Run key) --------------------------------------------

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_VALUE = "NanobotConnector"


def _install_windows() -> None:
    import winreg  # type: ignore

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _WIN_VALUE, 0, winreg.REG_SZ, _launch_command())
    print("Installed autostart (current user). It will run at next login.")


def _uninstall_windows() -> None:
    import winreg  # type: ignore

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _WIN_VALUE)
        print("Removed autostart.")
    except FileNotFoundError:
        print("Autostart was not installed.")


# -- macOS (LaunchAgent) ----------------------------------------------------

def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "ai.nanobot.connector.plist"


def _install_launchd() -> None:
    exe = sys.executable
    args = [exe] if getattr(sys, "frozen", False) else [exe, "-m", "nanobot_connector"]
    args.append("start")
    program_args = "".join(f"        <string>{a}</string>\n" for a in args)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "    <key>Label</key><string>ai.nanobot.connector</string>\n"
        f"    <key>ProgramArguments</key>\n    <array>\n{program_args}    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        "    <key>KeepAlive</key><true/>\n"
        "</dict>\n</plist>\n"
    )
    path = _launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "load", str(path)], check=False)
    print(f"Installed LaunchAgent at {path}")


def _uninstall_launchd() -> None:
    path = _launchd_plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], check=False)
        path.unlink()
        print("Removed LaunchAgent.")
    else:
        print("LaunchAgent was not installed.")


# -- Linux (user systemd) ---------------------------------------------------

def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "nanobot-connector.service"


def _install_systemd() -> None:
    unit = (
        "[Unit]\nDescription=nanobot connector\nAfter=network-online.target\n\n"
        f"[Service]\nExecStart={_launch_command_argv()}\nRestart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    path = _systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "nanobot-connector"], check=False)
    print(f"Installed systemd user unit at {path}")


def _uninstall_systemd() -> None:
    path = _systemd_unit_path()
    subprocess.run(["systemctl", "--user", "disable", "--now", "nanobot-connector"], check=False)
    if path.exists():
        path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("Removed systemd user unit.")
    else:
        print("systemd unit was not installed.")


def _launch_command_argv() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f"{exe} start"
    return f"{exe} -m nanobot_connector start"
