"""全局配置（全部可用环境变量覆盖，便于在 PXE 服务器上直接部署）。"""
from __future__ import annotations

import os
import shlex

APP_VERSION = "v1.9.11"

# 项目根目录（/opt/pxe-web）：下面几个默认路径都基于它，
# 这样 tftpboot/ 与 var/www/html/ 跟着项目走，不用再配绝对路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TFTPBOOT = os.path.join(BASE_DIR, "tftpboot")
DEFAULT_WWW = os.path.join(BASE_DIR, "var", "www", "html")

# 部署任务：脚本目录（只认该目录下 .sh / .py 文件，不递归子目录）
SCRIPTS_DIR = os.getenv("PXE_SCRIPTS_DIR", os.path.join(BASE_DIR, "scripts"))
TASK_TIMEOUT = int(os.getenv("PXE_TASK_TIMEOUT", "300"))
# 脚本执行结果落盘：scripts/log/<节点SN>/<脚本名_时间>.log
TASK_LOG_DIR = os.getenv("PXE_TASK_LOG_DIR", os.path.join(SCRIPTS_DIR, "log"))
# 批量部署时同时下发的最大节点数（避免几十台一起拉 ISO 把 HTTP 服务器打满）
TASK_CONCURRENCY = int(os.getenv("PXE_TASK_CONCURRENCY", "5"))
# 每批之间额外等待的秒数（错峰用，0 表示不等）
TASK_BATCH_DELAY = int(os.getenv("PXE_TASK_BATCH_DELAY", "0"))

# ---------- 日志 ----------
LOG_FILE = os.getenv("PXE_LOG_FILE", "/var/log/pxe-web/app.log")
LOG_LEVEL = os.getenv("PXE_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("PXE_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("PXE_LOG_BACKUPS", "3"))

