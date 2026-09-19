#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================================
# CONFIGURATION
# ============================================================================

AP_SSH_USER = "root"
REMOTE_DIR = "/root/3027"

# ---------------------------------------------------------------------------
# Wifi OTP signing endpoint used by the arista-ssh-agent Response[...] challenge/
# response prompt (same as SWAT's otpLib.getWifiOTP(), and qwrap-manager.py).
# ---------------------------------------------------------------------------
WIFI_OTP_URL = "https://license.aristanetworks.com/sign/wifi-otp/"
WIFI_OTP_KEY = "d9ca932a4bc8fbaaa5021b00e14dd453d469eaaf"


# ============================================================================
# WIFI OTP
# ============================================================================

def get_response(challenge):
    """
    Sign an arista-ssh-agent Response[...] challenge via the Wifi OTP
    endpoint. Stateless (no session/login required).
    """

    print(f"   [otp] Fetching response for challenge: {challenge}")

    header = {
        "Authorization": f"Token {WIFI_OTP_KEY}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({"message": challenge})

    try:
        response = requests.post(WIFI_OTP_URL, headers=header, data=payload, allow_redirects=True, timeout=30)
    except requests.RequestException as e:
        print(f"[ERROR] Wifi OTP request failed: {e}")
        return ""

    if response.status_code != 201:
        print(f"[ERROR] Wifi OTP request failed ({response.status_code}) for challenge {challenge}")
        return ""

    signature = response.json().get("signature", "")

    if not signature:
        print("[ERROR] Wifi OTP response did not contain 'signature'.")
        return ""

    return signature


# ============================================================================
# GET AP SSH CHALLENGE
# ============================================================================

def get_challenge_from_ap(ap_ip):
    """
    SSH to the AP as root and extract the challenge:

        Response[XXXXXXXX]:

    from the AP authentication prompt.
    """

    print(f"   [ssh] Connecting to root@{ap_ip}...")

    # NOTE: log_user is set to 1 here to display SSH errors to the terminal.
    # Legacy SSH ciphers are added to handle older AP firmware.
    expect_script = f"""
set timeout 15
log_user 1

spawn ssh \\
    -o StrictHostKeyChecking=no \\
    -o UserKnownHostsFile=/dev/null \\
    -o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1 \\
    -o HostKeyAlgorithms=+ssh-rsa \\
    -o PubkeyAcceptedKeyTypes=+ssh-rsa \\
    root@{ap_ip}

expect {{
    -re {{Response\\[([^\\]]+)\\]:}} {{
        log_user 0
        puts "\\n$expect_out(1,string)"
        exit 0
    }}

    timeout {{
        exit 1
    }}

    eof {{
        exit 1
    }}
}}
"""

    try:

        result = subprocess.run(
            [
                "/usr/bin/expect",
                "-c",
                expect_script
            ],
            capture_output=True,
            text=True,
            timeout=20
        )

    except FileNotFoundError:
        print("[ERROR] /usr/bin/expect was not found.")
        print("        Please install the 'expect' package.")
        return None

    except subprocess.TimeoutExpired:
        print(f"[ERROR] SSH probe timed out for {ap_ip}.")
        return None

    output = result.stdout + result.stderr

    # Primary parser
    match = re.search(
        r"Response\[([^\]]+)\]",
        output
    )

    if match:
        return match.group(1)

    return None


# ============================================================================
# TCL / EXPECT ESCAPING
# ============================================================================

def escape_tcl_string(value):
    """
    Escape special characters before putting the OTP response
    into an Expect/Tcl double-quoted string.
    """

    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("[", "\\[")
    value = value.replace("]", "\\]")
    value = value.replace("$", "\\$")

    return value


# ============================================================================
# SCP ONE FILE
# ============================================================================

def scp_file_to_ap(local_path, ap_ip, response):
    """
    Copy one local file to:

        root@AP_IP:/root/<filename>

    using scp -O.

    The OTP response is supplied when the AP asks:

        Response[...]:
    """

    filename = os.path.basename(local_path)

    remote_path = f"{REMOTE_DIR}/{filename}"

    safe_response = escape_tcl_string(response)

    # Legacy ciphers are added here as well for SCP transfers.
    expect_script = f"""
set timeout 90

spawn scp -O \\
    -o StrictHostKeyChecking=no \\
    -o UserKnownHostsFile=/dev/null \\
    -o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1 \\
    -o HostKeyAlgorithms=+ssh-rsa \\
    -o PubkeyAcceptedKeyTypes=+ssh-rsa \\
    -- "{local_path}" root@{ap_ip}:{remote_path}

expect {{
    -re {{Response\\[}} {{
        send "{safe_response}\\r"
        exp_continue
    }}

    -re {{100%}} {{
        exp_continue
    }}

    eof {{
        catch wait result
        exit [lindex $result 3]
    }}

    timeout {{
        exit 1
    }}
}}
"""

    print(
        f"   -> Uploading {filename} "
        f"to root@{ap_ip}:{remote_path}"
    )

    try:

        result = subprocess.run(
            [
                "/usr/bin/expect",
                "-c",
                expect_script
            ],
            timeout=120
        )

    except subprocess.TimeoutExpired:
        print(f"      [FAIL] SCP timeout: {filename}")
        return False

    if result.returncode == 0:
        print(f"      [OK] {filename}")
        return True

    print(
        f"      [FAIL] {filename} "
        f"(return code {result.returncode})"
    )

    return False


# ============================================================================
# TRANSFER ALL FILES TO ONE AP
# ============================================================================

def transfer_to_ap(ap_ip, files):

    print()
    print("=" * 65)
    print(f"AP: {ap_ip}")
    print("=" * 65)

    # ----------------------------------------------------------------------
    # Step 1: Get AP challenge
    # ----------------------------------------------------------------------

    print("[1/3] Getting challenge from AP...")

    challenge = get_challenge_from_ap(ap_ip)

    if not challenge:
        print(
            f"\n[FAIL] Could not retrieve challenge "
            f"from AP {ap_ip}"
        )
        return False

    print(f"      Challenge: {challenge}")

    # ----------------------------------------------------------------------
    # Step 2: Get OTP response
    # ----------------------------------------------------------------------

    print("[2/3] Getting OTP response...")

    response = get_response(challenge)

    if not response:
        print(
            f"[FAIL] Could not obtain OTP response "
            f"for AP {ap_ip}"
        )
        return False

    print("      [OK] OTP response obtained.")

    # ----------------------------------------------------------------------
    # Step 3: Upload files
    # ----------------------------------------------------------------------

    print("[3/3] Uploading files to /root...")

    success = True

    for local_path in files:

        if not scp_file_to_ap(
            local_path,
            ap_ip,
            response
        ):
            success = False

    # ----------------------------------------------------------------------
    # Result
    # ----------------------------------------------------------------------

    print()

    if success:

        print(
            f"[SUCCESS] All files copied to "
            f"{ap_ip}:{REMOTE_DIR}"
        )

    else:

        print(
            f"[PARTIAL] Some files failed on "
            f"{ap_ip}"
        )

    return success


# ============================================================================
# GET FILES FROM DIRECTORY
# ============================================================================

def get_files_from_directory(directory):
    """
    Return all regular files in the specified local directory.
    """

    if not os.path.isdir(directory):

        print(
            f"[ERROR] Directory does not exist: "
            f"{directory}"
        )

        sys.exit(1)

    files = []

    for name in sorted(os.listdir(directory)):

        path = os.path.join(
            directory,
            name
        )

        if os.path.isfile(path):
            files.append(path)

    if not files:

        print(
            f"[ERROR] No files found in directory: "
            f"{directory}"
        )

        sys.exit(1)

    return files


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 65)
    print("             ARISTA AP CERTIFICATE TRANSFER")
    print("=" * 65)

    # ----------------------------------------------------------------------
    # Check Expect
    # ----------------------------------------------------------------------

    if not os.path.exists("/usr/bin/expect"):

        print(
            "[ERROR] /usr/bin/expect was not found."
        )

        print(
            "        Install Expect before running "
            "this script."
        )

        sys.exit(1)

    # ----------------------------------------------------------------------
    # Get interactive inputs
    # ----------------------------------------------------------------------

    directory = input("\nEnter the local directory containing the certificate files: ").strip()
    while not directory:
        directory = input("[ERROR] Directory cannot be empty. Please enter the local directory: ").strip()

    ap_input = input("\nEnter the AP IP addresses (separated by space or comma): ").strip()
    while not ap_input:
        ap_input = input("[ERROR] AP IPs cannot be empty. Please enter the AP IP addresses: ").strip()

    # Convert the raw input into a list of IPs, handling both spaces and commas
    ap_list = [ip.strip() for ip in ap_input.replace(',', ' ').split() if ip.strip()]

    # ----------------------------------------------------------------------
    # Get source files
    # ----------------------------------------------------------------------

    files = get_files_from_directory(directory)

    # ----------------------------------------------------------------------
    # Header
    # ----------------------------------------------------------------------

    print()
    print("=" * 65)
    print("             STARTING TRANSFER")
    print("=" * 65)

    print(
        f"Source directory : {directory}"
    )

    print(
        f"Destination      : {REMOTE_DIR}"
    )

    print(
        f"SSH user         : {AP_SSH_USER}"
    )

    print(
        f"Number of APs    : {len(ap_list)}"
    )

    print()
    print("Files to upload:")

    for file_path in files:

        print(
            f"  - {file_path}"
        )

    # ----------------------------------------------------------------------
    # Process every AP
    # ----------------------------------------------------------------------

    results = {}

    for ap_ip in ap_list:

        results[ap_ip] = transfer_to_ap(
            ap_ip,
            files
        )

    # ----------------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------------

    print()
    print("=" * 65)
    print("                         SUMMARY")
    print("=" * 65)

    for ap_ip, success in results.items():

        status = (
            "SUCCESS"
            if success
            else "FAILED"
        )

        print(
            f"{ap_ip:<25} {status}"
        )

    print()

    failed = [
        ap_ip
        for ap_ip, success in results.items()
        if not success
    ]

    if failed:

        print(
            f"[DONE] {len(failed)} AP(s) failed."
        )

        sys.exit(1)

    print(
        "[DONE] All APs processed successfully."
    )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()