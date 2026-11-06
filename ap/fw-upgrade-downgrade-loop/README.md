# Firmware upgrade / downgrade loop

Dependencies: `paramiko`, `pexpect`, `requests`, plus the colocated
`afc-configure-6ghz-outdoor` helper folder.
Edit `AP_CONFIG` with each AP's complete platform-specific upgrade/downgrade
URLs. Metadata comes from `<url>/version` (`MWM_VER`); installed firmware comes
from `show device info` (`Device build`). If installed firmware matches the
upgrade URL, start with downgrade; otherwise start with upgrade.

```bash
python3 fw-upgrade-downgrade-loop.py
nohup python3 fw-upgrade-downgrade-loop.py > /dev/null 2>&1 &
```

Firmware commands run in root with `cli -c "upgrade method https url <url>"`,
with `y` piped to confirmation. Config login runs `privilege access` and
`rootuser unlock` before root SSH.
After submission, reconnect and verify a changed boot ID, expected firmware,
outdoor AFC where applicable, and WM Connected. Then wait ten minutes before
the other firmware URL. The initial change has no cooldown. Cooldown deadlines
survive reconnects and restarts. Test client association/traffic is external.

Connection/readiness/verification failures retry indefinitely, with the same
three-second reconnect interval as the reboot script. There is no overall
30-minute transition timeout and authentication rejection does not permanently
stop the AP worker. Each individual command/login still has a finite timeout.
Only one password/OTP answer is sent per login attempt. Persistent wrong
credentials can still lock accounts when retried; verify config credentials.

Root SSH handles firmware commands, device info, boot ID and outdoor AFC. Config
SSH handles root unlocking, WM status and failsafe recovery. Root is unlocked
before every root connection, including the first connection after a reboot.

For O-405/O-435, the firmware loop executes the working helper after a confirmed
new boot, before WM readiness:

```text
fw-upgrade-downgrade-loop/
  fw-upgrade-downgrade-loop.py
  afc-configure-6ghz-outdoor/
    afc-configure.py
    ap.conf.diff.afc.12345
```

It runs the equivalent of `python3 afc-configure.py --ap <IP>` using the same
Python interpreter/virtual environment as the firmware loop. The helper retains
its proven SCP-to-root and `cp ... /tmp/trigger` flow. Both copied helper files
are byte-identical to the source helper folder. The embedded payload/base64
transfer has been removed. Do not remove the helper folder during cleanup.

Helper output is streamed into `firmware_loop.log` with `[AFC-HELPER]` prefixes.
A successful subprocess exit AND `<IP>: afc-configure succeeded` are required
before saving `afc_configured_boot_id` locally. Reconnects/process restarts skip
the helper for that successful boot. The next firmware reboot permits a new run.
No AP-side tracking file is used. A failed helper is retried; an interrupted
helper whose remote action succeeded without confirmation remains an uncertain
outcome, so local-only tracking cannot guarantee exactly once in that case.

State and counts are in `firmware_loop_state.json`; normal and rotated logs
are `firmware_loop.log*`. Phase tags show SSH, unlock, firmware, reboot, AFC,
verification, WM, and retry. A pending transition is verified on restart and is
never automatically resent. Old persisted failures become `last_failure` history
and no longer block normal startup. `--resume-failed` remains a compatibility
option. Counters advance after successful reboot/build/WM verification.

After stopping the script, clean its local generated files with:

```bash
python3 fw-upgrade-downgrade-loop.py --delete
```

This removes logs and rotations, state and temporary state, lock, mock AFC file
if present, and this script's bytecode cache. Source, README and unrelated files
are preserved. It refuses while the loop lock is held. It does not touch AP files.
Deleting state resets counts and pending history. Stop only after checking that
AP flashing has finished; stopping Python does not cancel AP firmware flashing.

HTTPS metadata validates certificates unless an AP entry explicitly sets
`"version_verify_tls": False`. Config login uses only username `config` and password `Config@123` through
Paramiko’s password API, never terminal pasting. There is no fallback password. Config
credential environment overrides are not used. Root-only environment overrides:
`AP_ROOT_PASSWORD`, `AP_WIFI_OTP_KEY`.
