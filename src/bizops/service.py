"""Install bizops as a background service that starts on login and restarts if it crashes.

Linux: systemd user service. macOS: launchd agent. Windows: a scheduled task at logon.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.bizops.serve"


def _cmd(home: Path, port: int) -> list[str]:
    exe = shutil.which("bizops") or str(Path(sys.executable).with_name("bizops"))
    return [exe, "serve", "--port", str(port)]


def install(home: Path, port: int = 8000, start: bool = True) -> str:
    system = platform.system()
    cmd = _cmd(home, port)
    if system == "Linux":
        unit_dir = Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "bizops.service"
        unit.write_text(f"""[Unit]
Description=bizops: AI-run businesses (dashboard + scheduler)
After=network-online.target

[Service]
WorkingDirectory={home}
Environment=BIZOPS_HOME={home}
ExecStart={' '.join(cmd)}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""")
        if start and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "--now", "bizops"], check=False)
            if shutil.which("loginctl"):  # keep running after logout
                subprocess.run(["loginctl", "enable-linger"], check=False)
        return str(unit)
    if system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        logs = home / "data"
        logs.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{home}</string>
<key>EnvironmentVariables</key><dict><key>BIZOPS_HOME</key><string>{home}</string>
<key>PATH</key><string>{Path(cmd[0]).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{logs}/service.log</string>
<key>StandardErrorPath</key><string>{logs}/service.log</string>
</dict></plist>
""")
        if start:
            subprocess.run(["launchctl", "unload", str(plist)], check=False, capture_output=True)
            subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
        return str(plist)
    if system == "Windows":
        task = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        if start:
            subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "bizops", "/TR", task], check=False)
            subprocess.run(["schtasks", "/Run", "/TN", "bizops"], check=False)
        return "scheduled task 'bizops'"
    raise RuntimeError(f"unsupported OS: {system}")


def uninstall() -> None:
    system = platform.system()
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "bizops"], check=False)
        (Path.home() / ".config/systemd/user/bizops.service").unlink(missing_ok=True)
    elif system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink(missing_ok=True)
    elif system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "bizops"], check=False)
