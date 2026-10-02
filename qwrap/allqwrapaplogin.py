#!/usr/bin/env python3

import os
import sys
import subprocess
import time

# ============================================================
# Arista AP - Open all APs in separate iTerm2 tabs
# ============================================================

AP_SCRIPT = os.path.expanduser("~/bin/p")

IPS = [
    "10.86.205.122",
    "10.86.204.204",
    "10.86.205.60",
    "10.86.205.223",
    "10.86.204.227",
    "10.86.205.165"
    # "10.86.205.240",
    # "10.86.205.151",
    # "10.86.205.157",
    # "10.86.205.90",
    # "10.86.205.88",
    # "10.86.205.49"
]

# ------------------------------------------------------------
# Argument forwarding
# ------------------------------------------------------------

ARGS = sys.argv[1:]


# ------------------------------------------------------------
# Check AP script
# ------------------------------------------------------------

if not os.path.isfile(AP_SCRIPT) or not os.access(AP_SCRIPT, os.X_OK):
    print("❌ AP script not found or not executable:")
    print(f"   {AP_SCRIPT}")
    print()
    print(f"Run: chmod +x {AP_SCRIPT}")
    raise SystemExit(1)


# ------------------------------------------------------------
# Start iTerm2 if needed
# ------------------------------------------------------------

if subprocess.run(
    ["pgrep", "-x", "iTerm2"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL
).returncode != 0:

    print("Starting iTerm2...")
    subprocess.run(["open", "-a", "iTerm"])
    time.sleep(2)


# ------------------------------------------------------------
# AppleScript
# ------------------------------------------------------------

def open_tab(ip):

    command = [AP_SCRIPT, ip] + ARGS

    # Escape command for AppleScript
    command_text = " ".join(
        f'"{arg}"' if " " in arg else arg
        for arg in command
    )

    applescript = f'''
tell application "iTerm2"

    activate

    if (count of windows) = 0 then
        create window with default profile
        delay 1
    end if

    tell window 1
        create tab with default profile
        delay 0.2

        tell current session
            write text "{command_text}"
        end tell
    end tell

end tell
'''

    result = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"     ❌ {result.stderr.strip()}")
        return False

    return True


# ------------------------------------------------------------
# Open all APs
# ------------------------------------------------------------

print(f"Opening {len(IPS)} AP sessions...")

if ARGS:
    print(f"Arguments forwarded to p: {' '.join(ARGS)}")

print()

for ip in IPS:

    print(f"  → {ip}")

    open_tab(ip)

    # Small delay prevents iTerm2 from getting flooded
    time.sleep(0.3)


print()
print("✅ All AP sessions opened.")