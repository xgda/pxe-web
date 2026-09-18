"""解析 / 写回 ISC dhcpd 的 host 映射文件（/etc/dhcp/hosts/*.conf）。

支持读写的字段：
    host <名称> {
        hardware ethernet <MAC>;
        fixed-address <IP>;
        option ipxe-menu-item "<系统>";
    }
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import config

HOST_RE = re.compile(r"\bhost\s+([^\s{]+)\s*\{")
MAC_RE = re.compile(r"hardware\s+ethernet\s+([0-9A-Fa-f:]+)\s*;", re.I)
IP_RE = re.compile(r"fixed-address\s+([0-9.]+)\s*;", re.I)
SYS_RE = re.compile(r'option\s+ipxe-menu-item\s+"([^"]*)"\s*;', re.I)
ITEM_RE = re.compile(r"^\s*item\s+(?:--key\s+\S+\s+)?(\S+)")
# 架构写在 host 块上方的注释里：# arch: arm （dhcpd 不识别自定义 option，用注释最安全）
ARCH_RE = re.compile(r"^[ \t]*#[ \t]*arch[ \t]*:[ \t]*(\S+)", re.I | re.M)
ARCHES = ("x86", "arm")
DEFAULT_ARCH = "x86"


@dataclass
class Node:
    name: str
    mac: Optional[str] = None
    ip: Optional[str] = None
    system: Optional[str] = None
    arch: str = DEFAULT_ARCH
    file: str = ""
    start: int = 0
    end: int = 0
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mac": self.mac or "",
            "ip": self.ip or "",
            "system": self.system or "",
            "arch": self.arch or DEFAULT_ARCH,
            "file": self.file,
        }


# ---------------------------------------------------------------- 解析

def _iter_blocks(text: str):
    """产出 (name, start, end)，end 指向匹配右花括号之后。"""
    pos, n = 0, len(text)
    while pos < n:
        m = HOST_RE.search(text, pos)
        if not m:
            return
        depth, k = 0, m.end() - 1
        while k < n:
            ch = text[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= n:  # 括号不闭合，放弃后续
            return
        yield m.group(1), m.start(), k + 1
        pos = k + 1


def load_nodes() -> list[Node]:
    nodes: list[Node] = []
    for path in sorted(glob.glob(config.HOSTS_GLOB)):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for name, start, end in _iter_blocks(text):
            raw = text[start:end]
            # 架构注释写在 host 块上方，取离本块最近的一条
            prefix = text[max(0, start - 300) : start]
            arch_marks = ARCH_RE.findall(prefix)
            arch = (arch_marks[-1].lower() if arch_marks else DEFAULT_ARCH)
            if arch not in ARCHES:
                arch = DEFAULT_ARCH
            nodes.append(
                Node(
                    name=name,
                    mac=(MAC_RE.search(raw).group(1).upper() if MAC_RE.search(raw) else None),
                    ip=(IP_RE.search(raw).group(1) if IP_RE.search(raw) else None),
                    system=(SYS_RE.search(raw).group(1) if SYS_RE.search(raw) else None),
                    arch=arch,
                    file=path,
                    start=start,
                    end=end,
                    raw=raw,
                )
            )
    return nodes


def find_node(name: str) -> Optional[Node]:
    for n in load_nodes():
        if n.name == name:
            return n
    return None


def load_systems(arch: str = "x86") -> tuple[list[str], str]:
    """x86 → 读 boot.ipxe 的 item；arm → 读 grub.cfg 的 menuentry。"""
    source = config.GRUB_CFG if arch == "arm" else config.BOOT_IPXE
    items: list[str] = []

    import images as images_mod

    if arch == "arm":
        items = images_mod.list_grub_entries()
    elif os.path.exists(config.BOOT_IPXE):
        with open(config.BOOT_IPXE, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = ITEM_RE.match(line)
                # shell / reboot / cancel 属于内部条目，不展示给用户
                if m and m.group(1) not in ("shell", "reboot", "cancel"):
                    if m.group(1) not in items:
                        items.append(m.group(1))
    # 兜底项（Boot from next volume 等）只是"没匹配到系统时的出口"，对使用者不可见
    items = [i for i in items if not images_mod.is_fallback_name(i)]
    # 一个菜单项都没有时不再编造假选项：
    # 只在管理员用 PXE_DEFAULT_SYSTEMS 显式配了兜底清单时才补上。
    if not items:
        items = list(config.DEFAULT_SYSTEMS)
    return items, source


# ---------------------------------------------------------------- 写回

def render_block(
    name: str, mac: str = "", ip: str = "", system: str = "", arch: str = DEFAULT_ARCH
) -> str:
    if arch not in ARCHES:
        arch = DEFAULT_ARCH
    lines = [f"# arch: {arch}", f"host {name} {{"]
    if mac:
        lines.append(f"    hardware ethernet {mac.upper()};")
    if ip:
        lines.append(f"    fixed-address {ip};")
    if system:
        lines.append(f'    option ipxe-menu-item "{system}";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _leading_comments(text: str, upto: Optional[int] = None) -> str:
    head = text[:upto] if upto is not None else text
    keep = []
    for line in head.splitlines():
        s = line.strip()
        if s == "" or s.startswith("#"):
            keep.append(line)
        else:
            break
    return "\n".join(keep).strip()


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _backup() -> Optional[str]:
    """备份 hosts/*.conf 与索引文件，返回备份目录；同时写入 _map.json 记录原始路径。"""
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(config.BACKUP_DIR, ts)
        os.makedirs(dest, exist_ok=True)
        mapping: dict[str, str] = {}
        for f in glob.glob(config.HOSTS_GLOB):
            name = os.path.basename(f)
            shutil.copy2(f, os.path.join(dest, name))
            mapping[name] = f
        if os.path.exists(config.HOSTS_INDEX):
            key = "index." + os.path.basename(config.HOSTS_INDEX)
            shutil.copy2(config.HOSTS_INDEX, os.path.join(dest, key))
            mapping[key] = config.HOSTS_INDEX
        with open(os.path.join(dest, "_map.json"), "w", encoding="utf-8") as fh:
            json.dump(mapping, fh)
        return dest
    except Exception:
        return None


def _restore(backup: Optional[str]) -> None:
    if not backup:
        return
    map_file = os.path.join(backup, "_map.json")
    if os.path.exists(map_file):
        try:
            with open(map_file, "r", encoding="utf-8") as fh:
                mapping = json.load(fh)
            for name, origin in mapping.items():
                src = os.path.join(backup, name)
                if os.path.exists(src):
                    shutil.copy2(src, origin)
            return
        except Exception:
            pass
    for f in glob.glob(os.path.join(backup, "*.conf")):
        shutil.copy2(f, config.HOSTS_DIR)


def validate_conf() -> tuple[bool, str]:
    if config.DRY_RUN:
        return True, "DRY_RUN 模式：已跳过 dhcpd -t 校验"
    try:
        cmd = [c.replace("{conf}", config.DHCPD_CONF) for c in config.VALIDATE_CMD]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-2000:]
    except FileNotFoundError:
        return True, f"未找到命令 {config.VALIDATE_CMD[0]}，跳过校验"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _run_cmd(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-2000:]
    except FileNotFoundError:
        return False, f"未找到命令：{cmd[0]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# 主命令失败时依次尝试的备用重载方式（不同发行版服务名不同）
RELOAD_FALLBACKS = [
    ["systemctl", "restart", "isc-dhcp-server"],
    ["service", "isc-dhcp-server", "restart"],
    ["systemctl", "restart", "dhcpd"],
    ["service", "dhcpd", "restart"],
]


def reload_dhcp() -> tuple[bool, str]:
    if config.DRY_RUN:
        return True, "DRY_RUN 模式：已跳过服务重载"

    cmds = [config.RELOAD_CMD] + [c for c in RELOAD_FALLBACKS if c != config.RELOAD_CMD]
    tried, last = [], ""
    for cmd in cmds:
        ok, out = _run_cmd(cmd)
        tried.append(" ".join(cmd) + " → " + ("成功" if ok else "失败"))
        if ok:
            return True, "\n".join(tried) + ("\n" + out if out else "")
        last = out
    return False, "\n".join(tried) + "\n" + last


def _commit(files: dict[str, Optional[str]]) -> dict:
    """files: {路径: 内容 or None(删除)}"""
    backup = _backup()
    try:
        os.makedirs(config.HOSTS_DIR, exist_ok=True)
        for path, content in files.items():
            if content is None:
                if not os.path.exists(path):
                    continue
                try:
                    os.remove(path)
                except Exception:
                    # 受权限/安全策略限制删不掉时，退化为清空为注释，保证 host 不再生效
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                            header = _leading_comments(fh.read())
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write((header + "\n" if header else "") + "# 节点已删除\n")
                    except Exception:
                        pass
            else:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"写入文件失败：{exc}", "detail": ""}

    ok, out = validate_conf()
    if not ok:
        _restore(backup)
        return {"ok": False, "message": "dhcpd 配置校验失败，已自动回滚", "detail": out}

    rel_ok, rel_out = reload_dhcp()
    return {
        "ok": True,
        "reload_ok": rel_ok,
        "message": "已保存并重载 dhcpd" if rel_ok else "已保存，但重载失败",
        "detail": out,
        "reload": rel_out,
        "backup": backup or "",
    }


INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*;\s*$')


def _index_modify(conf_path: str, action: str) -> Optional[str]:
    """在 hosts.conf 索引文件里增删 `include "<conf>"` 行。返回新内容；无变化返回 None。"""
    idx = config.HOSTS_INDEX
    if not idx:
        return None
    target = os.path.abspath(conf_path)
    existed = os.path.exists(idx)
    lines = []
    if existed:
        with open(idx, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()

    hit = [
        line for line in lines
        if INCLUDE_RE.match(line) and os.path.abspath(INCLUDE_RE.match(line).group(1)) == target
    ]
    if action == "add" and hit:
        return None
    if action == "remove" and not hit:
        return None

    if action == "remove":
        lines = [line for line in lines if line not in hit]
    else:
        lines.append(f'include "{target}";')
        if not existed:
            lines.insert(0, "# 由 PXE 管理平台生成，请勿手动修改（新增/删除节点会自动维护）")

    return "\n".join(lines).rstrip("\n") + "\n"


def save_node(
    name: str,
    mac: str = "",
    ip: str = "",
    system: str = "",
    arch: str = DEFAULT_ARCH,
) -> dict:
    old = find_node(name)
    target = os.path.join(config.HOSTS_DIR, f"{_safe_filename(name)}.conf")
    block = render_block(name, mac, ip, system, arch or DEFAULT_ARCH)
    files: dict[str, Optional[str]] = {}

    if old:
        with open(old.file, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        # 删掉本节点块上方的 "# arch: xxx" 注释：它不在块内，直接留下会变成
        # 后面那个节点的"最近一条 arch 注释"（load_nodes 往前看 300 字符），
        # 于是下一个节点的架构就被串错了
        stripped = _drop_trailing_arch(content[: old.start]) + content[old.end :]
        if os.path.abspath(old.file) == os.path.abspath(target):
            # 复用原有注释，但去掉 arch 标记（block 里会重新生成，避免重复）
            header = "\n".join(
                line
                for line in _leading_comments(content, old.start).splitlines()
                if not ARCH_RE.match(line)
            )
            files[target] = (header + "\n\n" if header else "") + block
        else:
            if not _iter_blocks_any(stripped):
                files[old.file] = None  # 原文件已无 host，删掉
            else:
                files[old.file] = stripped.lstrip("\n")
            files[target] = block
    else:
        files[target] = f"# 节点 {name}（由 PXE 管理平台生成）\n" + block

    # 同步维护索引文件里的 include 行
    new_index = _index_modify(target, "add")
    if new_index:
        files[config.HOSTS_INDEX] = new_index

    result = _commit(files)
    result["index"] = config.HOSTS_INDEX
    return result


def delete_node(name: str) -> dict:
    old = find_node(name)
    if not old:
        return {"ok": False, "message": f"节点 {name} 不存在"}
    with open(old.file, "r", encoding="utf-8", errors="ignore") as fh:
        content = fh.read()
    # 同 save_node：删块时一并去掉它上方的 arch 注释，别污染后面的节点
    stripped = _drop_trailing_arch(content[: old.start]) + content[old.end :]

    files: dict[str, Optional[str]] = {}
    if _iter_blocks_any(stripped):
        files[old.file] = stripped.lstrip("\n")
    else:
        files[old.file] = None

    new_index = _index_modify(old.file, "remove")
    if new_index:
        files[config.HOSTS_INDEX] = new_index

    result = _commit(files)
    result["index"] = config.HOSTS_INDEX
    return result


def _iter_blocks_any(text: str) -> bool:
    for _ in _iter_blocks(text):
        return True
    return False


def _drop_trailing_arch(head: str) -> str:
    """去掉 head 末尾连续出现的 `# arch: xxx` 注释行（只保留一个空行做分隔）。"""
    lines = head.split("\n")
    blanks = 0
    while len(lines) > blanks and lines[len(lines) - 1 - blanks].strip() == "":
        blanks += 1
    core = lines[: len(lines) - blanks]
    while core and ARCH_RE.match(core[-1] or ""):
        core.pop()
    return "\n".join(core + ([""] if blanks and core else []))


# ---------------------------------------------------------------- 状态探测

async def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _ping(ip: str) -> bool:
    if not config.ENABLE_PING:
        return False
    try:
        args = ["ping", "-n", "1", "-w", "1000", ip] if os.name == "nt" else ["ping", "-c", "1", "-W", "1", ip]
        p = await asyncio.to_thread(
            subprocess.run, args, capture_output=True, timeout=3
        )
        return p.returncode == 0
    except Exception:
        return False


async def probe_node(node: Node) -> dict:
    result = {
        "status": "未知",
        "ssh_open": False,
        "ping": False,
        "checked_at": time.strftime("%H:%M:%S"),
        "cached": False,
    }
    if not node.ip:
        result["status"] = "未绑定IP"
        return result

    ssh_open, ping_ok = await asyncio.gather(
        _tcp_open(node.ip, config.SSH_PORT, config.PROBE_TIMEOUT),
        _ping(node.ip),
    )
    result["ssh_open"] = ssh_open
    result["ping"] = ping_ok
    if ssh_open:
        result["status"] = "在线"
    elif ping_ok:
        result["status"] = "可达(SSH未开)"
    else:
        result["status"] = "离线"
    return result


# ---------------------------------------------------------------- 探测缓存
# 一次 ping + TCP 探测最坏要 1 秒多，节点一多、再叠上前端 15 秒自动刷新，
# 接口就会被拖到秒级。这里按 IP 缓存一小段时间，列表接口瞬间返回。

_PROBE_CACHE: dict[str, tuple[float, dict]] = {}
_PROBE_LOCK = threading.Lock()


def clear_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


async def probe_nodes(nodes: list[Node], force: bool = False) -> list[dict]:
    """带 TTL 缓存的批量探测；force=True 强制重新探测。"""
    ttl = max(0, config.PROBE_CACHE_TTL)
    if ttl == 0:
        force = True

    now = time.time()
    results: list[dict] = [{} for _ in nodes]
    todo: list[tuple[int, str, Node]] = []

    for idx, node in enumerate(nodes):
        key = node.ip or node.name
        hit = None
        if not force:
            with _PROBE_LOCK:
                cached = _PROBE_CACHE.get(key)
            if cached and now - cached[0] < ttl:
                hit = dict(cached[1])
                hit["cached"] = True
        if hit is not None:
            results[idx] = hit
        else:
            todo.append((idx, key, node))

    if todo:
        fresh = await asyncio.gather(*[probe_node(n) for _i, _k, n in todo])
        stamp = time.time()
        with _PROBE_LOCK:
            for (_idx, key, _node), res in zip(todo, fresh):
                _PROBE_CACHE[key] = (stamp, res)
        for (idx, _key, _node), res in zip(todo, fresh):
            results[idx] = res

    return results
