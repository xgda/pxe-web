#!/usr/bin/env python3
# PXE 管理平台自检脚本（只读检查，可随意修改或删除）
import platform
import shutil
import socket
import time

print("===== 节点自检 %s =====" % time.strftime("%F %T"))
print("[主机]     %s" % socket.gethostname())
print("[系统]     %s" % platform.platform())
print("[内核]     %s" % platform.release())
total, used, free = shutil.disk_usage("/")
print("[根分区]   %.1f GB 可用 / %.1f GB" % (free / 2**30, total / 2**30))
print("===== 自检完成 =====")
