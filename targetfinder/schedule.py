"""Keep the data fresh on the user's own machine: systemd user timer (Linux), launchd agent (macOS), schtasks command (Windows)."""
import os, platform, subprocess
from . import config

SERVICE = "targetfinder-sync"
PLIST = "io.targetfinder.sync.plist"

def bin_path():
    return os.path.join(config.ROOT, "bin", "targetfinder")

def unit_files(every_hours, bin_path, system=None):
    """Text of the scheduler files for this OS, keyed by file name."""
    system = system or platform.system()
    every = max(1, int(every_hours))
    if system == "Darwin":
        return {PLIST: f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.targetfinder.sync</string>
  <key>ProgramArguments</key><array><string>{bin_path}</string><string>sync</string></array>
  <key>StartInterval</key><integer>{every * 3600}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{config.LOG_DIR}/sync.log</string>
  <key>StandardErrorPath</key><string>{config.LOG_DIR}/sync.log</string>
</dict></plist>
"""}
    cal = "*-*-* *:07:00" if every == 1 else f"*-*-* 00/{every}:07:00"
    return {
        f"{SERVICE}.service": f"""[Unit]
Description=TargetFinder collection (platforms + repository HEAD poll)

[Service]
Type=oneshot
ExecStart={bin_path} sync
WorkingDirectory={config.ROOT}
StandardOutput=append:{config.LOG_DIR}/sync.log
StandardError=append:{config.LOG_DIR}/sync.log
TimeoutStartSec=3000
""",
        f"{SERVICE}.timer": f"""[Unit]
Description=run TargetFinder sync every {every} hour(s)

[Timer]
OnCalendar={cal}
RandomizedDelaySec=3min
Persistent=true

[Install]
WantedBy=timers.target
""",
    }

def windows_command(every_hours, bin_path):
    return f'schtasks /Create /SC HOURLY /MO {max(1, int(every_hours))} /TN TargetFinderSync /TR "{bin_path} sync" /F'

def _dir(system):
    return os.path.expanduser("~/Library/LaunchAgents") if system == "Darwin" else os.path.expanduser("~/.config/systemd/user")

def install(every_hours):
    system = platform.system()
    if system == "Windows":
        return {"system": system, "command": windows_command(every_hours, bin_path()), "installed": False}
    config.ensure_dirs()
    d = _dir(system); os.makedirs(d, exist_ok=True)
    files = unit_files(every_hours, bin_path(), system)
    for name, text in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
    if system == "Darwin":
        subprocess.run(["launchctl", "unload", os.path.join(d, PLIST)], capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", os.path.join(d, PLIST)], capture_output=True, text=True)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", f"{SERVICE}.timer"], capture_output=True, text=True)
    return {"system": system, "files": sorted(os.path.join(d, n) for n in files), "installed": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[:500]}

def remove():
    system = platform.system()
    if system == "Windows":
        return {"system": system, "command": "schtasks /Delete /TN TargetFinderSync /F", "removed": False}
    d = _dir(system)
    if system == "Darwin":
        subprocess.run(["launchctl", "unload", "-w", os.path.join(d, PLIST)], capture_output=True)
        names = [PLIST]
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{SERVICE}.timer"], capture_output=True)
        names = [f"{SERVICE}.service", f"{SERVICE}.timer"]
    gone = []
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p): os.remove(p); gone.append(p)
    if system != "Darwin":
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return {"system": system, "removed": gone}

def status():
    system = platform.system()
    if system == "Windows":
        r = subprocess.run(["schtasks", "/Query", "/TN", "TargetFinderSync"], capture_output=True, text=True)
    elif system == "Darwin":
        r = subprocess.run(["launchctl", "list", "io.targetfinder.sync"], capture_output=True, text=True)
    else:
        r = subprocess.run(["systemctl", "--user", "list-timers", f"{SERVICE}.timer", "--no-pager"], capture_output=True, text=True)
    active = r.returncode == 0 and (system != "Linux" or f"{SERVICE}.timer" in r.stdout)
    return {"system": system, "active": active, "output": (r.stdout + r.stderr).strip()[:1000]}
