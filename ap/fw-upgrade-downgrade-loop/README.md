# TC-3 firmware upgrade / downgrade loop

Edit `AP_CONFIG` in `fw-upgrade-downgrade-loop.py`. Each key is an AP IP or
hostname; each value contains its full platform-specific `upgrade_url` and
`downgrade_url`. Add as many AP entries as needed. No other local script is
loaded or imported.

```python
AP_CONFIG = {
    "10.86.205.176": {
        "upgrade_url": "https://10.86.107.57/sensorimages/airtight/fw_compilation_c4xx_rfe/c430",
        "downgrade_url": "https://systemtest-upgrade.dt1.wifi.arista.cloud/builds/103/116/1/UPG_BUNDLE_23.1.0F-34_1_0_103_116_1_c430",
    },
}
```

Both URLs use the firmware CLI from `ap-upgrade.py`. The script adds a trailing
slash, but does not append a model directory. Confirm each URL points at the
correct platform, including platform sub-ID where relevant.

Install dependencies and run from this directory:

```bash
python3 -m pip install paramiko pexpect requests
nohup python3 fw-upgrade-downgrade-loop.py > /dev/null 2>&1 &
echo $!
tail -f firmware_loop.log
```

Stop using `kill <PID>`. Stopping the laptop script does not cancel firmware
flashing already started on an AP. The script uses the system SSH client and
Unix file locking, and is intended for macOS/Linux.

Each AP has an independent worker:

1. Wait for ping, SSH and `show device status` reporting WM `Connected`.
2. Skip the initial cooldown once the AP is ready. Subsequent firmware changes
   keep the ten-minute cooldown (`CONNECTED_DWELL_SECONDS`).
3. At startup, compare installed firmware from `show device info` with
   `MWM_VER` from `upgrade_url/version`. If equal, start with downgrade;
   otherwise start with upgrade. Save pending state before triggering firmware.
4. Wait for a changed kernel boot ID and WM `Connected`; record device info.
5. Verify the installed version matches `MWM_VER` fetched from the selected
   URL’s `/version` file before flashing.
6. Wait ten minutes, switch to the other URL, perform the same checks, and repeat.

As in the reboot script, once a cooldown starts its deadline survives SSH
reconnects and WM state changes; this script also persists that deadline across
process restarts. Test client association/configuration and traffic are managed
externally. WM connectivity is the readiness gate, not a client-count check.

`firmware_loop_state.json` contains per-AP successful upgrade/downgrade counts,
completed pairs, pending command details and failures. Counts advance only after
a new boot and WM connectivity. Commands are not automatically resubmitted after
an uncertain result or process restart. Each transition has a 30-minute deadline
(`TRANSITION_TIMEOUT_SECONDS`). If a flash may not have started, the worker waits
for reboot evidence until that deadline rather than risking a duplicate flash.

When an AP reports Connected - Failsafe-mode, the script sends `failsafe recover`,
answers `y` to confirmation, and reconnects. Recovery itself does not increment
firmware counts. An existing cooldown deadline is preserved. A pending firmware
transition keeps its original timeout and version checks.

A transition timeout or version mismatch stops only the affected AP worker and
persists a failure. Review the AP, rotating
`firmware_loop.log` and state file before resuming. To reset a failed AP, stop the
script first, preserve its logs/state as evidence, and remove that AP's entry
from the JSON state file. Restarting then selects that AP’s first direction from its installed version. Do
not clear pending state while an AP could still be flashing.

The test detects boot, WM readiness, target-version and reported failsafe
failures. It does not directly inspect partition integrity or assert that
clients are associated. Use immutable URLs so a build cannot change between
reading its version file and the AP downloading it. Different builds sharing
the same `MWM_VER` cannot be distinguished by this check.

SSH credentials and the OTP signing token have defaults inside the script.
Optional environment overrides: `AP_CONFIG_USER`, `AP_CONFIG_PASSWORD`,
`AP_ROOT_PASSWORD`, `AP_WIFI_OTP_KEY`. Root SSH is tried directly; if necessary,
the config account runs `privilege access` and `rootuser unlock` before retrying.

On each process start, workers without a pending transition recheck the installed
version before choosing their first URL. A saved pending transition always takes
precedence and is verified rather than resubmitted. Completed counts and existing
cooldown deadlines are preserved. Metadata or version parsing errors retry
without issuing a firmware command. `MWM_VER` must be present in each `/version`
file. Old `upgrade_expected_version` / `downgrade_expected_version` fields are no
longer needed; target versions are read automatically.

HTTPS metadata requests validate certificates by default. If a lab endpoint uses
a certificate your laptop does not trust, configure a trusted CA through
`REQUESTS_CA_BUNDLE`, or explicitly set `"version_verify_tls": False` in that
AP's configuration to disable certificate validation for its metadata requests.

A saved initial cooldown from an older script is also skipped if neither an
upgrade nor a downgrade has completed yet. Cooldowns after completed firmware
changes and saved pending transitions remain intact.

Normal logs show request acceptance, download/flash progress every 30 seconds,
reboot/WM readiness, cooldown and completed counts. Expected firmware download
waits no longer produce timeout dumps. The initiating SSH session stays open
while the firmware command runs. `show log upgrade` is not polled automatically,
since it can stream indefinitely and delay readiness checks; collect that log
manually when investigating an AP. Retry messages contain a short reason and
current phase rather than a full pexpect object dump.
