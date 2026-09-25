# WiFi Agent Monitor - Quick Start Guide (Standalone Version)

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip3 install pexpect requests urllib3
```

### 2. Configure APs

Edit `wifiagent-monitor.py` and set your AP list (around line 51):

```python
AP_IPS: list[str] = [
    '10.86.205.123',
    '10.86.204.227',
    # Add your APs here
]
```

### 3. Configure Credentials

Edit the credentials in `wifiagent-monitor.py` (around line 75-82):

```python
ONELOGIN_USER = "your.username"
ONELOGIN_PASS = "your_password"
CONFIG_PASS = 'Config@123'
```

### 4. Run the Monitor

```bash
# Basic usage (5-minute interval)
./wifiagent-monitor.py

# Test with single cycle
./wifiagent-monitor.py --once

# Custom interval (3 minutes)
./wifiagent-monitor.py --interval 180

# With debug logging
./wifiagent-monitor.py --logLevel INFO
```

### 5. Stop the Monitor

Press `Ctrl+C` to stop gracefully.

---

## 📋 What It Does

| Scenario                   | Action Taken                     |
| -------------------------- | -------------------------------- |
| Agent is **running**       | ✓ Do nothing (OK)                |
| Agent is **stopped**       | ⚠ Start the agent                |
| Agent is **not installed** | ⚠ Install + start the agent      |
| Start/install **fails**    | ✗ Log error, retry in next cycle |

---

## ⚙️ Key Parameters

- `--interval SECONDS` - How often to check (default: 300s = 5min)
- `--max-retries N` - Retry attempts per AP (default: 2)
- `--once` - Run one cycle only (for testing)
- `--logLevel LEVEL` - Logging detail (WARNING/INFO/DEBUG)

---

## 🔍 Example Output

```
================================================================================
WiFi Agent Monitoring Script
================================================================================
Monitoring 6 Access Points:
  • 10.86.205.123
  • 10.86.204.227
  • 10.86.205.165
  • 10.86.205.240
  • 10.86.205.157
  • 10.86.205.90

Monitoring interval: 300s (5.0 minutes)
Max retries per AP: 2
Mode: Continuous monitoring

Press Ctrl+C to stop monitoring
================================================================================

================================================================================
Monitoring Cycle Started: 2026-08-15 14:30:00
APs to monitor: 6
================================================================================

--- Checking 10.86.205.123 ---
[10.86.205.123]  ✓ wifiagent running (PID=1234) – OK

--- Checking 10.86.204.227 ---
[10.86.204.227]  ⚠ wifiagent stopped – attempting to start
[10.86.204.227]  ✓ wifiagent started successfully (PID=5678)

--- Checking 10.86.205.165 ---
[10.86.205.165]  ⚠ wifiagent not installed – attempting installation
[10.86.205.165]  curl https://...wifiagent
[10.86.205.165]    -> /root/wifiagent/wifiagent.app/wifiagent  (2,345,678 bytes)
[10.86.205.165]  curl https://...config.yaml
[10.86.205.165]    -> /root/wifiagent/conf/config.yaml  (1,234 bytes)
[10.86.205.165]  ✓ Installation completed

================================================================================
Cycle Completed: 2026-08-15 14:33:45
Duration: 225.3s  |  Success: 6/6  |  Failed: 0/6
================================================================================

Next cycle (2) in 300s... (Ctrl+C to stop)
```

---

## 🔧 Running in Background

### Option 1: Using nohup

```bash
# Restart with unbuffered output
nohup python3 -u ./wifiagent-monitor.py > monitor.log 2>&1 &

nohup python3 -u ./wifiagent-monitor.py --interval 600 > /dev/null 2>&1 &
# Now watch logs in real-time
tail -f monitor.log
```

### Option 2: Using screen

```bash
screen -S monitor
./wifiagent-monitor.py
# Ctrl+A then D to detach
# screen -r monitor to reattach
```

---

## 🛑 Troubleshooting

| Problem                        | Solution                                                             |
| ------------------------------ | -------------------------------------------------------------------- |
| `ModuleNotFoundError: pexpect` | Run: `pip3 install pexpect requests urllib3`                         |
| No APs monitored               | Check `AP_IPS` list in `wifiagent-monitor.py` is not empty           |
| Auth failures                  | Verify `ONELOGIN_USER` and `ONELOGIN_PASS` in `wifiagent-monitor.py` |
| Script crashes                 | Run with `--logLevel DEBUG` to see details                           |

---

## 📊 Monitoring Flow

```
Every 5 minutes (configurable):
  For each AP:
    1. Check if WiFi Agent is running
       ├─ Running? → Do nothing ✓
       ├─ Stopped? → Start it ⚠
       └─ Not installed? → Install + start ⚠

    2. If action fails:
       ├─ Retry (up to --max-retries times)
       └─ If still fails, log error and continue to next AP

  3. Wait for next cycle
```

---

## 📁 File

- `wifiagent-monitor.py` - **Standalone** monitoring script (no dependencies on other files)

## ✨ Key Features

- ✅ **Fully Standalone** - No need for `wifiagent.py`, all code embedded
- ✅ **Auto-Recovery** - Starts stopped agents, installs missing agents
- ✅ **Resilient** - Per-AP isolation, failures don't stop monitoring
- ✅ **Configurable** - Edit AP list and credentials directly in script
- ✅ **Production-Ready** - Can run as background service

---

## 👤 Author

Prince Tadhani - 2026-08-15
