#!/bin/bash
# PXE 管理平台自检脚本（只读检查，可随意修改或删除）
echo "===== 节点自检 $(date '+%F %T') ====="
echo "[主机]     $(hostname)"
echo "[内核]     $(uname -sr)"
echo "[IP 地址]  $(hostname -I 2>/dev/null || ip -4 addr show scope global | awk '{print $2}' | cut -d/ -f1 | tr '\n' ' ')"
echo "[负载]     $(awk '{print $1, $2, $3}' /proc/loadavg)"
echo "[内存]     $(free -h | awk '/^Mem:/{print $3" / "$2}')"
echo "[根分区]   $(df -h / | awk 'NR==2{print $4" 可用 / 共 "$2}')"
echo "[运行时长] $(uptime -p 2>/dev/null || uptime)"
echo "===== 自检完成 ====="
