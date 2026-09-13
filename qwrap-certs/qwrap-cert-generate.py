import os
import sys
import re
import shutil
import subprocess
import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration ---
CA_CERT       = "cacert.pem" #Tejas personal CA cert
CA_KEY        = "cakey.pem"  #Tejas personal CA Key
ORG_NAME      = "Arista Networks" #Org name which will goes in client cert
VALIDITY_DAYS = 1825  # 5 years

AP_SSH_USER   = "root"
REMOTE_DIR    = "/root/"

# CA cert that signed the AGNI EAP server certificate.
# The client uses this to verify the RADIUS server during mutual TLS.
# This is NOT the same as the CA that signs client certs (cacert.pem).
# Export it from AGNI and set the path here.
AGNI_SERVER_CA = "AGNI_Root_CA.pem"

CACHE_FILE    = "/tmp/nssh.txt"
ONELOGIN_USER = os.environ["ONELOGIN_USER"]
ONELOGIN_PASS = os.environ["ONELOGIN_PASS"]
PORTAL_LOGIN  = "https://license.aristanetworks.com/api-auth/login/"
PORTAL_OTP    = "https://license.aristanetworks.com/sign/wifi_otp/"


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
# Challenge-response helpers  (mirrors conn.sh logic)
# ---------------------------------------------------------------------------

def check_cache(challenge):
    """Return cached OTP response for a challenge, or None."""
    if not os.path.exists(CACHE_FILE):
        return None
    with open(CACHE_FILE) as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line == challenge and i + 1 < len(lines):
            return lines[i + 1]
    return None


def save_cache(challenge, response):
    """Append challenge/response pair; trim file if > 200 lines."""
    with open(CACHE_FILE, "a") as f:
        f.write(f"{challenge}\n{response}\n")
    with open(CACHE_FILE) as f:
        lines = f.readlines()
    if len(lines) > 200:
        with open(CACHE_FILE, "w") as f:
            f.writelines(lines[2:])


def get_portal_response(challenge):
    """Fetch OTP response from the Arista license portal (same flow as conn.sh)."""
    session = requests.Session()
    session.verify = False

    # Grab CSRF token from login page
    session.get(PORTAL_LOGIN)
    csrf = session.cookies.get("csrftoken", "")

    # Log in
    session.post(
        PORTAL_LOGIN,
        data={
            "username": ONELOGIN_USER,
            "password": ONELOGIN_PASS,
            "submit": "Log in",
            "csrfmiddlewaretoken": csrf,
        },
        headers={"Referer": PORTAL_LOGIN},
    )
    csrf = session.cookies.get("csrftoken", csrf)

    # Request OTP signature
    r = session.post(
        PORTAL_OTP,
        data={"message": challenge, "csrfmiddlewaretoken": csrf},
        headers={"Referer": PORTAL_OTP},
    )

    try:
        return r.json().get("signature", "")
    except Exception:
        return ""


def get_response(challenge):
    """Cache-first lookup, then hit the portal."""
    cached = check_cache(challenge)
    if cached:
        print(f"   [cache] Challenge {challenge[:12]}... → using cached response")
        return cached

    print(f"   [portal] Fetching response for challenge: {challenge} ...")
    response = get_portal_response(challenge)
    if response:
        save_cache(challenge, response)
        return response

    print("[ERROR] Could not get OTP response from portal.")
    return ""


# ---------------------------------------------------------------------------
# AP file transfer via scp -O + expect (challenge-response, mirrors conn.sh)
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
    # Fallback: parse from combined output (in case log_user 0 didn't suppress fully)
    match = re.search(r"Response\[([^\]]+)\]", result.stdout + result.stderr)
    return match.group(1) if match else None


def scp_file_to_ap(local_path, remote_dest, ap_ip, response):
    """Transfer one file using scp -O, answering the OTP challenge via expect."""
    # Escape Tcl special chars in the response string
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
            print(f"   [OK]")
        else:
            print(f"   [FAIL]")
            success = False

    if success:
        print(f"\n[SUCCESS] All files uploaded to root@{ap_ip}:{REMOTE_DIR}")
    else:
        print(f"\n[PARTIAL] Some files failed — check AP connectivity and challenge validity.")
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

    # Output: one folder per client, three files matching the UI placeholders
    out_dir     = username
    client_cert = os.path.join(out_dir, "client.pem")
    client_key  = os.path.join(out_dir, "key.pem")
    ca_copy     = os.path.join(out_dir, "ca.pem")
    csr_file    = os.path.join(out_dir, "client.csr")
    ext_config  = os.path.join(out_dir, "client_ext.cnf")

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

    # 4. AGNI server CA — client uses this to verify the RADIUS server cert
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

        files_to_upload = [
            (client_cert, "client.pem"),
            (client_key,  "key.pem"),
            (ca_copy,     "ca.pem"),
        ]

        scp_to_ap(ap_ip, files_to_upload)

    print("-" * 45)
    print(f"Done. Local files preserved in: {out_dir}/")
    print("-" * 45)


if __name__ == "__main__":
    main()