# ---------- 登录 ----------
ADMIN_USER = os.getenv("PXE_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("PXE_ADMIN_PASS", "admin")
SECRET_KEY = os.getenv("PXE_SECRET_KEY", "pxe-web-default-secret-please-change")
TOKEN_TTL = int(os.getenv("PXE_TOKEN_TTL", "28800"))  # 8 小时

# ---------- dhcpd / 节点映射 ----------
HOSTS_DIR = os.getenv("PXE_HOSTS_DIR", "/etc/dhcp/hosts")
HOSTS_GLOB = os.getenv("PXE_HOSTS_GLOB", os.path.join(HOSTS_DIR, "*.conf"))
# dhcpd.conf include 的索引文件；新增/删除节点时自动在里面增删 include 行
HOSTS_INDEX = os.getenv("PXE_HOSTS_INDEX", "/etc/dhcp/hosts.conf")
DHCPD_CONF = os.getenv("PXE_DHCPD_CONF", "/etc/dhcp/dhcpd.conf")
BACKUP_DIR = os.getenv("PXE_BACKUP_DIR", "/var/backups/pxe-web")

# 写回后是否执行 dhcpd -t 校验（本地调试可设 PXE_DRY_RUN=1 关闭）
DRY_RUN = os.getenv("PXE_DRY_RUN", "0") in ("1", "true", "yes")

_validate_env = os.getenv("PXE_VALIDATE_CMD")
VALIDATE_CMD = (
    shlex.split(_validate_env)
    if _validate_env
    else ["dhcpd", "-t", "-cf", DHCPD_CONF]
)

_reload_env = os.getenv("PXE_RELOAD_CMD")
RELOAD_CMD = (
    shlex.split(_reload_env)
    if _reload_env
    else ["systemctl", "reload", "isc-dhcp-server"]
)

# ---------- 镜像目录（HTTP 安装源根目录） ----------
# 把 HTTP 服务器的 DocumentRoot 指到项目里的 var/www/html 即可
WWW_ROOT = os.getenv("PXE_WWW_ROOT", DEFAULT_WWW)

# ---------- 镜像 ----------
# 布局：  <WWW>/<架构>/<镜像名>/             新系统类型自己上传的
#         <WWW>/UKI/<架构>/<系统类型>/         内置 vmlinuz / initrd 的现成类型
IMAGES_DIR = os.getenv("PXE_IMAGES_DIR", DEFAULT_WWW)
AUTOINSTALL_DIRNAME = os.getenv("PXE_AUTOINSTALL_DIR", "autoinstall")
# 内置内核目录名与位置。想用旧的「<架构>/UKI/<类型>/」布局，把
# PXE_UKI_LAYOUT 设成 "{www}/{arch}/UKI" 即可
UKI_FOLDER = os.getenv("PXE_UKI_FOLDER", "UKI")
UKI_LAYOUT = os.getenv("PXE_UKI_LAYOUT", "{www}/" + UKI_FOLDER + "/{arch}")
# 下拉里固定放在最末尾的"自己起名字"选项
NEW_TYPE_LABEL = os.getenv("PXE_NEW_TYPE_LABEL", "新系统类型")

# URL 路径模板：
#   UKI 类型（已有）：{arch}/UKI/{name}    →  /var/www/html/UKI/x86/ubuntu-24.04.4-desktop/
#   新类型（自己起名）：{arch}/{name}      →  /var/www/html/x86/ubuntu-desk-25/
URL_PATH_TPL = os.getenv("PXE_URL_PATH_TPL", "{arch}/{name}")
UKI_URL_PATH_TPL = os.getenv("PXE_UKI_URL_PATH_TPL", "UKI/{arch}/{name}")

# ---------- iPXE / GRUB 菜单 ----------
BOOT_IPXE = os.getenv("PXE_BOOT_IPXE", os.path.join(DEFAULT_TFTPBOOT, "boot.ipxe"))
# ARM 架构走 GRUB（UEFI HTTP boot），菜单项写在这个文件里
GRUB_CFG = os.getenv("PXE_GRUB_CFG", os.path.join(DEFAULT_TFTPBOOT, "grub.cfg"))

# 兜底菜单项名称。默认**不自动添加** menuentry，只识别文件里已有的那个
# （兼容空格 / 连字符 / 下划线写法）；确实想让平台自动补，设 PXE_GRUB_FALLBACK_ADD=1
GRUB_FALLBACK = os.getenv("PXE_GRUB_FALLBACK", "Boot from next volume")
GRUB_FALLBACK_ADD = os.getenv("PXE_GRUB_FALLBACK_ADD", "0") in ("1", "true", "yes")

# 节点发现：dhcpd 租约文件（不存在时自动探测常见路径）
LEASES_FILE = os.getenv("PXE_LEASES_FILE", "/var/lib/dhcp/dhcpd.leases")

# 节点发现：判断"是否真的走了 PXE 启动"用的 tftpd 日志。
# ISC 的 dhcpd.leases **不记录** filename，所以改为读 syslog 里 tftpd 的 RRQ 记录，
# 按「IP + 租约有效期」关联（tftpd 只在客户端真的来取引导文件时才打日志）。
SYSLOG_FILE = os.getenv("PXE_SYSLOG_FILE", "/var/log/syslog")
# 只读文件尾部，避免 syslog 太大拖慢接口
SYSLOG_TAIL_BYTES = int(os.getenv("PXE_SYSLOG_TAIL_BYTES", str(8 * 1024 * 1024)))
# 时间容差（分钟）：向租约起止两端各放宽这么多，兼容服务器与 syslog 的时钟偏差
TFTP_GRACE_MIN = int(os.getenv("PXE_TFTP_GRACE_MIN", "10"))
# 租约里既没有 starts 也没有 ends 时，往回看多少小时
TFTP_FALLBACK_HOURS = int(os.getenv("PXE_TFTP_FALLBACK_HOURS", "24"))
# 是否一并把轮转出来的旧日志（syslog.1 / messages.1）也扫一遍，
# 长租约(client 几天没续)的 TFTP 记录往往落在上一个文件里
SYSLOG_ROTATED = int(os.getenv("PXE_SYSLOG_ROTATED", "1"))

# 在线释放 DHCP 租约用的 OMAPI（omshell）；密钥留空则禁用
OMAPI_SERVER = os.getenv("PXE_OMAPI_SERVER", "127.0.0.1")
OMAPI_PORT = int(os.getenv("PXE_OMAPI_PORT", "7911"))
OMAPI_KEY_NAME = os.getenv("PXE_OMAPI_KEY_NAME", "omapi_key")
OMAPI_KEY = os.getenv("PXE_OMAPI_KEY", "")
# boot.ipxe / grub.cfg 里读不到菜单项时的兜底清单。
# 默认**留空**：平台里没有镜像，就不该凭空给出几个假选项误导人。
# 确实想要固定清单时用逗号分隔配置，例如
#   PXE_DEFAULT_SYSTEMS="ubuntu-24.04.5-server,dgxos-7.5.0"
DEFAULT_SYSTEMS = [
    s.strip() for s in os.getenv("PXE_DEFAULT_SYSTEMS", "").split(",") if s.strip()
]

# ---------- SSH ----------
SSH_PORT = int(os.getenv("PXE_SSH_PORT", "22"))
SSH_USER = os.getenv("PXE_SSH_USER", "root")
SSH_TIMEOUT = int(os.getenv("PXE_SSH_TIMEOUT", "10"))
# 目标节点只允许 publickey 时配置私钥路径；密码留空则自动改用密钥登录
SSH_KEY_FILE = os.getenv("PXE_SSH_KEY_FILE", "/root/.ssh/id_rsa")

# ---------- 状态探测 ----------
# 离线节点每次都要等满超时，节点一多列表接口就被拖到秒级，
# 所以探测结果按 IP 缓存 PROBE_CACHE_TTL 秒；想每次都实时探测就设成 0
PROBE_TIMEOUT = float(os.getenv("PXE_PROBE_TIMEOUT", "0.6"))
PROBE_CACHE_TTL = int(os.getenv("PXE_PROBE_CACHE_TTL", "20"))
ENABLE_PING = os.getenv("PXE_ENABLE_PING", "1") in ("1", "true", "yes")
# 节点发现：syslog 解析结果缓存秒数（文件可能好几 MB，逐行扫不便宜）
DISCOVERY_CACHE_TTL = int(os.getenv("PXE_DISCOVERY_CACHE_TTL", "15"))
