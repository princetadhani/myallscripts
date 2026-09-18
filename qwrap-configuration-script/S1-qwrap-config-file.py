"""

py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action configure --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action configure --config-persist enable --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action deconfigure --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action config-persist-enable --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action save-qwrap-config-yaml --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action apply-qwrap-config-yaml --ap 10.86.205.122,10.86.204.204,10.86.205.60,10.86.205.223,10.86.204.227,10.86.205.165

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

#########################################################################################################################################
#                                             Define the AP's here which need to configure                                              #
#########################################################################################################################################
AP_LIST = [
    {   # AP-1 1-C430--00923F--S1-QWRAP
        "host": "10.86.205.122",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-Agni-Corp",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3003@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3003@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3003@qwrap.com/ca.pem",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "qwrapAP3003@qwrap.com",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-Agni-Corp",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/ca.pem",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "qwrapAP3004@qwrap.com1",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-Agni-Corp",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3004@qwrap.com/ca.pem",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "qwrapAP3004@qwrap.com2",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },

    {   # AP-2: 2-C430--007F7F--S1-QWRAP
        "host": "10.86.204.204",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-GPSK",
                "security":         "wpa2",
                "passphrase":       "welcome3005",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "GPSK-R1-Vlan3005",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-GPSK",
                "security":         "wpa2",
                "passphrase":       "welcome3006",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "GPSK-R1-Vlan3006",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-Vxlan-VlanPool",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "vxlanvlanpool",
                "password":         "vxlanvlanpool",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "vxlanvlanpool",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },

    {   # AP-3 3-C430--00978F--S1-QWRAP
        "host": "10.86.205.60",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-Vxlan-VlanPool",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "vxlanvlanpool",
                "password":         "vxlanvlanpool",
                "keyMgmt":          "WPA-EAP-SHA256",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "vxlanvlanpool-AP-3",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-Vxlan-VlanPool",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "vxlanvlanpool",
                "password":         "vxlanvlanpool",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "vxlanvlanpool-AP-3",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-Vxlan-VlanPool",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "vxlanvlanpool",
                "password":         "vxlanvlanpool",
                "keyMgmt":          "FT-EAP",
                "ieee80211w":       "required",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "vxlanvlanpool-AP-3",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-4 1-C460D--C2755F--S1-QWRAP
        "host": "10.86.205.223",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-Vxlan-OWE",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Owe-Mac-Auth",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-Vxlan-OWE",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Owe-Mac-Auth",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-Vxlan-OWE",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Owe-Mac-Auth",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-5 2-C460D--C2714F--S1-QWRAP
        "host": "10.86.204.227",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-EoGRE",
                "security":         "wpa2-dot1x",
                "eapType":          "peap",
                "username":         "eogre3013",
                "password":         "eogre3013",
                "keyMgmt":          "FT-EAP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "eogre3013",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-EoGRE",
                "security":         "wpa2-dot1x",
                "eapType":          "peap",
                "username":         "role-EoGRE-11W",
                "password":         "role-EoGRE-11W",
                "keyMgmt":          "FT-EAP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "eogre3013",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-APHP-WPA3-SAE",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "portalType":       "internal",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "2-C460D--C2714F-Portal",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-6 1-O405--201EFF--S1-QWRAP
        "host": "10.86.205.165",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S1-NAT-PSK",
                "security":         "wpa2",
                "passphrase":       "Hello@123",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "NAT-R0",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S1-NAT-PSK",
                "security":         "wpa2",
                "passphrase":       "Hello@123",
                "groupCipher":      "CCMP",
                "pairwiseCipher":   "CCMP",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "NAT-R1",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S1-APHP-WPA3-SAE",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "portalType":       "internal",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "2-C460D--C2714F-Portal",
                    "ipv4":           1,
                    "ipv6":           1,
                },
            },
        },
    },


]
