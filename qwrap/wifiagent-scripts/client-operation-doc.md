# Client Traffic API Documentation

**Version:** 4.4  
**Endpoint:** `/device/traffic/client/config`  
**Method:** `POST`  
**Content-Type:** `application/json`

---

## Table of Contents

1. [Overview](#overview)
2. [API Endpoints](#api-endpoints)
3. [Request Structure](#request-structure)
4. [Complete Field Validation Rules](#complete-field-validation-rules)
5. [Traffic Type Specific Rules](#traffic-type-specific-rules)
6. [Host Format Requirements](#host-format-requirements)
7. [Network and IP Version Validation](#network-and-ip-version-validation)
8. [Traffic Schedule Validation](#traffic-schedule-validation)
9. [Response Format](#response-format)
10. [Error Messages](#error-messages)
11. [Complete Examples](#complete-examples)

---

## Overview

The Client Traffic API enables automated client-side HTTP, HTTPS, QUIC, and TCP traffic generation for network testing.

**Supported Protocols:**
- **HTTP** - Unencrypted HTTP traffic (GET, POST, PUT)
- **HTTPS** - Encrypted HTTPS traffic (GET, POST, PUT)
- **QUICT** - QUIC protocol traffic (upload/download)
- **TCPT** - TCP protocol traffic (upload/download)

**Key Features:**
- Configurable data transfer sizes (up to 5000 MB)
- DSCP QoS marking support
- IPv4 and IPv6 support
- Custom User-Agent support
- Connection keep-alive intervals
- Authentication support
- Traffic scheduling

---

## API Endpoints

### 1. Configure Client Operations
**POST** `/device/traffic/client/config`

Create or update client traffic configuration.

### 2. Get Configuration
**GET** `/device/traffic/client/profile/get`

Retrieve current client operation configuration.

### 3. Restart Client Operations
**POST** `/device/traffic/client/restart`

Restart all client traffic sessions.

### 4. Stop Client Operations
**GET** `/device/traffic/client/stop`

Stop all active client traffic sessions.

### 5. Delete Configuration
**DELETE** `/device/traffic/client/delete`

Delete all client traffic configurations.

---

## Request Structure

```json
{
  "status": "enable",
  "clientoperation": [
    {
      "status": "enable",
      "traffictype": "HTTPS",
      "host": "https://example.com/largefile.bin",
      "operation": "GET",
      "interval": 120,
      "datasize": 500,
      "network": "IPv4",
      "dscpvalue": 46,
      "interface": "",
      "port": "",
      "filename": "",
      "connectioninterval": 0,
      "packetsize": 0,
      "username": "",
      "password": "",
      "useragent": "",
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
| `clientoperation` | array | ✅ **YES** | Array of client operation objects | List of client traffic sessions |

**Validation:**
- ❌ **Missing/Invalid `status`**: "traffic status field is missing or status value is not from enable or disable"

---

### Client Operation Object Fields

| Field | Type | Required | Valid Values | Constraints | Default |
|-------|------|----------|--------------|-------------|---------|
| `status` | string | ✅ **YES** | `enable`, `disable` | Session control | - |
| `traffictype` | string | ✅ **YES** | `HTTP`, `HTTPS`, `QUICT`, `TCPT` | Protocol type | - |
| `host` | string | ✅ **YES** | See host rules | Format depends on traffic type | - |
| `operation` | string | ✅ **YES** | See operation rules | Depends on traffic type | - |
| `interval` | integer | ✅ **YES** | > 60 (seconds) | Time between operations | - |
| `datasize` | integer | ❌ No | 0 - 5000 (MB) | Data transfer size | `0` |
| `network` | string | ❌ No | `IPv4`, `IPv6` | IP version | Auto-detected |
| `dscpvalue` | integer | ❌ No | 0 - 63 | QoS marking | `0` |
| `port` | string | ⚠️ **COND** | `"1"` - `"65535"` | **Required for QUICT/TCPT** | - |
| `interface` | string | ❌ No | Interface name | Network interface | System default |
| `connectioninterval` | integer | ❌ No | Positive integer | Keep connection open (sec) | `0` |
| `filename` | string | ❌ No | Any string | Specific filename | Auto-generated |
| `packetsize` | integer | ❌ No | Positive integer | Packet size (bytes) | `0` |
| `username` | string | ❌ No | Any string | Authentication username | - |
| `password` | string | ❌ No | Any string | Authentication password | - |
| `useragent` | string | ❌ No | Max 256 chars | HTTP User-Agent header | - |
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
VALID VALUES: "HTTP" | "HTTPS" | "QUICT" | "TCPT"
ERROR: "traffictype field value should be [HTTP/HTTPS/QUICT/TCPT]"

CASE SENSITIVE: YES (must be uppercase)
  ✅ Correct: "HTTP", "HTTPS", "QUICT", "TCPT"
  ❌ Wrong: "http", "https", "quict", "tcpt"
```

### 3. `host`
```
REQUIRED: YES
TYPE: string
VALID FORMAT: Depends on traffictype

HTTP/HTTPS:
  MUST be full URL with scheme
  Examples:
    ✅ "https://example.com/file.zip"
    ✅ "http://192.168.1.100:8080/data.bin"
    ❌ "example.com" (missing scheme)
    ❌ "192.168.1.100" (missing scheme)

QUICT/TCPT:
  MUST be IP address or hostname (NOT URL)
  Examples:
    ✅ "192.168.1.100"
    ✅ "server.example.com"
    ✅ "2001:db8::1"
    ❌ "https://example.com" (do not include scheme)
    ❌ "http://192.168.1.100" (do not include scheme)

ERROR (HTTP/HTTPS): "[Remote:{ip}]host value: {host} not valid URL"
ERROR (QUICT/TCPT): "host field value is invalid IPv4/IPv6 or hostname address"
```

### 4. `operation`
```
REQUIRED: YES
TYPE: string
VALID VALUES: Depends on traffictype

HTTP/HTTPS:
  VALID: "GET" | "POST" | "PUT"
  ERROR: "operation field value should be [ GET/POST/PUT ]"

QUICT/TCPT:
  VALID: "upload" | "download"
  ERROR: "QUICT operation field value should be [upload/download]"
  ERROR: "TCPT operation field value should be [upload/download]"
```

### 5. `interval`
```
REQUIRED: YES
TYPE: integer
VALID RANGE: > 60 (seconds)
MINIMUM: 61 seconds
ERROR: "client operation timeout value has to be more than 60 seconds"

⚠️ CRITICAL CONSTRAINT: MUST be greater than 60 seconds
```

### 6. `datasize`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: 0 - 5000 (megabytes)
DEFAULT: 0
ERROR: "datasize field value can not be greater than 5000"

DESCRIPTION:
  Amount of data to transfer
  - For download: Data to receive
  - For upload: Data to send
  - 0 means use default or minimal transfer
```

### 7. `network`
```
REQUIRED: NO (recommended when host is IP address)
TYPE: string
VALID VALUES: "IPv4" | "IPv6"
DEFAULT: Auto-detected from host
ERROR: "network field value should be [IPv4/IPv6]"

⚠️ IP VERSION VALIDATION:
  If both network and host (as IP) are provided, they MUST match
```

### 8. `dscpvalue`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: 0 - 63
DEFAULT: 0
ERROR: "DSCP value should be in range 0-63"

Common DSCP Values:
  0:  Best Effort (default)
  46: Expedited Forwarding (EF) - VoIP/Real-time
  34: Assured Forwarding 4 (AF41) - Video
  26: Assured Forwarding 3 (AF31) - Streaming
  10: Assured Forwarding 1 (AF11) - Bulk data
```

### 9. `port` (QUICT/TCPT Only)
```
REQUIRED: YES for QUICT and TCPT, NO for HTTP/HTTPS
TYPE: string (numeric string)
VALID RANGE: "1" - "65535"

⚠️ CRITICAL:
  - Port is REQUIRED for QUICT and TCPT
  - Port is IGNORED for HTTP/HTTPS (uses port from URL)
  - Must be string format: "8080" not 8080

ERROR (QUICT): "port field is required for QUICT traffic"
ERROR (TCPT): "port field is required for TCPT traffic"
ERROR (invalid): "port field value must be a valid port number (0-65535)"
```

### 10. `interface`
```
REQUIRED: NO
TYPE: string
VALID VALUES: Network interface name
DEFAULT: System default

EXAMPLES:
  - macOS: "en0", "en1"
  - Linux: "eth0", "wlan0"
  - Windows: "Wi-Fi", "Ethernet"
```

### 11. `connectioninterval`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: Any positive integer
DEFAULT: 0
UNIT: Seconds

DESCRIPTION:
  Keep connection open for this many seconds after transfer completes
  Used for persistent HTTP connections or long-lived TCP connections
```

### 12. `filename`
```
REQUIRED: NO
TYPE: string
DEFAULT: Auto-generated

DESCRIPTION:
  - For download: Name to save file as
  - For upload: Name of file to upload
```

### 13. `packetsize`
```
REQUIRED: NO
TYPE: integer
VALID RANGE: Any positive integer
DEFAULT: 0 (uses system default)

DESCRIPTION: Controls packet size for network optimization
```

### 14. `username` and `password`
```
REQUIRED: NO
TYPE: string

DESCRIPTION:
  HTTP/HTTPS: Basic authentication credentials
  QUICT/TCPT: Protocol-specific authentication (if supported)
```

### 15. `useragent`
```
REQUIRED: NO
TYPE: string
VALID LENGTH: Maximum 256 characters
DEFAULT: System default user agent
ERROR: "UserAgent field value is too long. It should be less than 256 characters."

DESCRIPTION:
  HTTP/HTTPS User-Agent header
  Only applies to HTTP/HTTPS traffic
  Ignored for QUICT/TCPT

EXAMPLES:
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
  "WiFiAgent/4.4 (Automated Testing)"
  "CustomClient/1.0"
```

### 16. `payload`
```
REQUIRED: NO
TYPE: string

DESCRIPTION: Custom payload data (use case dependent)
```

### 17. `traffic_schedule`
```
REQUIRED: NO
TYPE: object
DEFAULT: {} (24/7 operation)

See "Traffic Schedule Validation" section for details
```

---

## Traffic Type Specific Rules

### 🌐 HTTP (Unencrypted HTTP Traffic)

**Protocol:** HTTP/1.1
**Default Port:** 80 (from URL)
**Encryption:** None

**Required Fields:**
- ✅ `status` = `"enable"` or `"disable"`
- ✅ `traffictype` = `"HTTP"`
- ✅ `host` = Full HTTP URL (MUST start with `http://`)
- ✅ `operation` = `"GET"`, `"POST"`, or `"PUT"`
- ✅ `interval` > 60 seconds

**Optional Fields:**
- `datasize` (0-5000 MB)
- `network` (`IPv4`/`IPv6`)
- `dscpvalue` (0-63)
- `interface`
- `connectioninterval`
- `filename`
- `username` (Basic Auth)
- `password` (Basic Auth)
- `useragent` (max 256 chars)
- `payload`
- `traffic_schedule`

**NOT Used:**
- ❌ `port` (taken from URL)
- ❌ `packetsize` (HTTP protocol managed)

**Validation:**

```javascript
if (traffictype === "HTTP") {
  // URL must be valid
  if (!isValidURL(host)) {
    ERROR: "[Remote:{ip}]host value: {host} not valid URL"
  }

  // URL scheme must be http
  if (url.scheme !== "http") {
    ERROR: "[Remote:{ip}]provided URL scheme is not HTTP"
  }

  // Operation must be GET/POST/PUT
  if (!["GET", "POST", "PUT"].includes(operation)) {
    ERROR: "operation field value should be [ GET/POST/PUT ]"
  }

  // If network is specified and URL contains IP, they must match
  if (network === "IPv6" && isIPv4(url.hostname)) {
    ERROR: "[Remote:{ip}]provided URL: {url} is IPv4 but network is IPv6"
  }
  if (network === "IPv4" && isIPv6(url.hostname)) {
    ERROR: "[Remote:{ip}]provided URL: {url} is IPv6 but network is IPv4"
  }
}
```

**Example:**
```json
{
  "status": "enable",
  "traffictype": "HTTP",
  "host": "http://example.com/api/data",
  "operation": "GET",
  "interval": 120,
  "datasize": 100,
  "network": "IPv4",
  "useragent": "WiFiAgent/4.4",
  "username": "apiuser",
  "password": "apipass"
}
```

---

### 🔒 HTTPS (Encrypted HTTPS Traffic)

**Protocol:** HTTPS (HTTP over TLS)
**Default Port:** 443 (from URL)
**Encryption:** TLS/SSL

**Required Fields:**
- ✅ `status` = `"enable"` or `"disable"`
- ✅ `traffictype` = `"HTTPS"`
- ✅ `host` = Full HTTPS URL (MUST start with `https://`)
- ✅ `operation` = `"GET"`, `"POST"`, or `"PUT"`
- ✅ `interval` > 60 seconds

**Optional Fields:**
- Same as HTTP (see above)

**Validation:**

```javascript
if (traffictype === "HTTPS") {
  // URL must be valid
  if (!isValidURL(host)) {
    ERROR: "[Remote:{ip}]host value: {host} not valid URL"
  }

  // URL scheme must be https
  if (url.scheme !== "https") {
    ERROR: "[Remote:{ip}]provided URL scheme is not HTTPS"
  }

  // Operation must be GET/POST/PUT
  if (!["GET", "POST", "PUT"].includes(operation)) {
    ERROR: "operation field value should be [ GET/POST/PUT ]"
  }

  // Network/IP version validation (same as HTTP)
  if (network === "IPv6" && isIPv4(url.hostname)) {
    ERROR: "[Remote:{ip}]provided URL: {url} is IPv4 but network is IPv6"
  }
}
```

**Example:**
```json
{
  "status": "enable",
  "traffictype": "HTTPS",
  "host": "https://cdn.example.com/files/largefile.zip",
  "operation": "GET",
  "interval": 300,
  "datasize": 2000,
  "network": "IPv4",
  "dscpvalue": 46,
  "useragent": "Mozilla/5.0"
}
```

---

### ⚡ QUICT (QUIC Protocol Traffic)

**Protocol:** QUIC (Quick UDP Internet Connections)
**Transport:** UDP
**Encryption:** Built-in

**Required Fields:**
- ✅ `status` = `"enable"` or `"disable"`
- ✅ `traffictype` = `"QUICT"`
- ✅ `host` = IP address or hostname (NOT URL)
- ✅ `port` = Port number as string (e.g., `"4433"`)
- ✅ `operation` = `"upload"` or `"download"`
- ✅ `interval` > 60 seconds

**Optional Fields:**
- `datasize` (0-5000 MB)
- `network` (`IPv4`/`IPv6`)
- `dscpvalue` (0-63)
- `interface`
- `connectioninterval`
- `filename`
- `packetsize`
- `username`
- `password`
- `payload`
- `traffic_schedule`

**NOT Used:**
- ❌ `useragent` (not applicable to QUIC)

**Validation:**

```javascript
if (traffictype === "QUICT") {
  // Host must be IP or hostname (not URL)
  if (!isIP(host) && !isValidHostname(host)) {
    ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
  }

  // Port is REQUIRED
  if (port === "") {
    ERROR: "port field is required for QUICT traffic"
  }

  // Port must be valid
  if (!isValidPort(port)) {
    ERROR: "port field value must be a valid port number (0-65535)"
  }

  // Operation must be upload/download
  if (!["upload", "download"].includes(operation)) {
    ERROR: "QUICT operation field value should be [upload/download]"
  }

  // Network/IP version validation
  if (isIP(host)) {
    if (network === "IPv6" && !isIPv6(host)) {
      ERROR: "host field value is not IPv6"
    }
    if (network === "IPv4" && !isIPv4(host)) {
      ERROR: "host field value is not IPv4"
    }
  }
}
```

**Example:**
```json
{
  "status": "enable",
  "traffictype": "QUICT",
  "host": "192.168.1.200",
  "port": "4433",
  "operation": "upload",
  "interval": 90,
  "datasize": 250,
  "network": "IPv4",
  "dscpvalue": 34
}
```

---

### 📡 TCPT (TCP Protocol Traffic)

**Protocol:** TCP (Transmission Control Protocol)
**Transport:** TCP
**Encryption:** None (plain TCP)

**Required Fields:**
- ✅ `status` = `"enable"` or `"disable"`
- ✅ `traffictype` = `"TCPT"`
- ✅ `host` = IP address or hostname (NOT URL)
- ✅ `port` = Port number as string (e.g., `"8080"`)
- ✅ `operation` = `"upload"` or `"download"`
- ✅ `interval` > 60 seconds

**Optional Fields:**
- Same as QUICT (see above)

**Validation:**

```javascript
if (traffictype === "TCPT") {
  // Host must be IP or hostname (not URL)
  if (!isIP(host) && !isValidHostname(host)) {
    ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
  }

  // Port is REQUIRED
  if (port === "") {
    ERROR: "port field is required for TCPT traffic"
  }

  // Port must be valid
  if (!isValidPort(port)) {
    ERROR: "port field value must be a valid port number (0-65535)"
  }

  // Operation must be upload/download
  if (!["upload", "download"].includes(operation)) {
    ERROR: "TCPT operation field value should be [upload/download]"
  }

  // Network/IP version validation (same as QUICT)
  if (isIP(host)) {
    if (network === "IPv6" && !isIPv6(host)) {
      ERROR: "host field value is not IPv6"
    }
    if (network === "IPv4" && !isIPv4(host)) {
      ERROR: "host field value is not IPv4"
    }
  }
}
```

**Example:**
```json
{
  "status": "enable",
  "traffictype": "TCPT",
  "host": "10.0.0.50",
  "port": "8080",
  "operation": "download",
  "interval": 150,
  "datasize": 1000,
  "network": "IPv4",
  "connectioninterval": 60
}
```

---

## Host Format Requirements

### Critical Rules by Traffic Type

| Traffic Type | Host Format | Port Specified | URL Scheme Required | Example |
|--------------|-------------|----------------|---------------------|---------|
| **HTTP** | Full URL | ❌ In URL | ✅ `http://` | `http://example.com/file` |
| **HTTPS** | Full URL | ❌ In URL | ✅ `https://` | `https://example.com/file` |
| **QUICT** | IP or hostname | ✅ Separate field | ❌ No scheme | `192.168.1.100` |
| **TCPT** | IP or hostname | ✅ Separate field | ❌ No scheme | `server.local` |

### Host Validation Logic

```javascript
function validateHost(traffictype, host) {
  if (traffictype === "HTTP" || traffictype === "HTTPS") {
    // Must be valid URL
    const url = parseURL(host);
    if (!url) {
      ERROR: "[Remote:{ip}]host value: {host} not valid URL"
    }

    // Scheme must match traffic type
    if (traffictype === "HTTP" && url.scheme !== "http") {
      ERROR: "[Remote:{ip}]provided URL scheme is not HTTP"
    }
    if (traffictype === "HTTPS" && url.scheme !== "https") {
      ERROR: "[Remote:{ip}]provided URL scheme is not HTTPS"
    }

    // Validate hostname is valid
    if (!isIP(url.hostname) && !isValidHostname(url.hostname)) {
      ERROR: "[Remote:{ip}]provided URL might have invalid hostname: {hostname}"
    }

  } else if (traffictype === "QUICT" || traffictype === "TCPT") {
    // Must be IP or hostname (NOT URL)
    if (!isIP(host) && !isValidHostname(host)) {
      ERROR: "host field value is invalid IPv4/IPv6 or hostname address"
    }
  }
}
```

### Examples

**✅ Valid HTTP/HTTPS Hosts:**
```
"http://example.com/api/data"
"https://cdn.example.com/files/video.mp4"
"http://192.168.1.100:8080/download"
"https://[2001:db8::1]:443/api"
```

**✅ Valid QUICT/TCPT Hosts:**
```
"192.168.1.100"
"10.0.0.1"
"2001:db8::1"
"fe80::1"
"server.example.com"
"ftp.local"
```

**❌ Invalid Examples:**
```
// HTTP/HTTPS with missing scheme
"example.com/file"          // ERROR: missing http:// or https://
"192.168.1.100/api"         // ERROR: missing scheme

// HTTP with HTTPS scheme
{"traffictype": "HTTP", "host": "https://example.com"}
// ERROR: provided URL scheme is not HTTP

// HTTPS with HTTP scheme
{"traffictype": "HTTPS", "host": "http://example.com"}
// ERROR: provided URL scheme is not HTTPS

// QUICT/TCPT with URL
{"traffictype": "QUICT", "host": "http://192.168.1.100"}
// ERROR: host should be IP/hostname, not URL
```

---

## Network and IP Version Validation

### IP Version Consistency Rules

The agent validates that `host` and `network` fields are consistent.

**For HTTP/HTTPS (URL-based):**
```javascript
if ((traffictype === "HTTP" || traffictype === "HTTPS") && network !== "") {
  const url = parseURL(host);
  const hostname = url.hostname;

  if (isIPv4(hostname) && network === "IPv6") {
    ERROR: "[Remote:{ip}]provided URL: {url} is IPv4 but network is IPv6"
  }

  if (isIPv6(hostname) && network === "IPv4") {
    ERROR: "[Remote:{ip}]provided URL: {url} is IPv6 but network is IPv4"
  }
}
```

**For QUICT/TCPT (IP/hostname-based):**
```javascript
if ((traffictype === "QUICT" || traffictype === "TCPT") && network !== "") {
  if (isIP(host)) {
    if (network === "IPv6" && !isIPv6(host)) {
      ERROR: "host field value is not IPv6"
    }
    if (network === "IPv4" && !isIPv4(host)) {
      ERROR: "host field value is not IPv4"
    }
  }
}
```

### Validation Matrix

| Traffic Type | Host Value | Network Value | Validation | Error |
|--------------|------------|---------------|------------|-------|
| HTTP/HTTPS | `http://192.168.1.1/` | `IPv4` | ✅ PASS | - |
| HTTP/HTTPS | `http://192.168.1.1/` | `IPv6` | ❌ FAIL | "provided URL: ... is IPv4 but network is IPv6" |
| HTTP/HTTPS | `https://example.com/` | `IPv4` | ✅ PASS | Assumes DNS resolves to IPv4 |
| QUICT/TCPT | `192.168.1.1` | `IPv4` | ✅ PASS | - |
| QUICT/TCPT | `192.168.1.1` | `IPv6` | ❌ FAIL | "host field value is not IPv6" |
| QUICT/TCPT | `server.com` | `IPv4` | ✅ PASS | Assumes DNS resolves to IPv4 |
| QUICT/TCPT | `server.com` | (empty) | ✅ PASS | Uses system DNS |

---

## Traffic Schedule Validation

Same as other traffic types.

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

### Default Behavior

```javascript
if (traffic_schedule === {} || traffic_schedule === null) {
  traffic_schedule.default = true;  // 24/7 operation
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
  "message": "client operation timeout value has to be more than 60 seconds"
}
```

---

## Error Messages

### Complete Error Reference

| Validation | Error Message |
|------------|---------------|
| **Global Status** | "traffic status field is missing or status value is not from enable or disable" |
| **Session Status** | "status field value should be [enable/disable]" |
| **Traffic Type** | "traffictype field value should be [HTTP/HTTPS/QUICT/TCPT]" |
| **Data Size Exceeded** | "datasize field value can not be greater than 5000" |
| **Interval Too Low** | "client operation timeout value has to be more than 60 seconds" |
| **Network Invalid** | "network field value should be [IPv4/IPv6]" |
| **DSCP Range** | "DSCP value should be in range 0-63" |
| **User Agent Too Long** | "UserAgent field value is too long. It should be less than 256 characters." |
| **HTTP/HTTPS: Invalid URL** | "[Remote:{ip}]host value: {host} not valid URL" |
| **HTTP/HTTPS: IPv4/IPv6 Mismatch** | "[Remote:{ip}]provided URL: {url} is IPv4 but network is IPv6" |
| **HTTP/HTTPS: Invalid Hostname** | "[Remote:{ip}]provided URL might have invalid hostname: {hostname}" |
| **HTTP/HTTPS: Wrong Scheme (HTTP)** | "[Remote:{ip}]provided URL scheme is not HTTP" |
| **HTTP/HTTPS: Wrong Scheme (HTTPS)** | "[Remote:{ip}]provided URL scheme is not HTTPS" |
| **HTTP/HTTPS: Operation** | "operation field value should be [ GET/POST/PUT ]" |
| **QUICT: Invalid Host** | "host field value is invalid IPv4/IPv6 or hostname address" |
| **QUICT: IPv6 Mismatch** | "host field value is not IPv6" |
| **QUICT: IPv4 Mismatch** | "host field value is not IPv4" |
| **QUICT: Operation** | "QUICT operation field value should be [upload/download]" |
| **QUICT: Missing Port** | "port field is required for QUICT traffic" |
| **QUICT: Invalid Port** | "port field value must be a valid port number (0-65535)" |
| **TCPT: Invalid Host** | "host field value is invalid IPv4/IPv6 or hostname address" |
| **TCPT: IPv6 Mismatch** | "host field value is not IPv6" |
| **TCPT: IPv4 Mismatch** | "host field value is not IPv4" |
| **TCPT: Operation** | "TCPT operation field value should be [upload/download]" |
| **TCPT: Missing Port** | "port field is required for TCPT traffic" |
| **TCPT: Invalid Port** | "port field value must be a valid port number (0-65535)" |
| **Server Busy** | "Another API call is in progress. Please wait." |

---

## Complete Examples

### Example 1: HTTPS GET Request

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://example.com/files/largefile.zip",
        "operation": "GET",
        "interval": 120,
        "datasize": 500,
        "network": "IPv4",
        "dscpvalue": 46
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

### Example 2: HTTP POST with Authentication

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTP",
        "host": "http://api.example.com/upload",
        "operation": "POST",
        "interval": 180,
        "datasize": 100,
        "network": "IPv4",
        "username": "apiuser",
        "password": "apipass",
        "useragent": "WiFiAgent/4.4 (Testing)"
      }
    ]
  }'
```

---

### Example 3: QUIC Upload Traffic

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "QUICT",
        "host": "192.168.1.200",
        "port": "4433",
        "operation": "upload",
        "interval": 90,
        "datasize": 250,
        "network": "IPv4",
        "dscpvalue": 34
      }
    ]
  }'
```

---

### Example 4: TCP Download (IPv6)

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "TCPT",
        "host": "2001:db8::1",
        "port": "8080",
        "operation": "download",
        "interval": 150,
        "datasize": 1000,
        "network": "IPv6",
        "connectioninterval": 60
      }
    ]
  }'
```

---

### Example 5: Multiple Client Operations with Schedule

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://cdn.example.com/video.mp4",
        "operation": "GET",
        "interval": 300,
        "datasize": 2000,
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
        "traffictype": "QUICT",
        "host": "192.168.1.150",
        "port": "4433",
        "operation": "upload",
        "interval": 180,
        "datasize": 500,
        "network": "IPv4",
        "dscpvalue": 26,
        "traffic_schedule": {
          "weekdays": ["Saturday", "Sunday"],
          "starthour": 0,
          "endhour": 23
        }
      }
    ]
  }'
```

---

### Example 6: Validation Error - Interval Too Low

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://example.com/file",
        "operation": "GET",
        "interval": 30
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "client operation timeout value has to be more than 60 seconds"
}
```

---

### Example 7: Validation Error - Wrong URL Scheme

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "http://example.com/file",
        "operation": "GET",
        "interval": 120
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "[Remote:127.0.0.1:54321]provided URL scheme is not HTTPS"
}
```

---

### Example 8: Validation Error - Missing Port for QUICT

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "QUICT",
        "host": "192.168.1.200",
        "operation": "upload",
        "interval": 90
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "port field is required for QUICT traffic"
}
```

---

### Example 9: Validation Error - IP Version Mismatch

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://192.168.1.100/file",
        "operation": "GET",
        "interval": 120,
        "network": "IPv6"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "[Remote:127.0.0.1:54321]provided URL: https://192.168.1.100/file is IPv4 but network is IPv6"
}
```

---

### Example 10: Validation Error - Data Size Exceeded

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://example.com/hugefile",
        "operation": "GET",
        "interval": 120,
        "datasize": 6000
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "datasize field value can not be greater than 5000"
}
```

---

### Example 11: Validation Error - User Agent Too Long

**Request:**
```bash
curl -X POST http://192.168.1.100:8083/device/traffic/client/config \
  -H "Content-Type: application/json" \
  -d '{
    "status": "enable",
    "clientoperation": [
      {
        "status": "enable",
        "traffictype": "HTTPS",
        "host": "https://example.com/file",
        "operation": "GET",
        "interval": 120,
        "useragent": "' + 'A'.repeat(300) + '"
      }
    ]
  }'
```

**Response:**
```json
{
  "status": "error",
  "message": "UserAgent field value is too long. It should be less than 256 characters."
}
```

---

## Summary of Critical Validations

### ✅ Always Required (All Traffic Types)
- `status` (global and per-session)
- `traffictype`
- `host` (format depends on traffic type)
- `operation` (values depend on traffic type)
- `interval` (MUST be > 60 seconds)

### ⚠️ Traffic Type-Specific Requirements

| Protocol | Host Format | Port Required | Operation Values | URL Scheme Required |
|----------|-------------|---------------|------------------|---------------------|
| **HTTP** | Full URL | ❌ (in URL) | GET, POST, PUT | ✅ `http://` |
| **HTTPS** | Full URL | ❌ (in URL) | GET, POST, PUT | ✅ `https://` |
| **QUICT** | IP/hostname | ✅ **YES** | upload, download | ❌ No scheme |
| **TCPT** | IP/hostname | ✅ **YES** | upload, download | ❌ No scheme |

### 🔢 Numeric Constraints

| Field | Min | Max | Default |
|-------|-----|-----|---------|
| `interval` | 61 sec | No limit | Required |
| `datasize` | 0 MB | 5000 MB | 0 MB |
| `dscpvalue` | 0 | 63 | 0 |
| `port` | 1 | 65535 | Required for QUICT/TCPT |
| `useragent` length | 0 chars | 256 chars | System default |

### 🌐 IP Validation Rules

- ✅ If `host` is IP + `network` specified → MUST match
- ✅ If `host` is URL with IP + `network` specified → MUST match
- ✅ If `host` is hostname → `network` is optional hint
- ✅ `network` is optional but recommended for IP-based hosts

### 🔄 Default Behavior

- Empty `traffic_schedule` → Runs 24/7
- Omitted `datasize` → 0 (minimal transfer)
- Omitted `dscpvalue` → 0 (best effort)
- Omitted `connectioninterval` → 0 (close after transfer)
- Omitted `useragent` → System default
- Omitted `network` → Auto-detected

---

**End of Client Traffic API Documentation**