# File Operation Traffic API Documentation

**Version:** 4.4  
**Endpoint:** `/device/traffic/fileop/config`  
**Method:** `POST`  
**Content-Type:** `application/json`

---

## Table of Contents

1. [Overview](#overview)
2. [API Endpoints](#api-endpoints)
3. [Request Structure](#request-structure)
4. [Complete Field Validation Rules](#complete-field-validation-rules)
5. [Traffic Type Specific Rules](#traffic-type-specific-rules)
6. [Network Type and IP Validation](#network-type-and-ip-validation)
7. [Default Values](#default-values)
8. [Traffic Schedule Validation](#traffic-schedule-validation)
9. [Response Format](#response-format)
10. [Error Messages](#error-messages)
11. [Complete Examples](#complete-examples)

---

## Overview

The File Operation API enables automated file transfers using:
- **TFTP** (Trivial File Transfer Protocol)
- **FTP** (File Transfer Protocol)
- **SFTP** (SSH File Transfer Protocol)

**Supported Operations:**
- Upload files to remote servers
- Download files from remote servers

**Key Features:**
- Configurable file sizes (1-5000 MB)
- DSCP QoS marking support
- IPv4 and IPv6 support
- Automatic interval-based transfers
- Traffic scheduling (weekday/time-based)

---

## API Endpoints

### 1. Configure File Operations
**POST** `/device/traffic/fileop/config`

Create or update file operation configuration.

### 2. Get Configuration
**GET** `/device/traffic/fileop/profile/get`

Retrieve current file operation configuration.

### 3. Restart File Operations
**POST** `/device/traffic/fileop/restart`

Restart all file operation sessions.

### 4. Stop File Operations
**GET** `/device/traffic/fileop/stop`

Stop all active file operation sessions.

### 5. Delete Configuration
**DELETE** `/device/traffic/fileop/delete`

Delete all file operation configurations.

---

## Request Structure

```json
{
  "status": "enable",
  "fileoperations": [
    {
      "status": "enable",
      "traffictype": "sftp",
      "host": "192.168.1.200",
      "port": "22",
      "operation": "download",
      "filesize": 100,
      "packetsize": 1400,
      "interval": 300,
      "username": "testuser",
      "password": "testpass",
      "network": "IPv4",
      "dscpvalue": 0,
      "interface": "",
      "filename": "",
      "mode": "",
      "payload": "",
      "traffic_schedule": {}
    }
  ]
}
```

---

## Complete Field Validation Rules

### Root Level Fields

| Field | Type | Required | Valid Values | Description |
|-------|------|----------|--------------|-------------|
| `status` | string | ✅ **YES** | `enable`, `disable` | Global enable/disable |
| `fileoperations` | array | ✅ **YES** | Array of file operation objects | List of file transfer sessions |

**Validation:**
- ❌ **Missing/Invalid `status`**: "traffic status field is missing or status value is not from enable or disable"

---

### File Operation Object Fields

| Field | Type | Required | Valid Values | Constraints | Default |
|-------|------|----------|--------------|-------------|---------|
| `status` | string | ✅ **YES** | `enable`, `disable` | Session control | - |
| `traffictype` | string | ✅ **YES** | `tftp`, `ftp`, `sftp` | Protocol type | - |
| `host` | string | ✅ **YES** | IPv4/IPv6/hostname | Server address | - |
| `port` | string | ❌ No | `"1"` - `"65535"` | Server port | See defaults |
| `operation` | string | ✅ **YES** | `upload`, `download` | Transfer direction | - |
| `filesize` | integer | ❌ No | 1 - 5000 (MB) | File size to transfer | `100` |
| `packetsize` | integer | ❌ No | Positive integer | Packet size (bytes) | TFTP: `1300` |
| `interval` | integer | ✅ **YES** | > 120 (seconds) | Time between transfers | - |
| `username` | string | ⚠️ **COND** | Any string | FTP/SFTP username | See traffic type |
| `password` | string | ⚠️ **COND** | Any string | FTP/SFTP password | See traffic type |
| `network` | string | ❌ No | `IPv4`, `IPv6` | IP version | Auto-detected |
| `dscpvalue` | integer | ❌ No | 0 - 63 | QoS marking | `0` |
| `interface` | string | ❌ No | Interface name | Network interface | System default |
| `filename` | string | ❌ No | Any string | Specific filename | Auto-generated |
| `mode` | string | ⚠️ **COND** | `netascii`, `octet` | **TFTP only** | `octet` |
| `payload` | string | ❌ No | Any string | Custom payload | - |
| `traffic_schedule` | object | ❌ No | Schedule object | When to run | 24/7 if empty |

**Legend:**
- ✅ **YES** = Always required
- ⚠️ **COND** = Conditionally required (depends on `traffictype`)
- ❌ **No** = Optional

---

## Field-by-Field Validation

### 1. `status` (Session Level)
```
REQUIRED: YES
TYPE: string
VALID VALUES: "enable" | "disable"
ERROR: "status field value should be [enable/disable]"
```

### 2. `traffictype`
```
REQUIRED: YES
TYPE: string
VALID VALUES: "tftp" | "ftp" | "sftp"
ERROR: "traffictype field value should be [tftp/ftp/sftp]"

Protocol Ports (Default):
  - TFTP: Port 69 (UDP)
  - FTP: Port 21 (TCP)
  - SFTP: Port 22 (TCP/SSH)
```

### 3. `host`
```
REQUIRED: YES
TYPE: string
VALID FORMATS:
  ✅ IPv4 address (e.g., "192.168.1.100")
  ✅ IPv6 address (e.g., "2001:db8::1" or "fe80::1")
  ✅ Hostname (e.g., "ftp.example.com")

VALIDATION LOGIC:
  1. Try to parse as IP address (IPv4 or IPv6)
  2. If not IP, validate as hostname (DNS-compatible format)
  3. If network field is provided, verify IP version matches

ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
```

**Examples of Valid Hosts:**
```
✅ "192.168.1.200"          (IPv4)
✅ "10.0.0.1"               (IPv4)
✅ "2001:db8::1"            (IPv6)
✅ "fe80::a1b2:c3d4"        (IPv6)
✅ "ftp.example.com"        (Hostname)
✅ "server.local"           (Hostname)
```

**Examples of Invalid Hosts:**
```
❌ ""                       (Empty string)
❌ "256.1.1.1"              (Invalid IPv4)
❌ "invalid..hostname"      (Invalid hostname format)
❌ "gggg::1"                (Invalid IPv6)
```

### 4. `port`
```
REQUIRED: NO (uses defaults if omitted)
TYPE: string (must be numeric string)
VALID RANGE: "1" - "65535"
FORMAT: String representation of port number

DEFAULT VALUES:
  - TFTP: "69"
  - FTP: "21" (typically)
  - SFTP: "22"

⚠️ NOTE: Port is passed as STRING, not integer
  Correct:   "port": "8080"
  Incorrect: "port": 8080
```

### 5. `operation`
```
REQUIRED: YES
TYPE: string
VALID VALUES: "upload" | "download"
ERROR: "operation field value should be [upload/download]"

DESCRIPTION:
  - "upload": Transfer file FROM agent TO server
  - "download": Transfer file FROM server TO agent
```

### 6. `filesize`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: 1 - 5000 (megabytes)
DEFAULT: 100
ERROR: "filesize field value can not be greater than 5000"

CONSTRAINTS:
  - Maximum file size is 5000 MB (5 GB)
  - Files are auto-generated if not specified
  - For upload: File is created on agent
  - For download: File must exist on server
```

### 7. `packetsize`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: Any positive integer
DEFAULT:
  - TFTP: 1300 bytes
  - FTP/SFTP: Uses protocol defaults

PURPOSE: Controls packet fragmentation and network optimization
```

### 8. `interval`
```
REQUIRED: YES
TYPE: integer
VALID RANGE: > 120 (seconds)
MINIMUM: 121 seconds
ERROR: "file operation timeout value has to be more than 120 seconds"

DESCRIPTION:
  Time between successive file transfer operations

⚠️ CRITICAL CONSTRAINT: MUST be greater than 120 seconds
```

**Validation Logic:**
```javascript
if (interval <= 120) {
  ERROR: "file operation timeout value has to be more than 120 seconds"
}
```

### 9. `username` and `password`
```
REQUIRED: Depends on traffictype
TYPE: string

REQUIREMENTS:
┌──────────┬──────────┬──────────┬─────────────────────┐
│ Protocol │ Username │ Password │ Default             │
├──────────┼──────────┼──────────┼─────────────────────┤
│ TFTP     │ ❌ No    │ ❌ No    │ N/A                 │
├──────────┼──────────┼──────────┼─────────────────────┤
│ FTP      │ ✅ YES   │ ✅ YES   │ "anonymous"/"anon"  │
├──────────┼──────────┼──────────┼─────────────────────┤
│ SFTP     │ ✅ YES   │ ✅ YES   │ No default          │
└──────────┴──────────┴──────────┴─────────────────────┘

ERROR (FTP/SFTP): "ftp/sftp mode will require username and password field"

⚠️ NOTE: TFTP does NOT use authentication
  If username/password provided for TFTP, they are IGNORED
```

**Default Values:**
```javascript
// Automatically set if omitted for FTP
if (traffictype === "ftp") {
  if (!username) username = "anonymous";
  if (!password) password = "anonymous";
}
```

### 10. `network`
```
REQUIRED: NO (optional, but recommended when using IP addresses)
TYPE: string
VALID VALUES: "IPv4" | "IPv6"
DEFAULT: Auto-detected from host
ERROR: "network field value should be [IPv4/IPv6]"

⚠️ IMPORTANT IP VERSION VALIDATION:
  If both network and host (as IP) are provided, they MUST match
```

**Validation Logic:**
```javascript
// If host is IP address AND network is specified
if (isIP(host) && network !== "") {
  if (network === "IPv6" && isIPv4(host)) {
    ERROR: "host field value is not IPv6"
  }
  if (network === "IPv4" && isIPv6(host)) {
    ERROR: "host field value is not IPv4"
  }
}
```

### 11. `dscpvalue`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: 0 - 63
DEFAULT: 0
ERROR: "DSCP value should be in range 0-63"

DESCRIPTION:
  Differentiated Services Code Point for QoS marking

Common DSCP Values:
  - 0:  Best Effort (default)
  - 46: Expedited Forwarding (EF) - VoIP
  - 34: Assured Forwarding 4 (AF41) - Video
  - 26: Assured Forwarding 3 (AF31) - Streaming
  - 10: Assured Forwarding 1 (AF11) - Bulk data
```

### 12. `interface`
```
REQUIRED: NO
TYPE: string
VALID VALUES: Network interface name
DEFAULT: System default interface

EXAMPLES:
  - macOS: "en0", "en1"
  - Linux: "eth0", "wlan0"
  - Windows: "Wi-Fi", "Ethernet"

PURPOSE: Bind traffic to specific network interface
```

### 13. `filename`
```
REQUIRED: NO
TYPE: string
VALID VALUES: Any valid filename string
DEFAULT: Auto-generated

DESCRIPTION:
  - For download: File to download from server
  - For upload: File to upload to server
  - If omitted, uses auto-generated filename
```

### 14. `mode` (TFTP Only)
```
REQUIRED: NO (only applies to TFTP)
TYPE: string
VALID VALUES: "netascii" | "octet"
DEFAULT: "octet"
ERROR: "tftp mode field value should be [netascii/octet]"

⚠️ ONLY VALID FOR TFTP:
  - Other protocols (FTP/SFTP) ignore this field
  - If provided for FTP/SFTP, it is silently ignored

DESCRIPTION:
  - "octet": Binary mode (recommended for all files)
  - "netascii": ASCII text mode (line-ending conversion)
```

**Validation Logic:**
```javascript
if (traffictype === "tftp" && mode !== "") {
  if (mode !== "netascii" && mode !== "octet") {
    ERROR: "tftp mode field value should be [netascii/octet]"
  }
}

// For FTP/SFTP, mode is ignored (no validation error)
```

### 15. `payload`
```
REQUIRED: NO
TYPE: string
VALID VALUES: Any string
DEFAULT: Empty

DESCRIPTION: Custom payload data (use case dependent)
```

### 16. `traffic_schedule`
```
REQUIRED: NO
TYPE: object
STRUCTURE:
  {
    "weekdays": ["Monday", "Tuesday", ...],
    "starthour": 9,
    "endhour": 17
  }
DEFAULT: {} (empty = 24/7 operation)

See "Traffic Schedule Validation" section for full details.
```

---

## Traffic Type Specific Rules

### 📄 TFTP (Trivial File Transfer Protocol)

**Protocol:** UDP
**Default Port:** 69
**Authentication:** None

**Characteristics:**
- Simple, connectionless protocol
- No authentication required
- Suitable for small file transfers
- Often used for network boot and config files

**Required Fields:**
- ✅ `status`
- ✅ `traffictype` = `"tftp"`
- ✅ `host`
- ✅ `operation`
- ✅ `interval` (> 120 seconds)

**Optional Fields:**
- `port` (default: `"69"`)
- `filesize` (default: `100` MB, max: `5000` MB)
- `packetsize` (default: `1300` bytes)
- `mode` (default: `"octet"`)
- `network` (auto-detected from `host`)
- `dscpvalue` (default: `0`)
- `interface`
- `filename`
- `traffic_schedule`

**NOT Used:**
- ❌ `username` (ignored if provided)
- ❌ `password` (ignored if provided)

**Example:**
```json
{
  "status": "enable",
  "traffictype": "tftp",
  "host": "192.168.1.150",
  "port": "69",
  "operation": "download",
  "mode": "octet",
  "filesize": 50,
  "packetsize": 1400,
  "interval": 180,
  "network": "IPv4"
}
```

---

### 📂 FTP (File Transfer Protocol)

**Protocol:** TCP
**Default Port:** 21 (control), 20 (data)
**Authentication:** Username/Password

**Characteristics:**
- Widely supported
- Clear-text authentication (not secure)
- Active and passive modes
- Suitable for general file transfers

**Required Fields:**
- ✅ `status`
- ✅ `traffictype` = `"ftp"`
- ✅ `host`
- ✅ `operation`
- ✅ `interval` (> 120 seconds)
- ✅ `username` **REQUIRED**
- ✅ `password` **REQUIRED**

**Optional Fields:**
- `port` (default: `"21"`)
- `filesize` (default: `100` MB, max: `5000` MB)
- `network` (auto-detected)
- `dscpvalue` (default: `0`)
- `interface`
- `filename`
- `packetsize`
- `traffic_schedule`

**Default Credentials:**
```javascript
// If omitted, automatically set to:
username = "anonymous"
password = "anonymous"
```

**NOT Used:**
- ❌ `mode` (FTP has its own binary/ASCII mode handling)

**Example:**
```json
{
  "status": "enable",
  "traffictype": "ftp",
  "host": "ftp.example.com",
  "port": "21",
  "operation": "upload",
  "filesize": 250,
  "interval": 600,
  "username": "ftpuser",
  "password": "ftppass123",
  "network": "IPv4",
  "dscpvalue": 0
}
```

---

### 🔒 SFTP (SSH File Transfer Protocol)

**Protocol:** TCP (over SSH)
**Default Port:** 22
**Authentication:** SSH credentials
**Security:** Encrypted

**Characteristics:**
- Secure, encrypted file transfer
- Runs over SSH connection
- Requires SSH credentials
- Recommended for sensitive data

**Required Fields:**
- ✅ `status`
- ✅ `traffictype` = `"sftp"`
- ✅ `host`
- ✅ `operation`
- ✅ `interval` (> 120 seconds)
- ✅ `username` **REQUIRED**
- ✅ `password` **REQUIRED**

**Optional Fields:**
- `port` (default: `"22"`)
- `filesize` (default: `100` MB, max: `5000` MB)
- `network` (auto-detected)
- `dscpvalue` (default: `0`)
- `interface`
- `filename`
- `packetsize`
- `traffic_schedule`

**⚠️ IMPORTANT:**
- No default credentials (username/password must be provided)
- SSH key authentication not currently supported (password only)

**NOT Used:**
- ❌ `mode` (SFTP always uses binary mode)

**Example:**
```json
{
  "status": "enable",
  "traffictype": "sftp",
  "host": "192.168.1.200",
  "port": "22",
  "operation": "download",
  "filesize": 1000,
  "interval": 300,
  "username": "sftpuser",
  "password": "securepass123",
  "network": "IPv4",
  "dscpvalue": 46
}
```

---

## Network Type and IP Validation

### IP Version Consistency Rules

The agent validates that `host` and `network` fields are consistent when both are provided.

**Validation Matrix:**

| Host Format | Network Value | Validation | Error |
|-------------|---------------|------------|-------|
| IPv4 (e.g., `192.168.1.1`) | `"IPv4"` | ✅ PASS | - |
| IPv4 (e.g., `192.168.1.1`) | `"IPv6"` | ❌ FAIL | "host field value is not IPv6" |
| IPv4 (e.g., `192.168.1.1`) | (empty/not provided) | ✅ PASS | Auto-detected as IPv4 |
| IPv6 (e.g., `2001:db8::1`) | `"IPv6"` | ✅ PASS | - |
| IPv6 (e.g., `2001:db8::1`) | `"IPv4"` | ❌ FAIL | "host field value is not IPv4" |
| IPv6 (e.g., `2001:db8::1`) | (empty/not provided) | ✅ PASS | Auto-detected as IPv6 |
| Hostname (e.g., `server.com`) | `"IPv4"` | ✅ PASS | Assumes hostname resolves to IPv4 |
| Hostname (e.g., `server.com`) | `"IPv6"` | ✅ PASS | Assumes hostname resolves to IPv6 |
| Hostname (e.g., `server.com`) | (empty/not provided) | ✅ PASS | Uses system DNS resolution |

**Validation Code Logic:**
```javascript
// Step 1: Validate host format
if (!isValidIP(host) && !isValidHostname(host)) {
  ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
}

// Step 2: If host is IP AND network is specified, check consistency
if (isIP(host) && network !== "") {
  if (network === "IPv6" && !isIPv6(host)) {
    ERROR: "host field value is not IPv6"
  }
  if (network === "IPv4" && !isIPv4(host)) {
    ERROR: "host field value is not IPv4"
  }
}
```

### Examples

**✅ Valid Combinations:**
```json
// IPv4 with matching network
{"host": "192.168.1.100", "network": "IPv4"}

// IPv4 without network (auto-detected)
{"host": "192.168.1.100"}

// IPv6 with matching network
{"host": "2001:db8::1", "network": "IPv6"}

// IPv6 without network (auto-detected)
{"host": "fe80::1"}

// Hostname with IPv4 preference
{"host": "ftp.example.com", "network": "IPv4"}

// Hostname without network (uses DNS)
{"host": "server.local"}
```

**❌ Invalid Combinations:**
```json
// IPv4 host with IPv6 network
{
  "host": "192.168.1.100",
  "network": "IPv6"
}
// ERROR: "host field value is not IPv6"

// IPv6 host with IPv4 network
{
  "host": "2001:db8::1",
  "network": "IPv4"
}
// ERROR: "host field value is not IPv4"

// Invalid IP format
{
  "host": "999.999.999.999",
  "network": "IPv4"
}
// ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
```

---

## Default Values

The agent automatically applies default values for optional fields if not provided.

### Default Value Summary

| Field | TFTP Default | FTP Default | SFTP Default | Notes |
|-------|--------------|-------------|--------------|-------|
| `port` | `"69"` | `"21"` | `"22"` | Protocol standard ports |
| `filesize` | `100` | `100` | `100` | Megabytes |
| `packetsize` | `1300` | `0` | `0` | TFTP uses custom packet size |
| `mode` | `"octet"` | - | - | TFTP only |
| `username` | - | `"anonymous"` | - | FTP only default |
| `password` | - | `"anonymous"` | - | FTP only default |
| `dscpvalue` | `0` | `0` | `0` | Best effort QoS |
| `traffic_schedule` | `{}` (24/7) | `{}` (24/7) | `{}` (24/7) | Empty = always on |

### Code Implementation

```go
func (data *FileOperation) setDefaultValues() {
    // TFTP defaults
    if data.TrafficType == "tftp" {
        if data.Port == "" {
            data.Port = "69"
        }
        if data.Mode == "" {
            data.Mode = "octet"
        }
        if data.PacketSize == 0 {
            data.PacketSize = 1300
        }
    }

    // FTP defaults
    if data.TrafficType == "ftp" {
        if data.Username == "" {
            data.Username = "anonymous"
        }
        if data.Password == "" {
            data.Password = "anonymous"
        }
    }

    // Common defaults
    if data.FileSize == 0 {
        data.FileSize = 100
    }
    if data.DscpValue == 0 {
        data.DscpValue = 0
    }
}
```

---

## Traffic Schedule Validation

Same as browsing traffic - controls when file operations execute.

### Schedule Structure

```json
{
  "traffic_schedule": {
    "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "starthour": 9,
    "endhour": 17
  }
}
```

### Fields

| Field | Type | Required | Valid Values | Description |
|-------|------|----------|--------------|-------------|
| `weekdays` | array | ❌ | `Monday` - `Sunday` | Days to run |
| `starthour` | integer | ❌ | 0 - 23 | Start hour (24h) |
| `endhour` | integer | ❌ | 0 - 23 | End hour (24h) |

### Default Behavior

```javascript
// Empty or omitted = 24/7 operation
if (traffic_schedule === {} || traffic_schedule === null) {
  traffic_schedule.default = true;
}
```

### Examples

**Business Hours:**
```json
{
  "traffic_schedule": {
    "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "starthour": 9,
    "endhour": 17
  }
}
```

**Night Hours:**
```json
{
  "traffic_schedule": {
    "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "starthour": 22,
    "endhour": 6
  }
}
```

**24/7:**
```json
{
  "traffic_schedule": {}
}
```

---

## Response Format

### Success Response

**Status Code:** `200 OK`

```json
{
  "status": "success"
}
```

### Error Response

**Status Codes:**
- `400 Bad Request` - Validation error
- `500 Internal Server Error` - Server error
- `503 Service Unavailable` - Server busy

```json
{
  "status": "error",
  "message": "filesize field value can not be greater than 5000"
}
```

---

## Error Messages

### Complete Error Reference

| Validation | Error Message |
|------------|---------------|
| **Global Status** | "traffic status field is missing or status value is not from enable or disable" |
| **Session Status** | "status field value should be [enable/disable]" |
| **Traffic Type** | "traffictype field value should be [tftp/ftp/sftp]" |
| **Operation** | "operation field value should be [upload/download]" |
| **Host Invalid** | "host field value is invalid IPv4/IPv6 or hostname address" |
| **File Size Exceeded** | "filesize field value can not be greater than 5000" |
| **Host IPv6 Mismatch** | "host field value is not IPv6" |
| **Host IPv4 Mismatch** | "host field value is not IPv4" |
| **Interval Too Low** | "file operation timeout value has to be more than 120 seconds" |
| **TFTP Mode Invalid** | "tftp mode field value should be [netascii/octet]" |
| **FTP/SFTP No Credentials** | "ftp/sftp mode will require username and password field" |
| **Network Type Invalid** | "network field value should be [IPv4/IPv6]" |
| **DSCP Range** | "DSCP value should be in range 0-63" |
| **Server Busy** | "Another API call is in progress. Please wait." |

---

## Complete Examples

### Example 1: SFTP Download (Basic)

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "192.168.1.200",
        "port": "22",
        "operation": "download",
        "filesize": 500,
        "interval": 300,
        "username": "sftpuser",
        "password": "sftppass",
        "network": "IPv4"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "success"
}
```

---

### Example 2: FTP Upload with DSCP Marking

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "ftp",
        "host": "ftp.example.com",
        "port": "21",
        "operation": "upload",
        "filesize": 250,
        "interval": 600,
        "username": "ftpuser",
        "password": "ftppass",
        "network": "IPv4",
        "dscpvalue": 34
      }
    ]
  }'
```

---

### Example 3: TFTP Download (Binary Mode)

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "tftp",
        "host": "192.168.1.150",
        "operation": "download",
        "mode": "octet",
        "filesize": 50,
        "packetsize": 1400,
        "interval": 180,
        "network": "IPv4"
      }
    ]
  }'
```

---

### Example 4: SFTP IPv6 with Schedule

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "2001:db8::200",
        "port": "22",
        "operation": "upload",
        "filesize": 1000,
        "interval": 450,
        "username": "user",
        "password": "pass",
        "network": "IPv6",
        "dscpvalue": 46,
        "traffic_schedule": {
          "weekdays": ["Monday", "Wednesday", "Friday"],
          "starthour": 10,
          "endhour": 16
        }
      }
    ]
  }'
```

---

### Example 5: Multiple File Operations

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "192.168.1.200",
        "port": "22",
        "operation": "download",
        "filesize": 500,
        "interval": 300,
        "username": "user1",
        "password": "pass1",
        "network": "IPv4",
        "dscpvalue": 46,
        "traffic_schedule": {
          "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
          "starthour": 9,
          "endhour": 17
        }
      },
      {
        "status": "enable",
        "traffictype": "ftp",
        "host": "ftp.example.com",
        "operation": "upload",
        "filesize": 100,
        "interval": 600,
        "username": "ftpuser",
        "password": "ftppass",
        "network": "IPv4",
        "traffic_schedule": {
          "weekdays": ["Saturday", "Sunday"],
          "starthour": 0,
          "endhour": 23
        }
      },
      {
        "status": "enable",
        "traffictype": "tftp",
        "host": "192.168.1.150",
        "operation": "download",
        "mode": "octet",
        "filesize": 25,
        "interval": 180,
        "network": "IPv4"
      }
    ]
  }'
```

---

### Example 6: Validation Error - Interval Too Low

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "192.168.1.200",
        "operation": "download",
        "interval": 60,
        "username": "user",
        "password": "pass"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "file operation timeout value has to be more than 120 seconds"
}
```

---

### Example 7: Validation Error - Missing FTP Credentials

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "192.168.1.200",
        "operation": "download",
        "interval": 300
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "ftp/sftp mode will require username and password field"
}
```

---

### Example 8: Validation Error - IP Version Mismatch

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "sftp",
        "host": "192.168.1.200",
        "network": "IPv6",
        "operation": "download",
        "interval": 300,
        "username": "user",
        "password": "pass"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "host field value is not IPv6"
}
```

---

### Example 9: Validation Error - File Size Exceeded

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/fileop/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "fileoperations": [
      {
        "status": "enable",
        "traffictype": "ftp",
        "host": "ftp.example.com",
        "operation": "download",
        "filesize": 6000,
        "interval": 300,
        "username": "user",
        "password": "pass"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "filesize field value can not be greater than 5000"
}
```

---

## Summary of Critical Validations

### ✅ Always Required (All Protocols)
- `status` (global and per-session)
- `traffictype`
- `host`
- `operation`
- `interval` (MUST be > 120 seconds)

### ⚠️ Protocol-Specific Requirements

| Protocol | Port Default | Credentials Required | Mode Field |
|----------|--------------|----------------------|------------|
| **TFTP** | `"69"` | ❌ No | ✅ Optional (`netascii`/`octet`) |
| **FTP** | `"21"` | ✅ Yes (defaults to `"anonymous"`) | ❌ Not used |
| **SFTP** | `"22"` | ✅ Yes (no defaults) | ❌ Not used |

### 🔢 Numeric Constraints

| Field | Min | Max | Default |
|-------|-----|-----|---------|
| `filesize` | 1 MB | 5000 MB | 100 MB |
| `interval` | 121 sec | No limit | Required |
| `dscpvalue` | 0 | 63 | 0 |
| `port` | 1 | 65535 | See protocol |

### 🌐 IP Validation Rules

- ✅ If `host` is IP + `network` specified → MUST match
- ✅ If `host` is IP + `network` empty → Auto-detected
- ✅ If `host` is hostname → `network` is optional hint

### 🔄 Default Behavior

- Empty `traffic_schedule` → Runs 24/7
- Omitted `filesize` → 100 MB
- Omitted `port` → Protocol default
- Omitted `mode` (TFTP) → `"octet"`
- Omitted `username`/`password` (FTP) → `"anonymous"`

---

**End of File Operation API Documentation**