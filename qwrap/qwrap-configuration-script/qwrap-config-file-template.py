"""
QWRAP AP configuration file.

Edit AP_LIST below. Each entry is one standalone QWRAP AP with up to 3 radios
(0=2.4ghz, 1=5ghz, 2=6ghz). For a radio you don't want to touch, leave its
dict as {} (skipped for configure/deconfigure).

All possible per-radio fields are listed below (leave unused ones as None):

    ssid              - SSID name                                                   
    security          - "open" | "owe" | "wpa2" | "wpa2-dot1x" | "wpa3" | "wpa3-dot1x"  (all security types)
    passphrase        - PSK/SAE passphrase                                              (wpa2 / wpa3)
    eapType           - "peap" | "tls"                                                  (wpa2-dot1x / wpa3-dot1x)
    username          - dot1x username                                                  (peap: required)   
                                                                                        (tls: optional - auto-derived from digitalCertPath's CN if omitted, same as CueQwrapCluster)                   
    password          - dot1x password                                                  (peap only)
    digitalCertPath   - client cert .pem LOCAL path on this laptop                      (tls only)
    privateKeyPath    - private key .pem LOCAL path on this laptop                      (tls only)
    serverCaCertPath  - CA cert .pem LOCAL path on this laptop                          (tls only)
    keyMgmt           - "WPA-EAP" | "WPA-EAP-SHA256" | "FT-EAP" | "WPA-EAP-SUITE-B-192"
    bssid             - Specific BSSID to connect to
    ieee80211w        - "disabled" | "optional" | "required"
    groupCipher       - "GCMP-256" | "CCMP"
    pairwiseCipher    - "GCMP-256" | "CCMP"
    portalType        - "internal"
    protocol          - "N" | "VHT" | "HE" | "EHT"
    channelWidth      - 20 | 40 | 80 | 160 | 320

    clients           - Optional dictonary to add virtual clients to this radio after it's configured (mirrors qwraptor's Client Configuration panel):
                             count          - Number of virtual clients to add (1-28)
                             hostnamePrefix - Hostname prefix for the clients (default: None, uses device name)
                             ipv4           - 1 to have clients acquire an IPv4 address via DHCP (default: 0)
                             ipv6           - 1 to have clients acquire an IPv6 address (default: 0)
                         To add clients to a radio: define the "clients" dict with a count (1-28) i.e "count":          28.
                         To NOT add clients to a radio: either don't define the "clients" dict at all, or define it with count: 0.

NOTE: keyMgmt "WPA-EAP-SUITE-B-192" generally requires groupCipher/pairwiseCipher
      "GCMP-256" and disables FT roaming (FT-EAP not settable together with it).

NOTE: For eapType "tls", digitalCertPath/privateKeyPath/serverCaCertPath must be
      LOCAL paths on this laptop. qwrap-manager.py automatically SCPs each file
      to "/root/<username>/<filename>" on the AP (creating the directory if
      needed) and rewrites the CLI command to use the AP-side path, mirroring
      CueQwrapCluster's cert upload behavior. No manual SCP or AP-side path is
      needed. If "username" is left blank, it's auto-extracted from
      digitalCertPath's certificate commonName (CN) - you can remove that field
      entirely for eapType "tls".
"""

# Global CLI credentials used for every AP (only add "cliUsername"/"cliPassword"
# keys to a specific AP dict below if that one AP needs different credentials).
DEFAULT_CLI_USERNAME = "root"
DEFAULT_CLI_PASSWORD = "arastra"


# TEMPLATE - copy this block per AP, paste into AP_LIST below, fill in the host
# IP and only the fields you need per radio - remove any field you don't need.
# Leave a radio as {} (or remove its line) to skip it entirely - it will NOT be
# configured/deconfigured.
#
# Fields pre-filled with "opt1 | opt2 | ..." are pick-one enums - replace the
# whole string with just the option you want (e.g. "security": "wpa3"). Fields
# left as "" (ssid, passphrase, username, password, bssid, cert paths) are
# free-text - fill in your own value.
"""

    {
        "host": "",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "",
                "security":         "open | owe | wpa2 | wpa2-dot1x | wpa3 | wpa3-dot1x",
                "passphrase":       "",
                "eapType":          "peap | tls",
                "username":         "",
                "password":         "",
                "digitalCertPath":  "",
                "privateKeyPath":   "",
                "serverCaCertPath": "",
                "keyMgmt":          "WPA-EAP | WPA-EAP-SHA256 | FT-EAP | WPA-EAP-SUITE-B-192",
                "bssid":            "",
                "ieee80211w":       "disabled | optional | required",
                "groupCipher":      "GCMP-256 | CCMP",
                "pairwiseCipher":   "GCMP-256 | CCMP",
                "portalType":       "internal",
                "protocol":         "N | VHT | HE | EHT",
                "channelWidth":     "20 | 40 | 80 | 160 | 320",
                "clients": {
                    "count":          0,
                    "hostnamePrefix": "",
                    "ipv4":           0,
                    "ipv6":           0,
                },
            },
            1: {  # 5ghz
                "ssid":             "",
                "security":         "open | owe | wpa2 | wpa2-dot1x | wpa3 | wpa3-dot1x",
                "passphrase":       "",
                "eapType":          "peap | tls",
                "username":         "",
                "password":         "",
                "digitalCertPath":  "",
                "privateKeyPath":   "",
                "serverCaCertPath": "",
                "keyMgmt":          "WPA-EAP | WPA-EAP-SHA256 | FT-EAP | WPA-EAP-SUITE-B-192",
                "bssid":            "",
                "ieee80211w":       "disabled | optional | required",
                "groupCipher":      "GCMP-256 | CCMP",
                "pairwiseCipher":   "GCMP-256 | CCMP",
                "portalType":       "internal",
                "protocol":         "N | VHT | HE | EHT",
                "channelWidth":     "20 | 40 | 80 | 160 | 320",
                "clients": {
                    "count":          0,
                    "hostnamePrefix": "",
                    "ipv4":           0,
                    "ipv6":           0,
                },
            },
            2: {  # 6ghz
                "ssid":             "",
                "security":         "open | owe | wpa2 | wpa2-dot1x | wpa3 | wpa3-dot1x",
                "passphrase":       "",
                "eapType":          "peap | tls",
                "username":         "",
                "password":         "",
                "digitalCertPath":  "",
                "privateKeyPath":   "",
                "serverCaCertPath": "",
                "keyMgmt":          "WPA-EAP | WPA-EAP-SHA256 | FT-EAP | WPA-EAP-SUITE-B-192",
                "bssid":            "",
                "ieee80211w":       "disabled | optional | required",
                "groupCipher":      "GCMP-256 | CCMP",
                "pairwiseCipher":   "GCMP-256 | CCMP",
                "portalType":       "internal",
                "protocol":         "N | VHT | HE | EHT",
                "channelWidth":     "20 | 40 | 80 | 160 | 320",
                "clients": {
                    "count":          0,
                    "hostnamePrefix": "",
                    "ipv4":           0,
                    "ipv6":           0,
                },
            },
        },
    },

"""
#########################################################################################################################################
#                                             Define the AP's here which need to configure                                              #
#########################################################################################################################################
AP_LIST = [
    
]
