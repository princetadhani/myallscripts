#!/bin/sh

# Default mode is IPv6 only (-6)
MODE="6"
DO_PING=0

# Parse command line flags
for arg in "$@"; do
  case "$arg" in
    -4)       MODE="4" ;;
    -6)       MODE="6" ;;
    -46|-64)  MODE="46" ;;
    -p|p)     DO_PING=1 ;;
  esac
done

# Fetch global fallback default gateways once
GLOBAL_V4_GW=$(ip -4 route show 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)
GLOBAL_V6_GW=$(ip -6 route show 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)

# Print Header Function
print_header() {
  echo "=========================================================================================================================================="
  printf "RADIO-ID: %-19s BAND: %s (Mode: IPv%s)\n" "$1" "$2" "$MODE"
  echo "=========================================================================================================================================="

  if [ "$MODE" = "4" ]; then
    if [ "$DO_PING" -eq 1 ]; then
      printf "%-13s | %-6s | %-20s | %-20s | %s\n" "veth" "Exists" "IPv4 Addr" "IPv4 Gateway" "v4 Ping"
      printf "%s-+-%s-+-%s-+-%s-+-%s\n" "-------------" "------" "--------------------" "--------------------" "-------"
    else
      printf "%-13s | %-6s | %-20s | %s\n" "veth" "Exists" "IPv4 Addr" "IPv4 Gateway"
      printf "%s-+-%s-+-%s-+-%s\n" "-------------" "------" "--------------------" "--------------------"
    fi
  elif [ "$MODE" = "6" ]; then
    if [ "$DO_PING" -eq 1 ]; then
      printf "%-13s | %-6s | %-32s | %-28s | %s\n" "veth" "Exists" "Global IPv6" "IPv6 Gateway" "v6 Ping"
      printf "%s-+-%s-+-%s-+-%s-+-%s\n" "-------------" "------" "--------------------------------" "----------------------------" "-------"
    else
      printf "%-13s | %-6s | %-32s | %s\n" "veth" "Exists" "Global IPv6" "IPv6 Gateway"
      printf "%s-+-%s-+-%s-+-%s\n" "-------------" "------" "--------------------------------" "----------------------------"
    fi
  elif [ "$MODE" = "46" ]; then
    if [ "$DO_PING" -eq 1 ]; then
      printf "%-13s | %-6s | %-16s | %-16s | %-6s | %-32s | %-28s | %s\n" \
             "veth" "Exists" "IPv4 Addr" "IPv4 GW" "v4 Ping" "Global IPv6" "IPv6 Gateway" "v6 Ping"
      printf "%s-+-%s-+-%s-+-%s-+-%s-+-%s-+-%s-+-%s\n" \
             "-------------" "------" "----------------" "----------------" "-------" "--------------------------------" "----------------------------" "-------"
    else
      printf "%-13s | %-6s | %-16s | %-16s | %-32s | %s\n" \
             "veth" "Exists" "IPv4 Addr" "IPv4 GW" "Global IPv6" "IPv6 Gateway"
      printf "%s-+-%s-+-%s-+-%s-+-%s-+-%s\n" \
             "-------------" "------" "----------------" "----------------" "--------------------------------" "----------------------------"
    fi
  fi
}

radios="0:2.4GHz 1:5GHz 2:6GHz"

