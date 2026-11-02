import json
import os
import sys
import re
import shutil
import subprocess
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration ---
CA_CERT       = "cacert.pem" # Tejas personal CA cert
CA_KEY        = "cakey.pem"  # Tejas personal CA Key
ORG_NAME      = "Arista Networks" # Org name which will go into client cert
VALIDITY_DAYS = 1825  # 5 years

AP_SSH_USER   = "root"
REMOTE_DIR    = "/root/"  # Destination folder on AP

# CA cert that signed the AGNI EAP server certificate.
# The client uses this to verify the RADIUS server during mutual TLS.
AGNI_SERVER_CA = "AGNI_Root_CA.pem"

# ---------------------------------------------------------------------------
# Wifi OTP signing endpoint
# ---------------------------------------------------------------------------
WIFI_OTP_URL = "https://license.aristanetworks.com/sign/wifi-otp/"
WIFI_OTP_KEY = "d9ca932a4bc8fbaaa5021b00e14dd453d469eaaf"


# ---------------------------------------------------------------------------
# Cert generation helpers
# ---------------------------------------------------------------------------

def run_command(cmd, capture=True):
    try:
        if not capture:
            subprocess.run(cmd, check=True)
            return None
        result = subprocess.run(cmd, check=True, text=True, capture_output=True)
        return result
    except subprocess.CalledProcessError as e:
        if capture:
            print(f"\n[ERROR] Command failed: {' '.join(cmd)}")
            print(f"Error Output: {e.stderr}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Challenge-response helpers
# ---------------------------------------------------------------------------

def get_response(challenge):
    """Sign an arista-ssh-agent challenge via the Wifi OTP endpoint."""
    print(f"   [otp] Fetching response for challenge: {challenge} ...")

    header = {
        "Authorization": f"Token {WIFI_OTP_KEY}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({"message": challenge})

    try:
        r = requests.post(WIFI_OTP_URL, headers=header, data=payload, allow_redirects=True, timeout=30)
    except requests.RequestException as e:
        print(f"[ERROR] Wifi OTP request failed: {e}")
        return ""

    if r.status_code != 201:
        print(f"[ERROR] Wifi OTP request failed ({r.status_code}) for challenge {challenge}")
        return ""

    signature = r.json().get("signature", "")
    if not signature:
        print("[ERROR] Wifi OTP response did not contain 'signature'.")
    return signature


# ---------------------------------------------------------------------------
# AP file transfer via scp -O + expect
# ---------------------------------------------------------------------------

def get_challenge_from_ap(ap_ip):
    """SSH probe using expect to extract the OTP challenge string from the AP."""
    result = subprocess.run(
        ["/usr/bin/expect", "-c",
         f"set timeout 10\n"
         f"log_user 0\n"
         f"spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{ap_ip}\n"
         f"expect -re {{Response\\[([^\\]]+)\\]:}}\n"
         f"puts $expect_out(1,string)\n"
         f"exit 0\n"],
        capture_output=True, text=True, timeout=15,
    )
    ch = result.stdout.strip()
    if ch:
        return ch
    match = re.search(r"Response\[([^\]]+)\]", result.stdout + result.stderr)
    return match.group(1) if match else None


def scp_file_to_ap(local_path, remote_dest, ap_ip, response):
    """Transfer one file using scp -O, answering the OTP challenge via expect."""
    safe_resp = response.replace("\\", "\\\\").replace('"', '\\"') \
                        .replace("[", "\\[").replace("]", "\\]")

    result = subprocess.run(
        ["/usr/bin/expect", "-c",
         f"set timeout 60\n"
         f"spawn scp -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
         f" {local_path} root@{ap_ip}:{remote_dest}\n"
         f"expect -re {{Response\\[}}\n"
         f"send \"{safe_resp}\\r\"\n"
         f"expect eof\n"],
        timeout=90,
    )
    return result.returncode == 0


def scp_to_ap(ap_ip, files):
    """Probe AP for OTP challenge, resolve it, then scp -O each file to REMOTE_DIR."""
    print(f"\n-> Probing {AP_SSH_USER}@{ap_ip} for challenge ...")
    challenge = get_challenge_from_ap(ap_ip)
    if not challenge:
        print("[FAIL] Could not retrieve challenge from AP.")
        return False

    print(f"   Challenge: {challenge}")
    response = get_response(challenge)
    if not response:
        print("[FAIL] Could not get OTP response.")
        return False

    remote_dir = REMOTE_DIR.rstrip("/")
    success = True

    for local_path, remote_name in files:
        dest = f"{remote_dir}/{remote_name}"
        print(f"-> Uploading {local_path} → root@{ap_ip}:{dest} ...")
        if scp_file_to_ap(local_path, dest, ap_ip, response):
            print("   [OK]")
        else:
            print("   [FAIL]")
            success = False

    if success:
        print(f"\n[SUCCESS] All files uploaded to root@{ap_ip}:{REMOTE_DIR}")
    else:
        print("\n[PARTIAL] Some files failed — check AP connectivity and challenge validity.")
    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(CA_CERT) or not os.path.exists(CA_KEY):
        print("Error: CA files (cacert.pem / cakey.pem) not found.")
        sys.exit(1)

    if len(sys.argv) > 1:
        username = sys.argv[1]
    else:
        username = input("Enter the Certificate Name (CN) for this RadSec client: ").strip()

    if not username:
        print("Error: Name cannot be empty.")
        sys.exit(1)

    print(f"\n--- Generating RadSec Client Certificates for: {username} ---")

    # Output: folder per client with CN-prefixed filenames
    out_dir     = username
    client_cert = os.path.join(out_dir, f"{username}-client.pem")
    client_key  = os.path.join(out_dir, f"{username}-key.pem")
    ca_copy     = os.path.join(out_dir, f"{username}-ca.pem")
    csr_file    = os.path.join(out_dir, f"{username}-client.csr")
    ext_config  = os.path.join(out_dir, f"{username}-client_ext.cnf")

    os.makedirs(out_dir, exist_ok=True)

    # 1. Private key
    run_command(["openssl", "genrsa", "-out", client_key, "2048"])

    # 2. CSR
    subj = f"/C=IN/ST=Maharashtra/L=Pune/O={ORG_NAME}/CN={username}"
    run_command(["openssl", "req", "-new", "-key", client_key, "-out", csr_file, "-subj", subj])

    # 3. Sign with CA
    with open(ext_config, "w") as f:
        f.write("basicConstraints = CA:FALSE\n")
        f.write("keyUsage = digitalSignature, keyEncipherment\n")
        f.write("extendedKeyUsage = clientAuth\n")

    run_command([
        "openssl", "x509", "-req",
        "-in",  csr_file,
        "-CA",  CA_CERT, "-CAkey", CA_KEY, "-CAcreateserial",
        "-out", client_cert,
        "-days", str(VALIDITY_DAYS),
        "-sha256", "-extfile", ext_config,
    ])

    # 4. AGNI server CA
    shutil.copy(AGNI_SERVER_CA, ca_copy)

    # Cleanup temp files
    for f in [csr_file, ext_config]:
        if os.path.exists(f):
            os.remove(f)

    print(f"\n[OK] Certificates ready in: {out_dir}/")
    print(f"  Client Cert:    {client_cert}")
    print(f"  Private Key:    {client_key}")
    print(f"  Server CA Cert: {ca_copy}")

    # --- Transfer to AP ---
    print("\n" + "=" * 45)
    print("        AUTOMATED TRANSFER TO AP (RadSec)       ")
    print("=" * 45)

    should_copy = input("Do you want to copy these files to the AP now? (y/n): ").lower()

    if should_copy == "y":
        ap_ip = input("Enter AP IP address: ").strip()

        # Files uploaded using CN prefixes on the remote destination as well
        files_to_upload = [
            (client_cert, f"{username}-client.pem"),
            (client_key,  f"{username}-key.pem"),
            (ca_copy,     f"{username}-ca.pem"),
        ]

        scp_to_ap(ap_ip, files_to_upload)

    print("-" * 45)
    print(f"Done. Local files preserved in: {out_dir}/")
    print("-" * 45)


if __name__ == "__main__":
    main()