"""部署任务：管理 /opt/pxe-web 下的 .sh / .py 脚本，并到目标节点上执行。

执行方式：通过 SSH 把脚本内容喂给 `bash -s` 或 `python3 -` 的标准输入，
无需先传文件，执行完回收 stdout / stderr / 退出码。

执行结果除了回给 Web 界面，还会**落盘**到

    <SCRIPTS_DIR>/log/<节点 SN>/<脚本名>_<YYYYmmdd-HHMMSS>.log

SN 通过 SSH 现场读取（DMI / device-tree / dmidecode / product_uuid 依次降级）。
SN 相同但明显不是同一台机器时（用 MAC/IP 判定），目录名自动追加 -2 / -3 后缀。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Optional

import paramiko

import config
from logger import log

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(sh|py)$", re.I)
EXTS = (".sh", ".py")

LOG_INDEX = "_index.json"


def _dir() -> str:
    return os.path.abspath(config.SCRIPTS_DIR)


def _path(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("脚本名不合法：仅支持字母数字组成的 .sh / .py 文件名")
    base = _dir()
    path = os.path.abspath(os.path.join(base, name))
    if os.path.dirname(path) != base:
        raise ValueError("非法脚本名")
    return path


def _lang(name: str) -> str:
    return "bash" if name.lower().endswith(".sh") else "python"


# ------------------------------------------------------------------ 列表

def list_scripts() -> list[dict]:
    base = _dir()
    if not os.path.isdir(base):
        return []
    items = []
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if not os.path.isfile(path) or not name.lower().endswith(EXTS):
            continue
        items.append(
            {
                "name": name,
                "lang": _lang(name),
                "size": os.path.getsize(path),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))),
            }
        )
    return items


def save_script(upload) -> dict:
    name = os.path.basename(getattr(upload, "filename", "") or "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    path = _path(name)
    if os.path.exists(path):
        raise ValueError(f"脚本 {name} 已存在")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    log.info("新增脚本 %s", path)
    return {"ok": True, "message": f"脚本 {name} 已上传", "path": path}


def delete_script(name: str) -> dict:
    path = _path(name)
    if not os.path.isfile(path):
        raise ValueError(f"脚本 {name} 不存在")
    os.remove(path)
    log.info("删除脚本 %s", path)
    return {"ok": True, "message": f"脚本 {name} 已删除"}


# ------------------------------------------------------------------ 节点 SN

# 依次尝试：DMI（x86 服务器）→ device-tree（ARM）→ dmidecode → product_uuid
SN_SCRIPT = r"""
sn=""
for f in /sys/class/dmi/id/product_serial /sys/class/dmi/id/chassis_serial \
         /sys/class/dmi/id/board_serial /sys/firmware/devicetree/base/serial-number; do
    if [ -r "$f" ]; then
        v=$(tr -d '\0' < "$f" 2>/dev/null | tr -d ' \t\r\n')
        if [ -n "$v" ]; then sn="$v"; break; fi
    fi
done
if [ -z "$sn" ] && command -v dmidecode >/dev/null 2>&1; then
    v=$(dmidecode -s system-serial-number 2>/dev/null | head -n1 | tr -d ' \t\r\n')
    [ -n "$v" ] && sn="$v"
fi
if [ -z "$sn" ] && [ -r /sys/class/dmi/id/product_uuid ]; then
    sn=$(tr -d '\0' < /sys/class/dmi/id/product_uuid 2>/dev/null | tr -d ' \t\r\n')
