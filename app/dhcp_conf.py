"""解析 / 修改 dhcpd.conf（含 include 递归展开）里的 subnet 段。

可编辑字段：subnet / netmask / range / option routers /
option domain-name-servers / next-server
其余内容（含注释、其它语句）原样保留。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import time

import config
from hosts_store import reload_dhcp, validate_conf
from logger import log

SUBNET_RE = re.compile(r"\bsubnet\s+([0-9.]+)\s+netmask\s+([0-9.]+)\s*\{", re.I)
INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*;', re.M)

# 每个字段的匹配正则：(前缀)(原值)(;及行尾)
FIELD_RE = {
    "range": re.compile(r"(^[ \t]*range[ \t]+)(?:dynamic-bootp[ \t]+)?([^\n;]+)(;.*$)", re.M),
    "routers": re.compile(r"(^[ \t]*option[ \t]+routers[ \t]+)([^\n;]+)(;.*$)", re.M),
    "dns": re.compile(r"(^[ \t]*option[ \t]+domain-name-servers[ \t]+)([^\n;]+)(;.*$)", re.M),
    "next_server": re.compile(r"(^[ \t]*next-server[ \t]+)([^\n;]+)(;.*$)", re.M),
}

# 字段缺失时需要插入的完整语句名
FIELD_STMT = {
    "range": "range",
    "routers": "option routers",
    "dns": "option domain-name-servers",
    "next_server": "next-server",
}


# ---------------------------------------------------------------- 文件收集

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def collect_files(path: str | None = None, _seen: set | None = None) -> list[tuple[str, str]]:
    """返回 [(绝对路径, 内容)]，递归展开 include（支持通配符）。"""
    if _seen is None:
        _seen = set()
    root = os.path.abspath(path or config.DHCPD_CONF)
    if root in _seen or not os.path.exists(root):
        return []
    _seen.add(root)
    try:
        text = _read(root)
    except OSError:
        return []

    result = [(root, text)]
    base = os.path.dirname(root)
    for m in INCLUDE_RE.finditer(text):
        raw = m.group(1)
        candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
        for expanded in sorted(glob.glob(candidate)):
            result.extend(collect_files(expanded, _seen))
    return result


# ---------------------------------------------------------------- 解析

def _iter_blocks(text: str):
    pos, n = 0, len(text)
    while pos < n:
        m = SUBNET_RE.search(text, pos)
        if not m:
            return
        depth, k = 0, m.end() - 1
        while k < n:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= n:  # 括号不闭合，放弃
            return
        yield m, m.start(), k + 1
        pos = k + 1


def _grab(pattern: re.Pattern, block: str) -> str:
    m = pattern.search(block)
    return m.group(2).strip() if m else ""


def _split_range(value: str) -> tuple[str, str]:
    parts = value.split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def parse_subnets() -> dict:
    files = collect_files()
    items = []
    for path, text in files:
        for m, _start, _end in _iter_blocks(text):
            block = text[_start:_end]
            start_ip, end_ip = _split_range(_grab(FIELD_RE["range"], block))
            items.append(
                {
                    "subnet": m.group(1),
                    "netmask": m.group(2),
                    "range_start": start_ip,
                    "range_end": end_ip,
                    "routers": _grab(FIELD_RE["routers"], block),
                    "dns": _grab(FIELD_RE["dns"], block),
                    "next_server": _grab(FIELD_RE["next_server"], block),
                    "file": path,
                }
            )
    root = os.path.abspath(config.DHCPD_CONF)
    return {
        "items": items,
        "conf": config.DHCPD_CONF,
        "exists": os.path.exists(root),
        "size": os.path.getsize(root) if os.path.exists(root) else 0,
        "scanned": [f for f, _ in files],
    }


# ---------------------------------------------------------------- 写回

def _insert_before_close(block: str, line: str) -> str:
    lines = block.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if "}" in lines[i]:
            indent = re.match(r"[ \t]*", lines[i]).group(0)
            lines.insert(i, f"{indent}    {line}")
            break
    else:
        lines.append("    " + line)
    return "\n".join(lines)


def _set_field(block: str, key: str, statement: str) -> str:
    pattern = FIELD_RE[key]
    if pattern.search(block):
        return pattern.sub(lambda m: m.group(1) + statement + m.group(3), block, count=1)
    return _insert_before_close(block, f"{FIELD_STMT[key]} {statement};")


def _locate(original: str):
    for path, text in collect_files():
        for m, start, end in _iter_blocks(text):
            if m.group(1) == original:
                return path, text, start, end
    return None


def _backup_conf(path: str) -> str | None:
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(config.BACKUP_DIR, ts)
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, os.path.basename(path) + "." + str(abs(hash(path)) % 10000))
        shutil.copy2(path, target)
        return target
    except Exception:  # noqa: BLE001
        return None


def update_subnet(original: str, data: dict) -> dict:
    """original：要修改的 subnet 地址；data：新值字典。"""
    root = os.path.abspath(config.DHCPD_CONF)
    if not os.path.exists(root):
        raise ValueError(f"配置文件不存在：{root}")

    found = _locate(original)
    if found is None:
        scanned = [f for f, _ in collect_files()]
        raise ValueError(
            f"未找到 subnet {original}。已扫描文件：{', '.join(scanned) or '（无）'}"
        )

    path, text, start, end = found
    block = text[start:end]

    new_block = re.sub(
        r"subnet\s+[0-9.]+\s+netmask\s+[0-9.]+",
        f"subnet {data['subnet']} netmask {data['netmask']}",
        block,
        count=1,
    )
    rng = f"{data.get('range_start','').strip()} {data.get('range_end','').strip()}".strip()
    if rng:
        new_block = _set_field(new_block, "range", rng)
    if data.get("routers", "").strip():
        new_block = _set_field(new_block, "routers", data["routers"].strip())
    if data.get("dns", "").strip():
        new_block = _set_field(new_block, "dns", data["dns"].strip())
    if data.get("next_server", "").strip():
        new_block = _set_field(new_block, "next_server", data["next_server"].strip())

    if new_block == block:
        # 内容没变就不要写盘，避免产生无意义的备份与重载
        return {"ok": True, "changed": False, "reload_ok": True, "message": "配置无变化，未写入"}

    new_text = text[:start] + new_block + text[end:]

    backup = _backup_conf(path)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"写入失败：{exc}"}

    ok, out = validate_conf()
    if not ok:
        if backup:
            shutil.copy2(backup, path)
        return {"ok": False, "message": "dhcpd 校验失败，已自动回滚", "detail": out}

    rel_ok, rel_out = reload_dhcp()
    log.info("更新 subnet %s（文件 %s）-> %s/%s", original, path, data["subnet"], data["netmask"])
    return {
        "ok": True,
        "reload_ok": rel_ok,
        "message": "子网配置已更新并重载" if rel_ok else "已更新，但重载失败",
        "detail": out,
        "reload": rel_out,
        "file": path,
        "backup": backup or "",
    }
