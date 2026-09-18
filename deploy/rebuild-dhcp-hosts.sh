#!/bin/bash
# 生成 /etc/dhcp/hosts.conf（dhcpd 的 include 不支持通配符，只能逐文件列出）
# 用法： sudo /usr/local/sbin/rebuild-dhcp-hosts.sh
set -e

SRC_DIR=${SRC_DIR:-/etc/dhcp/hosts}
TARGET=${TARGET:-/etc/dhcp/hosts.conf}

{
  echo "# AUTO-GENERATED $(date '+%F %T') - 请勿手动修改"
  for f in "${SRC_DIR}"/*.conf; do
    [ -e "$f" ] || continue
    echo "include \"$f\";"
  done
} > "$TARGET"

chmod 644 "$TARGET"

if command -v dhcpd >/dev/null 2>&1; then
  dhcpd -t -cf /etc/dhcp/dhcpd.conf
fi

systemctl reload isc-dhcp-server && echo "dhcpd 已重载"
