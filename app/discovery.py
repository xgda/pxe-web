"""节点发现：从 dhcpd 租约文件里找出"已拿到 IP 但没有选中安装系统"的主机。

判断"是否拿到过引导文件"的两条数据来源（按顺序）：
    1. **syslog 里 tftpd 的传输记录**（主）：ISC 的 dhcpd.leases 不写 filename，
       但客户端真来取引导文件时 tftpd 会往 /var/log/syslog 打日志，例如
           in.tftpd[1234]: RRQ from 192.168.1.50 filename grubnetx64.efi
       按「IP + 时间落在租约有效期内（两端各放宽 PXE_TFTP_GRACE_MIN 分钟）」关联；
    2. **dhcpd.leases 里的 filename 字段**（备）：部分发行版/配置会记录，有就直接用。

再判断登记情况：
        - MAC/IP 都没登记            → 未登记（停在兜底菜单）
        - 已登记但没写 ipxe-menu-item → 已登记未选系统
        - 已登记且指定了系统          → 正常，默认排在最后
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import threading
import time

import config
from hosts_store import load_nodes
from logger import log

LEASE_RE = re.compile(r"^lease\s+([0-9.]+)\s*\{", re.M)
# dhcpd.leases 里引导文件有几种写法，都兼容一下
FILENAME_RES = (
    re.compile(r'filename\s+"([^"]*)"\s*;', re.I),
    re.compile(r'option\s+filename\s+"([^"]*)"\s*;', re.I),
    re.compile(r'set\s+filename\s*=\s*"([^"]*)"', re.I),
)
NEXTSERVER_RE = re.compile(r"(?:option\s+)?next-server\s+([0-9.]+)\s*;", re.I)
CANDIDATE_LEASES = (
    "/var/lib/dhcp/dhcpd.leases",
    "/var/lib/dhcpd/dhcpd.leases",
    "/var/db/dhcpd.leases",
)


def leases_path() -> str:
    if config.LEASES_FILE and os.path.exists(config.LEASES_FILE):
        return config.LEASES_FILE
    for path in CANDIDATE_LEASES:
        if os.path.exists(path):
            return path
    return config.LEASES_FILE


# ------------------------------------------------------------------ syslog / TFTP

CANDIDATE_SYSLOGS = (
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/daemon.log",
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}

# 'Sep 10 10:23:45'
SYSLOG_TS_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s")
# '2026-09-10 10:23:45' / '2026-09-10T10:23:45'（部分 rsyslog 模板带年份）
ISO_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

_IP = r"(\d{1,3}(?:\.\d{1,3}){3})"
# 日志行里"客户端 IP"的常见位置，按可靠程度从高到低；
# 全都没命中时才退化为"行内第一个 IP"（有些 syslog 模板会在行首带服务器地址）
CLIENT_IP_PATTERNS = (
    re.compile(rf"\bRRQ\s+from\s+{_IP}", re.I),
    re.compile(rf"\bWRQ\s+from\s+{_IP}", re.I),
    re.compile(rf"\bclient\s+{_IP}", re.I),
    re.compile(rf"to\s+{_IP}\s*[:\s]", re.I),
    re.compile(rf"from\s+{_IP}", re.I),
)


def _client_ip(line: str) -> str:
    for pattern in CLIENT_IP_PATTERNS:
        found = pattern.search(line)
        if found:
            return found.group(1)
    found = IPV4_RE.search(line)
    return found.group(1) if found else ""

# 各家 tftpd 的日志里引导文件的写法，按顺序试
FILENAME_PATTERNS = (
    re.compile(r'\bfilename\s*[:=]?\s*"?([^\s",;]+)', re.I),
    re.compile(r"\bfinished\s+([^\s,;]+)", re.I),
    re.compile(r"\bsent\s+([^\s,;]+)\s+to\b", re.I),
    re.compile(r"\bServing\s+([^\s,;]+)\s+to\b", re.I),
    re.compile(r"\bRRQ\s+from\s+\S+\s+for\s+([^\s,;]+)", re.I),
)
# 上面都匹配不到时的兜底：长得像引导文件的 token
BOOTFILE_RE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:efi|kpxe|pxe|ipxe|0|bin|img|cfg|txt))", re.I
)


def syslog_path() -> str:
    if config.SYSLOG_FILE and os.path.exists(config.SYSLOG_FILE):
        return config.SYSLOG_FILE
    for path in CANDIDATE_SYSLOGS:
        if os.path.exists(path):
            return path
    return config.SYSLOG_FILE or ""


def syslog_files() -> list[str]:
    """待扫的日志：主文件 + 可选的 .1 轮转文件（都存在的才返回）。"""
    primary = syslog_path()
    if not primary:
        return []
    files = [primary]
    if config.SYSLOG_ROTATED:
        for suffix in (".1",):
            candidate = primary + suffix
            if os.path.exists(candidate):
                files.append(candidate)
    return files


def _read_tail(path: str, max_bytes: int) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢掉可能被截断的半行
                raw = fh.read()
            else:
                raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", "ignore").splitlines()


def _syslog_time(line: str, now: datetime.datetime) -> datetime.datetime | None:
    """取出行首时间戳。syslog 常见格式没有年份，按当前年补齐；跨年时自动减一年。"""
    iso = ISO_TS_RE.match(line)
    if iso:
        try:
            return datetime.datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                int(iso.group(4)), int(iso.group(5)), int(iso.group(6)),
            )
        except ValueError:
            return None

    m = SYSLOG_TS_RE.match(line)
    month = _MONTHS.get(m.group(1)) if m else None
    if not month:
        return None
    try:
        dt = datetime.datetime(
            now.year, month, int(m.group(2)),
            int(m.group(3)), int(m.group(4)), int(m.group(5)),
        )
    except ValueError:
        return None
    if dt - now > datetime.timedelta(days=1):  # 明显在未来 → 是去年的
        dt = dt.replace(year=now.year - 1)
    return dt


def parse_tftp_events(now: datetime.datetime | None = None) -> tuple[dict[str, list[dict]], str]:
    """从 syslog 里解析 tftpd 的传输记录，返回 {ip: [{ts, filename, line}]}。"""
    now = now or datetime.datetime.now()
    path = syslog_path()
    files = syslog_files()
    if not files:
        return {}, path

    events: dict[str, list[dict]] = {}
    for path in syslog_files():
        for line in _read_tail(path, config.SYSLOG_TAIL_BYTES):
            lowered = line.lower()
            if "tftp" not in lowered and "rrq" not in lowered:
                continue
            ts = _syslog_time(line, now)
            ip = _client_ip(line)
            if ts is None or not ip:
                continue

            filename = ""
            for pattern in FILENAME_PATTERNS:
                found = pattern.search(line)
                if found:
                    filename = found.group(1).strip('"')
                    break
            if not filename:
                found = BOOTFILE_RE.search(line)
                filename = found.group(1).strip('"') if found else ""
            # 只有连接、没取到文件的记录也算 PXE，文件名留空以便界面区分
            events.setdefault(ip, []).append(
                {"ts": ts, "filename": os.path.basename(filename), "line": line.strip()}
            )

    for items in events.values():
        items.sort(key=lambda e: e["ts"])
    return events, path


def _in_window(ts, start, end, now, grace, fallback_hours):
    """时间戳是否落在这条租约的有效期内（两端各放宽 grace 分钟）。"""
    if start is None and end is None:
        return ts >= now - datetime.timedelta(hours=fallback_hours)
    lo = (start or end) - datetime.timedelta(minutes=grace)
    hi = (end or start or now)
    hi = min(hi, now) + datetime.timedelta(minutes=grace)
    return lo <= ts <= hi


def attach_bootfile(
    leases: list[dict],
    events: dict[str, list[dict]],
    now: datetime.datetime | None = None,
) -> None:
    """给每条租约补 filename / pxe / boot_source / boot_time。

    leases 里的 starts / ends 需要在调用前保留（parse_active_leases 会 pop 掉 _ends，
    这里用的是 _starts / _ends 原始 datetime，由调用方塞进来）。
    """
    now = now or datetime.datetime.now()
    grace = max(0, config.TFTP_GRACE_MIN)
    fallback_hours = max(1, config.TFTP_FALLBACK_HOURS)

    for lease in leases:
        if lease.get("filename"):
            lease["boot_source"] = "leases"
            lease["boot_time"] = lease.get("starts") or ""
            continue

        start = lease.get("_starts")
        end = lease.get("_ends")
        hit = None
        for event in events.get(lease["ip"], []):
            if _in_window(event["ts"], start, end, now, grace, fallback_hours):
                hit = event  # events 已按时间排序，取窗口内最后一条
        if hit is None:
            lease["boot_source"] = ""
            lease["boot_time"] = ""
            continue
        lease["filename"] = hit["filename"]
        lease["pxe"] = True
        lease["boot_source"] = "syslog"
        lease["boot_time"] = hit["ts"].strftime("%m-%d %H:%M:%S")


# db-time-format local 时写成 epoch 秒：`1768520400; # Mon Jan 15 22:00:00 2026`
_EPOCH_RE = re.compile(r"^\s*(\d{9,})")
_DB_LOCAL_RE = re.compile(r"^\s*db-time-format\s+local\s*;", re.I | re.M)

# 租约文件用的是 UTC 还是本地时间，探测一次缓存起来（按配置文件 mtime 失效）
_TZ_CACHE: dict = {"key": None, "local": False}


def leases_time_is_local() -> bool:
    """dhcpd.leases 里的日期是 UTC 还是本地时间。

    ISC 官方 dhcpd.leases(5) 原话：「Lease times are specified in Universal
    Coordinated Time (UTC), not in the local time zone」。只有 dhcpd.conf 里
    显式写了 `db-time-format local;` 才是本地时间（那时写成 epoch 秒）。
    东八区上要是按本地时间解释 UTC，所有租约都会"提前 8 小时到期"，
    节点发现会一条都不剩。
    """
    forced = (os.getenv("PXE_LEASES_TZ", "") or "").strip().lower()
    conf = getattr(config, "DHCPD_CONF", "") or ""
    try:
        mtime = int(os.path.getmtime(conf)) if conf and os.path.exists(conf) else 0
    except OSError:
        mtime = 0

    key = (forced, conf, mtime)
    if _TZ_CACHE["key"] == key:
        return _TZ_CACHE["local"]

    local = False
    if forced in ("local", "localtime"):
        local = True
    elif forced not in ("utc", "gmt") and conf and os.path.exists(conf):
        try:
            with open(conf, "r", encoding="utf-8", errors="ignore") as fh:
                local = bool(_DB_LOCAL_RE.search(fh.read()))
        except OSError:
            local = False

    _TZ_CACHE.update({"key": key, "local": local})
    return local


def _to_local(dt: "datetime.datetime | None") -> "datetime.datetime | None":
    """把租约文件里的时间戳换成**本地 naive datetime**。

    syslog 里的时间戳是本地时间，两边必须对齐，"租约有效期窗口"才判定得准。
    """
    if dt is None:
        return None
    if leases_time_is_local():
        return dt
    return dt.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)


def _parse_time(value: str) -> datetime.datetime | None:
    """解析 dhcpd.leases 的日期，统一返回本地 naive datetime。

    - `3 2026/09/08 10:00:00`  → 星期字段丢弃，按 UTC 解释（dhcpd 默认）
    - `1768520400; # Mon ...`  → epoch 秒，本身就是本地时间（db-time-format local）
    """
    value = (value or "").strip()
    if not value:
        return None

    epoch = _EPOCH_RE.match(value)
    if epoch:
        try:
            return datetime.datetime.fromtimestamp(int(epoch.group(1)))
        except (ValueError, OSError, OverflowError):
            return None

    parts = value.split()
    if len(parts) >= 3:
        value = f"{parts[1]} {parts[2]}"
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return _to_local(datetime.datetime.strptime(value, fmt))
        except ValueError:
            continue
    return None


# 上一次扫描的统计（排查用：租约到底是被判过期、还是被判非 active）
LAST_SCAN: dict = {"total": 0, "expired": 0, "inactive": 0, "kept": 0}


def parse_active_leases() -> tuple[list[dict], str]:
    path = leases_path()
    if not path or not os.path.exists(path):
        return [], path

    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        text = fh.read()

    now = datetime.datetime.now()
    latest: dict[str, dict] = {}
    scan = {"total": 0, "expired": 0, "inactive": 0, "kept": 0}

    for m in LEASE_RE.finditer(text):
        scan["total"] += 1
        depth, k = 0, m.end() - 1
        while k < len(text):
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        block = text[m.start() : k + 1]

        def grab(pattern: str) -> str:
            found = re.search(pattern, block, re.I)
            return found.group(1).strip() if found else ""

        ends = _parse_time(grab(r"ends\s+([^;]+);"))
        starts = _parse_time(grab(r"starts\s+([^;]+);"))
        if ends and ends < now:  # 租约已过期
            scan["expired"] += 1
            continue
        state = grab(r"binding\s+state\s+([^;]+);")
        if state and state not in ("active", "backup"):
            scan["inactive"] += 1
            continue
        scan["kept"] += 1

        mac = grab(r"hardware\s+ethernet\s+([0-9A-Fa-f:]+)\s*;").lower()
        hostname = grab(r'client-hostname\s+"([^"]*)"')
        filename = ""
        for pattern in FILENAME_RES:
            found = pattern.search(block)
            if found and found.group(1).strip():
                filename = found.group(1).strip()
                break
        record = {
            "ip": m.group(1),
            "mac": mac,
            "mac_upper": mac.upper(),
            "hostname": hostname,
            "filename": filename,
            # 拿到引导文件 = 真的走了 PXE 启动
            "pxe": bool(filename),
            "next_server": (NEXTSERVER_RE.search(block).group(1) if NEXTSERVER_RE.search(block) else ""),
            "binding": state or "active",
            "starts": starts.strftime("%m-%d %H:%M") if starts else "",
            "ends": ends.strftime("%m-%d %H:%M") if ends else "",
            # 内部字段：给 syslog 关联用，discover() 用完会清掉
            "_starts": starts,
            "_ends": ends,
        }
        key = mac or record["ip"]
        old = latest.get(key)
        if old is None or (ends and old["_ends"] and ends > old["_ends"]):
            latest[key] = record

    items = list(latest.values())
    items.sort(key=lambda r: r["ip"])
    scan["kept"] = len(items)
    LAST_SCAN.update(scan)
    return items, path


OMAPI_HOWTO = (
    "未配置 OMAPI 密钥，无法在线释放租约。\n\n"
    "在 dhcpd.conf 里开启 OMAPI：\n"
    "    omapi-port 7911;\n"
    "    key omapi_key { algorithm hmac-md5; secret \"<自定义密钥>\"; };\n"
    "    omapi-key omapi_key;\n"
    "重启 isc-dhcp-server，然后给平台配上同样的密钥：\n"
    "    Environment=\"PXE_OMAPI_KEY=<同一个密钥>\"\n"
    "再 systemctl restart pxe-web。\n\n"
    "临时办法：在服务器上停 dhcpd → 删除 dhcpd.leases 中对应的 lease 段 → 启动 dhcpd。"
)


def release_lease(ip: str, mac: str = "") -> tuple[bool, str]:
    """通过 OMAPI（omshell）释放指定 IP 的 DHCP 租约。"""
    if not ip:
        return False, "缺少 IP"
    if not config.OMAPI_KEY:
        return False, OMAPI_HOWTO

    script = "\n".join(
        [
            f"server {config.OMAPI_SERVER}",
            f"port {config.OMAPI_PORT}",
            f'key {config.OMAPI_KEY_NAME} "{config.OMAPI_KEY}"',
            "connect",
            "new lease",
            f"set ip-address = {ip}",
            "open",
            "remove",
            "",
        ]
    )
    try:
        proc = subprocess.run(
            ["omshell"], input=script, capture_output=True, text=True, timeout=20
        )
    except FileNotFoundError:
        return False, "服务器上找不到 omshell 命令（Debian/Ubuntu: apt install isc-dhcp-server）"
    except Exception as exc:  # noqa: BLE001
        return False, f"执行 omshell 失败：{exc}"

    out = (proc.stdout + proc.stderr).strip()
    lowered = out.lower()
    failed = proc.returncode != 0 or any(
        key in lowered for key in ("can't", "cannot", "no such", "not connected", "error")
    )
    log.info("释放租约 ip=%s mac=%s → %s", ip, mac, "成功" if not failed else "失败")
    return (not failed), (out or "omshell 无输出")


_SYSLOG_CACHE = {"at": 0.0, "events": {}, "path": ""}
_SYSLOG_LOCK = threading.Lock()


def _cached_tftp_events(now, force: bool = False):
    """syslog 动辄几 MB，逐行扫一遍不便宜，缓存 DISCOVERY_CACHE_TTL 秒。"""
    ttl = max(0, config.DISCOVERY_CACHE_TTL)
    with _SYSLOG_LOCK:
        if not force and ttl and (time.time() - _SYSLOG_CACHE["at"]) < ttl:
            return _SYSLOG_CACHE["events"], _SYSLOG_CACHE["path"]
    events, path = parse_tftp_events(now)
    with _SYSLOG_LOCK:
        _SYSLOG_CACHE.update({"at": time.time(), "events": events, "path": path})
    return events, path


def discover(pxe_only: bool = True, pending_only: bool = False, fresh: bool = False) -> dict:
    """pxe_only    默认 True：只看拿到过 filename（走了 PXE 启动）的租约
    pending_only        False：只看还没选定安装系统的
    fresh               True：跳过 syslog 解析缓存，重新扫一遍
    """
    now = datetime.datetime.now()
    leases, path = parse_active_leases()

    # ISC 的 dhcpd.leases 不记录 filename，主数据源改成 syslog 里 tftpd 的传输记录
    events, syslog = _cached_tftp_events(now, force=fresh)
    attach_bootfile(leases, events, now)

    nodes = load_nodes()
    by_mac = {n.mac.lower(): n for n in nodes if n.mac}
    by_ip = {n.ip: n for n in nodes if n.ip}

    items = []
    for lease in leases:
        node = by_mac.get(lease["mac"]) or by_ip.get(lease["ip"])
        if node and node.system:
            status, pending = "已选择系统", False
        elif node:
            status, pending = "已登记未选系统", True
        else:
            status, pending = "未登记", True
        items.append(
            {
                **lease,
                "node": node.name if node else "",
                "arch": node.arch if node else "",
                "system": node.system if node else "",
                "status": status,
                "pending": pending,
            }
        )

    stats = {
        "leases_total": len(items),
        "pxe": sum(1 for i in items if i["pxe"]),
        "pxe_syslog": sum(1 for i in items if i.get("boot_source") == "syslog"),
        "pxe_leases": sum(1 for i in items if i.get("boot_source") == "leases"),
        "pending": sum(1 for i in items if i["pending"]),
        "pending_pxe": sum(1 for i in items if i["pxe"] and i["pending"]),
    }

    if pxe_only:
        items = [i for i in items if i["pxe"]]
    if pending_only:
        items = [i for i in items if i["pending"]]

    for item in items:
        item.pop("_starts", None)
        item.pop("_ends", None)

    items.sort(key=lambda r: (not r["pending"], r["ip"]))
    return {
        **stats,
        "items": items,
        "total": len(items),
        # 待处理数量跟随当前筛选口径，避免界面上和实际行数对不上
        "pending": stats["pending_pxe"] if pxe_only else stats["pending"],
        "leases": path,
        "exists": bool(path) and os.path.exists(path),
        "leases_tz": "local" if leases_time_is_local() else "utc",
        "scan": dict(LAST_SCAN),
        "pxe_only": pxe_only,
        "syslog": syslog,
        "syslog_exists": bool(syslog) and os.path.exists(syslog),
        "tftp_events": sum(len(v) for v in events.values()),
        "grace_min": config.TFTP_GRACE_MIN,
    }
