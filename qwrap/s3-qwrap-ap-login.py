#!/usr/bin/env python3

import os
import shlex
import sys
import subprocess
import time

# ============================================================
# Arista AP - Open all APs in separate iTerm2 tabs
# ============================================================

AP_SCRIPT = os.path.expanduser("~/bin/p")

IPS = [
# For S3 Qwrap - ABZ
"10.87.169.113","10.87.169.243","10.87.169.17","10.87.169.130","10.87.169.112","10.87.169.87",
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

    # Quote for the terminal shell; pass as data to AppleScript separately.
    command_text = shlex.join(command)

    applescript = '''
on run argv
set commandText to item 1 of argv
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
            write text commandText
        end tell
    end tell

end tell
end run
'''

    result = subprocess.run(
        ["osascript", "-e", applescript, command_text],
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
    print(f"Arguments forwarded to p: {shlex.join(ARGS)}")

print()

opened = 0
for ip in IPS:

    print(f"  → {ip}")

    if open_tab(ip):
        opened += 1

    # Small delay prevents iTerm2 from getting flooded
    time.sleep(0.3)


print()
if opened == len(IPS):
    print("✅ All AP sessions opened.")
else:
    print(f"❌ Opened {opened}/{len(IPS)} AP sessions.")
    raise SystemExit(1)
