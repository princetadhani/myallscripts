#!/usr/bin/env python3
"""Summarize pasted tcpdump IPv6 router advertisements. Run: python3 parse_ra.py."""

import argparse
import re


# Replace the text between the triple quotes with your complete tcpdump output.
TCPDUMP_OUTPUT = r"""
09:19:15.257955 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 64) fe80::a8bb:ccff:fedd:8581 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 64
	hop limit 64, Flags [managed, other stateful], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): aa:bb:cc:dd:85:81
	    0x0000:  aabb ccdd 8581
	  mtu option (5), length 8 (1):  9000
	    0x0000:  0000 0000 2328
	  prefix info option (3), length 32 (4): fdd5:caf1:7b7e:8004::/64, Flags [onlink, auto], valid time 3600s, pref. time 3600s
	    0x0000:  40c0 0000 0e10 0000 0e10 0000 0000 fdd5
	    0x0010:  caf1 7b7e 8004 0000 0000 0000 0000
09:19:15.258286 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 64) fe80::a8bb:ccff:fedd:8581 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 64
	hop limit 64, Flags [managed, other stateful], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): aa:bb:cc:dd:85:81
	    0x0000:  aabb ccdd 8581
	  mtu option (5), length 8 (1):  9000
	    0x0000:  0000 0000 2328
	  prefix info option (3), length 32 (4): fdd5:caf1:7b7e:8004::/64, Flags [onlink, auto], valid time 3600s, pref. time 3600s
	    0x0000:  40c0 0000 0e10 0000 0e10 0000 0000 fdd5
	    0x0010:  caf1 7b7e 8004 0000 0000 0000 0000
09:19:15.264675 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 32) fe80::c269:11ff:fe51:5077 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 32
	hop limit 64, Flags [none], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): c0:69:11:51:50:77
	    0x0000:  c069 1151 5077
	  mtu option (5), length 8 (1):  1500
	    0x0000:  0000 0000 05dc
09:19:15.265051 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 32) fe80::c269:11ff:fe51:5077 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 32
	hop limit 64, Flags [none], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): c0:69:11:51:50:77
	    0x0000:  c069 1151 5077
	  mtu option (5), length 8 (1):  1500
	    0x0000:  0000 0000 05dc
09:19:15.415221 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 64) fe80::a8bb:ccff:fedd:8581 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 64
	hop limit 64, Flags [managed, other stateful], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): aa:bb:cc:dd:85:81
	    0x0000:  aabb ccdd 8581
	  mtu option (5), length 8 (1):  9000
	    0x0000:  0000 0000 2328
	  prefix info option (3), length 32 (4): fdd5:caf1:7b7e:8004::/64, Flags [onlink, auto], valid time 3600s, pref. time 3600s
	    0x0000:  40c0 0000 0e10 0000 0e10 0000 0000 fdd5
	    0x0010:  caf1 7b7e 8004 0000 0000 0000 0000
09:19:15.415601 IP6 (hlim 255, next-header ICMPv6 (58) payload length: 64) fe80::a8bb:ccff:fedd:8581 > fe80::24b6:2dff:fe00:acaa: [icmp6 sum ok] ICMP6, router advertisement, length 64
	hop limit 64, Flags [managed, other stateful], pref medium, router lifetime 1800s, reachable time 0ms, retrans timer 1000ms
	  source link-address option (1), length 8 (1): aa:bb:cc:dd:85:81
	    0x0000:  aabb ccdd 8581
	  mtu option (5), length 8 (1):  9000
	    0x0000:  0000 0000 2328
	  prefix info option (3), length 32 (4): fdd5:caf1:7b7e:8004::/64, Flags [onlink, auto], valid time 3600s, pref. time 3600s
	    0x0000:  40c0 0000 0e10 0000 0e10 0000 0000 fdd5
	    0x0010:  caf1 7b7e 8004 0000 0000 0000 0000
"""


def flag_bit(flags, name):
    """Report unknown when tcpdump did not print a flags field."""
    if flags is None:
        return "unknown"
    return str(int(name in {item.strip().lower() for item in flags.split(",")}))