fi
printf '%s\n' "$sn"
""".strip()

# 厂商没写 SN 时常见占位值，读了等于没读
INVALID_SN = {
    "", "none", "null", "nil", "unknown", "n/a", "na", "0",
    "0000000000", "000000000000", "0123456789", "123456789",
    "default string", "defaultstring", "not specified", "notspecified",
    "to be filled by o.e.m.", "tobefilledbyo.e.m.", "to be filled by o.e.m",
    "system serial number", "chassis serial number", "invalid",
}


def _sanitize_sn(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]", "_", (raw or "").strip()).strip("._-")
    return value[:64]


def _valid_sn(raw: str) -> str:
    value = (raw or "").strip()
    return "" if value.lower() in INVALID_SN else _sanitize_sn(value)


def _fetch_sn(client) -> str:
    try:
        _in, out, _err = client.exec_command(SN_SCRIPT, timeout=config.SSH_TIMEOUT)
        raw = out.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("读取节点 SN 失败：%s", exc)
        return ""
    return _valid_sn(raw)


# ------------------------------------------------------------------ 日志落盘

def log_root() -> str:
    return os.path.abspath(config.TASK_LOG_DIR)


def _load_index(root: str) -> dict:
    path = os.path.join(root, LOG_INDEX)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_index(root: str, index: dict) -> None:
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, LOG_INDEX), "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as exc:
        log.warning("写入日志索引失败：%s", exc)


_INDEX_LOCK = threading.Lock()


def resolve_sn_dir(sn: str, identity: str, node: str = "", ip: str = "") -> str:
    """返回该 SN 对应的日志目录；SN 与既有条目冲突时自动加 -2 / -3 后缀。"""
    root = log_root()
    os.makedirs(root, exist_ok=True)
    with _INDEX_LOCK:  # 并发执行时索引文件只能串行改
        index = _load_index(root)

        candidate, n = sn, 1
        while True:
            owner = index.get(candidate)
            if owner is None or owner.get("id") == identity:
                break
            n += 1
            if n > 200:  # 兜底，别真死循环
                candidate = f"{sn}-{n}"
                break
            candidate = f"{sn}-{n}"

        index[candidate] = {
            "id": identity,
            "node": node,
            "ip": ip,
            "sn_raw": sn,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_index(root, index)
    return os.path.join(root, candidate)


def write_task_log(result: dict, node: str, ip: str, sn: str, script: str) -> str:
    """把一次执行的完整输出写成日志文件，返回绝对路径；失败返回空串。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = os.path.splitext(os.path.basename(script or "task"))[0]
    identity = (result.get("mac") or ip or node or "unknown").lower()

    raw_sn = sn or ""
    folder_name = raw_sn or _sanitize_sn(node) or _sanitize_sn(ip.replace(".", "-")) or "unknown"
    directory = resolve_sn_dir(folder_name, identity, node=node, ip=ip)

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "task"
    filename = f"{safe}_{stamp}.log"
    path = os.path.join(directory, filename)
    n = 1
    while os.path.exists(path):  # 同一秒内的重复执行
        n += 1
        path = os.path.join(directory, f"{safe}_{stamp}-{n}.log")

    status = "成功" if result.get("ok") else "失败"
    lines = [
        "# PXE 部署任务执行日志",
        f"# 节点名称：{node or '-'}",
        f"# 节点 IP  ：{ip or '-'}",
        f"# 节点 MAC ：{result.get('mac') or '-'}",
        f"# 节点 SN  ：{raw_sn or '（未取到，用节点名/IP 兜底）'}",
        f"# 脚本    ：{script}",
        f"# 登录用户：{result.get('user') or '-'}（端口 {result.get('port') or 22}）",
        f"# 开始时间：{result.get('started_at') or '-'}",
        f"# 耗时    ：{result.get('duration', 0)}s",
        f"# 退出码  ：{result.get('exit_code')}（{status}）",
        "# " + "-" * 68,
        "",
        "===== STDOUT =====",
        result.get("stdout_full") or result.get("stdout") or "",
    ]
    if result.get("stderr_full") or result.get("stderr"):
        lines += ["", "===== STDERR =====", result.get("stderr_full") or result.get("stderr") or ""]
    if result.get("error"):
        lines += ["", "===== ERROR =====", result["error"]]
    lines.append("")

    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError as exc:
        log.warning("写执行日志失败 %s：%s", path, exc)
        return ""
    return path


def list_task_logs() -> list[dict]:
    """列出 scripts/log 下按 SN 归档的执行日志。"""
    root = log_root()
    if not os.path.isdir(root):
        return []
    index = _load_index(root)
    out = []
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        files = []
        for f in sorted(os.listdir(directory), reverse=True):
            fp = os.path.join(directory, f)
            if not os.path.isfile(fp) or not f.endswith(".log"):
                continue
            files.append(
                {
                    "name": f,
                    "size": os.path.getsize(fp),
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fp))),
                }
            )
        meta = index.get(name, {})
        out.append(
            {
                "sn": name,
                "node": meta.get("node", ""),
                "ip": meta.get("ip", ""),
                "updated": meta.get("updated", ""),
                "files": files,
            }
        )
    out.sort(key=lambda r: (r["node"] or r["sn"]).lower())
    return out


def read_task_log(sn: str, filename: str) -> str:
    root = log_root()
    directory = os.path.abspath(os.path.join(root, sn or ""))
    if os.path.dirname(directory) != root or not os.path.isdir(directory):
        raise ValueError("非法 SN")
    path = os.path.abspath(os.path.join(directory, filename or ""))
    if os.path.dirname(path) != directory or not path.endswith(".log") or not os.path.isfile(path):
        raise ValueError("日志文件不存在")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ------------------------------------------------------------------ 执行

def run_on_node(
    host: str,
    name: str,
    user: str,
    password: str,
    port: int = 22,
    timeout: int = 0,
    node: str = "",
    mac: str = "",
) -> dict:
    """在单台节点上执行脚本，返回标准输出 / 错误 / 退出码，并把完整输出落盘。"""
    result = {
        "host": host,
        "script": name,
        "node": node,
        "mac": mac,
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": "",
        "duration": 0.0,
        "sn": "",
        "log_file": "",
    }
    try:
        path = _path(name)
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError as exc:
        result["error"] = f"读取脚本失败：{exc}"
        return result

    command = "bash -s" if _lang(name) == "bash" else "python3 -"
    timeout = timeout or config.TASK_TIMEOUT
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    started = time.time()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    out = err = ""
    try:
        kwargs = {
            "port": port,
            "username": user,
            "timeout": config.SSH_TIMEOUT,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if password:
            kwargs["password"] = password
        elif os.path.exists(config.SSH_KEY_FILE or ""):
            kwargs["key_filename"] = config.SSH_KEY_FILE
        client.connect(host, **kwargs)
        result["sn"] = _fetch_sn(client)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.write(content)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        result.update(
            {
                "ok": code == 0,
                "exit_code": code,
                "stdout": out[-8000:],
                "stderr": err[-8000:],
                "duration": round(time.time() - started, 1),
            }
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration"] = round(time.time() - started, 1)
    finally:
        try:
            client.close()
        except Exception:
            pass

    result["stdout_full"] = out
    result["stderr_full"] = err
    result["user"] = user
    result["port"] = port
    result["started_at"] = started_at
    result["log_file"] = write_task_log(result, node or host, host, result.get("sn", ""), name)
    return result
