#!/usr/bin/env python3

import json
import logging
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cert_scp")


# ============================================================================
# CONFIGURATION
# ============================================================================

AP_SSH_USER = "root"

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


def extract_common_name_from_cert(cert_path):
    """
    Extract the subject commonName (CN) from a PEM-encoded certificate.
    """

    cert_path = os.path.expanduser(cert_path)

    try:
        certificate = ssl._ssl._test_decode_cert(cert_path)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RuntimeError(
            f"Could not read a PEM certificate from {cert_path}: {exc}"
        ) from exc

    for attribute_group in certificate.get("subject", ()):
        for attribute, value in attribute_group:
            if attribute == "commonName":
                return value

    raise RuntimeError(
        f"No commonName (CN) found in certificate {cert_path}"
    )


def get_client_certificate_from_directory(directory):
    """
    Return the standard client.pem path from a local certificate directory.
    """

    client_cert = os.path.join(directory, "client.pem")

    if not os.path.isfile(client_cert):
        raise RuntimeError(
            f"Could not find client.pem in certificate directory: {directory}"
        )

    return client_cert


def create_remote_directory(ap_ip, remote_dir):
    """
    Authenticate to the AP and create the remote directory with mkdir -p.
    """

    print(f"   [ssh] Creating remote directory {remote_dir}...")

    challenge = get_challenge_from_ap(ap_ip)
    if not challenge:
        print(f"[ERROR] Could not retrieve challenge from AP {ap_ip}.")
        return False

    response = get_response(challenge)
    if not response:
        print(f"[ERROR] Could not obtain OTP response for AP {ap_ip}.")
        return False

    safe_response = escape_tcl_string(response)
    remote_command = f"mkdir -p -- {shlex.quote(remote_dir)}"
    safe_command = escape_tcl_string(remote_command)

    expect_script = f"""
set timeout 30

spawn ssh \\
    -o StrictHostKeyChecking=no \\
    -o UserKnownHostsFile=/dev/null \\
    -o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1 \\
    -o HostKeyAlgorithms=+ssh-rsa \\
    -o PubkeyAcceptedKeyTypes=+ssh-rsa \\
    root@{ap_ip} \"{safe_command}\"

expect {{
    -re {{Response\\[}} {{
        send \"{safe_response}\\r\"
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

    try:
        result = subprocess.run(
            [
                "/usr/bin/expect",
                "-c",
                expect_script
            ],
            timeout=45
        )
    except subprocess.TimeoutExpired:
        print(f"[ERROR] mkdir timed out on AP {ap_ip}.")
        return False

    if result.returncode != 0:
        print(
            f"[ERROR] Could not create {remote_dir} on AP {ap_ip} "
            f"(return code {result.returncode})."
        )
        return False

    return True


# ============================================================================
# SCP FILES (PARALLEL, VIA SSHPASS)
# ============================================================================
#
# Uses sshpass (lighter than spawning an expect/Tcl interpreter per file) and
# fires all transfers to an AP in parallel instead of waiting on them one at
# a time.

def scp_files_to_ap(files, ap_ip, response, remote_dir):
    """
    Copy all local files to:

        root@AP_IP:<remote_dir>/<filename>

    using scp -O, in parallel, via sshpass.

    The OTP response is supplied when the AP asks:

        Response[...]:
    """

    if shutil.which("sshpass") is None:
        print(
            "[ERROR] sshpass is required for SCP mode. "
            "Install it with: brew install sshpass"
        )
        return False

    env = os.environ.copy()
    env["SSHPASS"] = response

    # sshpass -P matches on the leading text of the prompt; "Response[" is
    # the common prefix regardless of the per-AP challenge/response value.
    prompt_search = "Response["

    procs = []

    for local_path in files:

        filename = os.path.basename(local_path)
        remote_path = f"{remote_dir.rstrip('/')}/{filename}"

        print(
            f"   -> Uploading {filename} "
            f"to root@{ap_ip}:{remote_path}"
        )

        cmd = [
            "sshpass", "-e", "-P", prompt_search,
            "scp", "-O",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
            "--", local_path, f"root@{ap_ip}:{remote_path}",
        ]

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        procs.append((filename, proc))

    success = True

    for filename, proc in procs:

        try:
            returncode = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"      [FAIL] SCP timeout: {filename}")
            success = False
            continue

        if returncode == 0:
            print(f"      [OK] {filename}")
        else:
            print(f"      [FAIL] {filename} (return code {returncode})")
            success = False

    return success


# ============================================================================
# CONCURRENT AP RUNNER
# ============================================================================

def runConcurrently(func, apList: list, actionName: str) -> dict:
    '''Run func(ip) concurrently across apList (one worker per AP), same
    pattern as qwrap-manager.py's runConcurrently(). func is expected to
    return a truthy/falsy success result (not raise) for normal pass/fail;
    unexpected exceptions are also caught and counted as failures.'''
    errors = []
    results = {}
    with ThreadPoolExecutor(max_workers=len(apList)) as executor:
        futureToHost = {executor.submit(func, ip): ip for ip in apList}
        for future in as_completed(futureToHost):
            host = futureToHost[future]
            try:
                result = future.result()
                results[host] = result
                if result:
                    log.info(f'{host}: {actionName} succeeded')
                else:
                    log.error(f'{host}: {actionName} FAILED')
                    errors.append(host)
            except Exception as e:
                log.error(f'{host}: {actionName} FAILED: {e}')
                results[host] = False
                errors.append(host)

    if errors:
        log.error(f'{actionName} failed on: {errors}')
    return results


# ============================================================================
# TRANSFER ALL FILES TO ONE AP
# ============================================================================

def transfer_to_ap(ap_ip, files, remote_dir, create_remote_dir=False):

    print()
    print("=" * 65)
    print(f"AP: {ap_ip}")
    print("=" * 65)

    if create_remote_dir:
        print("[PREP] Creating CN-based destination directory...")
        if not create_remote_directory(ap_ip, remote_dir):
            return False

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

    print(f"[3/3] Uploading files to {remote_dir}...")

    success = scp_files_to_ap(
        files,
        ap_ip,
        response,
        remote_dir
    )

    # ----------------------------------------------------------------------
    # Result
    # ----------------------------------------------------------------------

    print()

    if success:

        print(
            f"[SUCCESS] All files copied to "
            f"{ap_ip}:{remote_dir}"
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

    directory = os.path.expanduser(
        input("\nEnter the local directory containing the certificate files: ").strip()
    )
    while not directory:
        directory = os.path.expanduser(
            input("[ERROR] Directory cannot be empty. Please enter the local directory: ").strip()
        )

    remote_dir_input = input(
        "\nEnter the remote destination directory "
        "(leave blank to derive /root/<CN>): "
    ).strip()
    create_remote_dir = not remote_dir_input

    if create_remote_dir:
        try:
            client_cert = get_client_certificate_from_directory(directory)
            common_name = extract_common_name_from_cert(client_cert)
        except RuntimeError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

        if not re.fullmatch(r"[A-Za-z0-9._@-]+", common_name):
            print(
                "[ERROR] The certificate CN contains characters that cannot "
                f"be safely used as a directory name: {common_name!r}"
            )
            sys.exit(1)

        remote_dir = f"/root/{common_name}"
        print(
            f"   [cert] Derived CN {common_name!r}; "
            f"using remote directory {remote_dir}"
        )
    else:
        remote_dir = remote_dir_input
    if not remote_dir.startswith('/'):
        remote_dir = f"/{remote_dir}"

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
        f"Destination      : {remote_dir}"
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

    results = runConcurrently(
        lambda ip: transfer_to_ap(ip, files, remote_dir, create_remote_dir),
        ap_list,
        "cert transfer"
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