def summarize(text):
    """Group RAs by source and advertised contents, in first-seen order.

    Timestamps, IPv6 destinations, and checksum annotations are ignored.
    Changes to advertised options or lifetimes produce a separate group.
    """
    packets = []
    current = None
    for line in text.splitlines():
        # A new IP packet ends the previous packet, including non-RA packets.
        if re.search(r"\bIP6?\s", line) and not line[:1].isspace():
            current = None
            if re.search(r"\brouter advertisement\b", line, re.I):
                current = [line]
                packets.append(current)
        elif current is not None and line[:1].isspace() and line.strip():
            current.append(line)

    if not packets:
        return "No router advertisements found."

    groups = {}
    for packet in packets:
        addresses = re.search(r"(\S+)\s+>\s+(\S+):\s", packet[0])
        source = addresses.group(1).lower() if addresses else packet[0]
        # Normalize indentation/spacing, retaining every advertised field.
        contents = tuple(" ".join(line.lower().split()) for line in packet[1:])
        key = (source, contents)
        if key not in groups:
            groups[key] = {"packet": packet, "count": 0}
        groups[key]["count"] += 1

    results = []
    for number, group in enumerate(groups.values(), 1):
        packet = group["packet"]
        header = packet[0]
        addresses = re.search(r"(\S+)\s+>\s+(\S+):\s", header)
        output = [f"Router advertisement {number}",
                  f"Received: {group['count']} packet(s)", ""]
        if addresses:
            output.append(f"Source: {addresses.group(1)}")
        body = "\n".join(packet[1:])
        mac = re.search(
            r"source link-address option[^\n]*?:\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})",
            body, re.I,
        )
        mtu = re.search(r"mtu option[^\n]*?:\s*(\d+)", body, re.I)
        output.append(f"Source link-address: {mac.group(1) if mac else 'not present'}")
        output.append(f"MTU: {mtu.group(1) if mtu else 'not present'}")
        flags = re.search(r"\bhop limit\s+\d+,\s*Flags\s*\[([^]]*)\]",
                          "\n".join(packet), re.I)
        router_flags = flags.group(1) if flags else None
        output.extend([f"M bit= {flag_bit(router_flags, 'managed')}",
                       f"O bit= {flag_bit(router_flags, 'other stateful')}"])
        prefix_count = 0
        for line in packet[1:]:
            if "prefix info option" not in line.lower():
                continue
            prefix_count += 1
            prefix = re.search(r":\s*([0-9a-f:]+/\d+)", line, re.I)
            flags = re.search(r"Flags\s*\[([^]]*)\]", line, re.I)
            output.append(f"Prefix info {prefix_count}: {prefix.group(1) if prefix else 'unknown'}")
            output.append(f"  A bit= {flag_bit(flags.group(1) if flags else None, 'auto')}")
            for label, pattern in (
                ("Valid lifetime", r"\bvalid time\s+([^\s,]+)"),
                ("Preferred lifetime", r"\bpref\. time\s+([^\s,]+)"),
            ):
                match = re.search(pattern, line, re.I)
                if match:
                    output.append(f"  {label}: {match.group(1)}")
        if not prefix_count:
            output.extend(["Prefix info: not present", "A bit= N/A (no prefix option)"])
        # Keep all field values in one column, preserving prefix indentation.
        for index in range(3, len(output)):
            field = re.match(r"(.*?[:=])\s*(.*)", output[index])
            if field:
                output[index] = f"{field.group(1):<24} {field.group(2)}"
        results.append("\n".join(output))
    totals = (f"Total RA packets: {len(packets)}\n"
              f"Unique RAs: {len(groups)}\n"
              f"Duplicate packets: {len(packets) - len(groups)}")
    separator = "\n\n" + "=" * 56 + "\n"
    return totals + separator + separator.join(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Show unique IPv6 router advertisements from TCPDUMP_OUTPUT pasted in this file.",
        epilog="Groups use source + advertised contents; timestamps and destinations are ignored.",
    )
    parser.parse_args()
    print(summarize(TCPDUMP_OUTPUT))