for radio_info in $radios; do
  radio_id="${radio_info%%:*}"
  band="${radio_info#*:}"

  print_header "$radio_id" "$band"

  idx=1
  while [ "$idx" -le 28 ]; do
    iface="veth_in_${radio_id}_${idx}"
    table_name="sta_${radio_id}_${idx}_table"

    # Check if interface exists in kernel
    if ip link show dev "$iface" >/dev/null 2>&1; then
      if_exists="yes"
    else
      if_exists="no"
    fi

    if [ "$if_exists" = "yes" ]; then
      # ---------------- IPv4 DATA ----------------
      ipv4_addrs=$(ip -4 addr show dev "$iface" scope global 2>/dev/null | awk '/inet / {print $2}' | cut -d'/' -f1)
      v4_primary=$(echo "$ipv4_addrs" | head -n 1)

      # 1. Query specific STA policy table -> 2. Query interface route -> 3. Global fallback
      v4_gw=$(ip -4 route show table "$table_name" 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)
      [ -z "$v4_gw" ] && v4_gw=$(ip -4 route show dev "$iface" 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)
      [ -z "$v4_gw" ] && v4_gw="$GLOBAL_V4_GW"
      [ -z "$v4_gw" ] && v4_gw="N/A"

      v4_ping="N/A"
      if [ "$DO_PING" -eq 1 ] && [ "$v4_gw" != "N/A" ] && [ -n "$v4_primary" ]; then
        if ping -c 2 -W 1 -I "$v4_primary" "$v4_gw" >/dev/null 2>&1; then
          v4_ping="OK"
        else
          v4_ping="FAIL"
        fi
      fi

      # ---------------- IPv6 DATA ----------------
      ipv6_addrs=$(ip -6 addr show dev "$iface" scope global 2>/dev/null | awk '/inet6/ {print $2}' | cut -d'/' -f1)
      v6_primary=$(echo "$ipv6_addrs" | head -n 1)

      # 1. Query specific STA policy table -> 2. Query interface route -> 3. Global fallback
      v6_gw=$(ip -6 route show table "$table_name" 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)
      [ -z "$v6_gw" ] && v6_gw=$(ip -6 route show dev "$iface" 2>/dev/null | awk '/default via/ {print $3}' | head -n 1)
      [ -z "$v6_gw" ] && v6_gw="$GLOBAL_V6_GW"
      [ -z "$v6_gw" ] && v6_gw="N/A"

      v6_ping="N/A"
      if [ "$DO_PING" -eq 1 ] && [ "$v6_gw" != "N/A" ] && [ -n "$v6_primary" ]; then
        if ping6 -c 2 -W 1 -I "$v6_primary" "$v6_gw" >/dev/null 2>&1; then
          v6_ping="OK"
        else
          v6_ping="FAIL"
        fi
      fi

      # ---------------- OUTPUT PRINTING ----------------
      if [ "$MODE" = "4" ]; then
        if [ -n "$ipv4_addrs" ]; then
          first_line=1
          echo "$ipv4_addrs" | while read -r curr_ip; do
            if [ "$first_line" -eq 1 ]; then
              [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-20s | %-20s | %s\n" "$iface" "$if_exists" "$curr_ip" "$v4_gw" "$v4_ping" \
                                   || printf "%-13s | %-6s | %-20s | %s\n" "$iface" "$if_exists" "$curr_ip" "$v4_gw"
              first_line=0
            else
              [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-20s | %-20s | %s\n" "" "" "$curr_ip" "" "" \
                                   || printf "%-13s | %-6s | %-20s |\n" "" "" "$curr_ip"
            fi
          done
        else
          [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-20s | %-20s | %s\n" "$iface" "$if_exists" "N/A" "$v4_gw" "$v4_ping" \
                               || printf "%-13s | %-6s | %-20s | %s\n" "$iface" "$if_exists" "N/A" "$v4_gw"
        fi

      elif [ "$MODE" = "6" ]; then
        if [ -n "$ipv6_addrs" ]; then
          first_line=1
          echo "$ipv6_addrs" | while read -r curr_ip; do
            if [ "$first_line" -eq 1 ]; then
              [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-32s | %-28s | %s\n" "$iface" "$if_exists" "$curr_ip" "$v6_gw" "$v6_ping" \
                                   || printf "%-13s | %-6s | %-32s | %s\n" "$iface" "$if_exists" "$curr_ip" "$v6_gw"
              first_line=0
            else
              [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-32s | %-28s | %s\n" "" "" "$curr_ip" "" "" \
                                   || printf "%-13s | %-6s | %-32s |\n" "" "" "$curr_ip"
            fi
          done
        else
          [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-32s | %-28s | %s\n" "$iface" "$if_exists" "N/A" "$v6_gw" "$v6_ping" \
                               || printf "%-13s | %-6s | %-32s | %s\n" "$iface" "$if_exists" "N/A" "$v6_gw"
        fi

      elif [ "$MODE" = "46" ]; then
        [ -z "$v4_primary" ] && v4_primary="N/A"
        [ -z "$v6_primary" ] && v6_primary="N/A"

        if [ "$DO_PING" -eq 1 ]; then
          printf "%-13s | %-6s | %-16s | %-16s | %-6s | %-32s | %-28s | %s\n" \
                 "$iface" "$if_exists" "$v4_primary" "$v4_gw" "$v4_ping" "$v6_primary" "$v6_gw" "$v6_ping"
        else
          printf "%-13s | %-6s | %-16s | %-16s | %-32s | %s\n" \
                 "$iface" "$if_exists" "$v4_primary" "$v4_gw" "$v6_primary" "$v6_gw"
        fi
      fi

    else
      # Interface does NOT exist
      if [ "$MODE" = "4" ]; then
        [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-20s | %-20s | %s\n" "$iface" "no" "N/A" "N/A" "N/A" \
                             || printf "%-13s | %-6s | %-20s | %s\n" "$iface" "no" "N/A" "N/A"
      elif [ "$MODE" = "6" ]; then
        [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-32s | %-28s | %s\n" "$iface" "no" "N/A" "N/A" "N/A" \
                             || printf "%-13s | %-6s | %-32s | %s\n" "$iface" "no" "N/A" "N/A"
      elif [ "$MODE" = "46" ]; then
        [ "$DO_PING" -eq 1 ] && printf "%-13s | %-6s | %-16s | %-16s | %-6s | %-32s | %-28s | %s\n" "$iface" "no" "N/A" "N/A" "N/A" "N/A" "N/A" "N/A" \
                             || printf "%-13s | %-6s | %-16s | %-16s | %-32s | %s\n" "$iface" "no" "N/A" "N/A" "N/A" "N/A"
      fi
    fi

    idx=$((idx + 1))
  done

  echo ""
done