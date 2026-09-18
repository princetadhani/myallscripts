"""

py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action configure --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action configure --config-persist enable --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action deconfigure --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action config-persist-enable --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action save-qwrap-config-yaml --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49
py /Users/prince.tadhani/myallscripts/qwrap-configuration-script/qwrap-manager.py --action apply-qwrap-config-yaml --ap 10.86.205.82,10.86.205.240,10.86.205.157,10.86.205.90,10.86.205.88,10.86.205.49


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

    {   # AP-1 1-C400--F031BF--S2-QWRAP
        "host": "10.86.205.82",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-Agni-Office",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/ca.pem",
                "keyMgmt":          "WPA-EAP-SUITE-B-192",
                "ieee80211w":       "required",
                "groupCipher":      "GCMP-256",
                "pairwiseCipher":   "GCMP-256",
                "clients": {
                    "count":          26,
                    "hostnamePrefix": "CN-3028",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-Agni-Office",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3027@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3027@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3027@qwrap.com/ca.pem",
                "keyMgmt":          "WPA-EAP-SUITE-B-192",
                "ieee80211w":       "required",
                "groupCipher":      "GCMP-256",
                "pairwiseCipher":   "GCMP-256",
                 "clients": {
                    "count":          28,
                    "hostnamePrefix": "CN-3027",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S2-Agni-Office",
                "security":         "wpa3-dot1x",
                "eapType":          "tls",
                "digitalCertPath":  "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/client.pem",
                "privateKeyPath":   "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/key.pem",
                "serverCaCertPath": "/Users/prince.tadhani/myallscripts/qwrap-certs/qwrapAP3028@qwrap.com/ca.pem",
                "keyMgmt":          "WPA-EAP-SUITE-B-192",
                "ieee80211w":       "required",
                "groupCipher":      "GCMP-256",
                "pairwiseCipher":   "GCMP-256",
                "clients": {
                    "count":          26,
                    "hostnamePrefix": "CN-3028",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-2 1-C430--05D47F--S2-QWRAP
        "host": "10.86.205.240",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-Dynamic-Vlan",
                "security":         "wpa2-dot1x",
                "eapType":          "peap",
                "username":         "S2-Dynamic-Vlan-ssid-vlan-with-bw",
                "password":         "S2-Dynamic-Vlan-ssid-vlan-with-bw",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "S2-Dynamic-Vlan-ssid-vlan-with-bw",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-Dynamic-Vlan",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "S2-Dynamic-Vlan-3038",
                "password":         "S2-Dynamic-Vlan-3038",
                "keyMgmt":          "WPA-EAP-SHA256",
                "ieee80211w":       "required",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Dynamic-Vlan-3038",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
               "ssid":             "S2-Dynamic-Vlan",
                "security":         "wpa3-dot1x",
                "eapType":          "peap",
                "username":         "S2-Dynamic-Vlan-3080",
                "password":         "S2-Dynamic-Vlan-3080",
                "keyMgmt":          "WPA-EAP-SHA256",
                "ieee80211w":       "required",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Dynamic-Vlan-3080",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-3 1-C460D--C2750F--S2-QWRAP
        "host": "10.86.205.157",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-VlanPool",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "ieee80211w":       "required",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "S2-VlanPool",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-VlanPool",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "ieee80211w":       "required",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "S2-VlanPool",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S2-VlanPool",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "ieee80211w":       "required",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "S2-VlanPool",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-4 2-C460D--C274BF--S2-QWRAP
        "host": "10.86.205.90",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-APHP-WPA3-T-SAE",
                "security":         "wpa2",
                "passphrase":       "Hello@123",
                "portalType":       "internal",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Portal",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-APHP-WPA3-T-SAE",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "portalType":       "internal",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Portal",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S2-APHP-WPA3-T-SAE",
                "security":         "wpa3",
                "passphrase":       "Hello@123",
                "portalType":       "internal",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "Portal",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },


    {   # AP-5 3-C460D--C27D2F--S2-QWRAP
        "host": "10.86.205.88",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-OWE-Firewall",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "OWE",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-OWE-Firewall",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "OWE",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S2-OWE-Firewall",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "OWE",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },


    {   #AP-6 1-O405--20203F--S2-QWRAP
        "host": "10.86.205.49",
        "radios": {
            0: {  # 2.4ghz
                "ssid":             "S2-GPSK",
                "security":         "wpa2",
                "passphrase":       "welcome3080ra",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "R1-3080RA",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            1: {  # 5ghz
                "ssid":             "S2-GPSK",
                "security":         "wpa2",
                "passphrase":       "welcome3081dhcpv6",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "R1-3081dhcpv6",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
            2: {  # 6ghz
                "ssid":             "S2-OWE-Firewall",
                "security":         "owe",
                "clients": {
                    "count":          28,
                    "hostnamePrefix": "OWE",
                    "ipv4":           0,
                    "ipv6":           1,
                },
            },
        },
    },
  
]
