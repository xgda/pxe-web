"""系统服务状态监听（nginx / isc-dhcp-server / tftpd-hpa）——事件驱动。

只读，不做任何启停操作。

工作方式（双保险，任何一条路通了都能更新）：
1. 事件通道：常驻监督线程挂 `dbus-monitor`，订阅 systemd 的 PropertiesChanged
   信号（真·事件驱动，服务一变立刻知道）。掉了会自动重挂；
   没装 dbus-monitor 就完全不重试。
2. 巡检兜底：另有一条 `PXE_SERVICE_RECHECK` 秒的巡检（默认 15 秒）——
   **事件模式正常时它也照跑**，只是签名没变就不推，页面上完全无感。
   它的存在是为了自愈：dbus 没广播、广播漏了、容器里没 systemd、
   或者 dbus-monitor 静默假死，都不会让页面卡在旧状态。
3. 无论哪条路触发，只有当状态签名（ActiveState / SubState / installed / PID）
   相比上次发生了变化，才把结果经 WebSocket 推给页面。
4. WebSocket /ws/services：连接即先推一份当前快照，之后只在变化时推。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time

import config
from logger import log

# 默认盯这三个：HTTP 安装源、DHCP、TFTP，任一挂了装机链路就断
DEFAULT_SERVICES = ("nginx", "isc-dhcp-server", "tftpd-hpa")

# systemctl show 里要取的属性（--value 下按此顺序一行一个）
_PROPS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ActiveEnterTimestampMonotonic",
)

# 上面那些属性的 Key=value 形式（--value 解析失败时的备用问法）
_P_ARGS = ["--property=" + p for p in _PROPS]

# 退化轮询的间隔（秒）。事件模式正常时不使用它
WATCH_INTERVAL = max(1.0, float(os.getenv("PXE_SERVICE_WATCH_INTERVAL", "5")))

# 安全网巡检间隔（秒）。**事件模式正常时它也照跑**，只是签名没变就不推，
# 页面上完全无感；作用是 dbus 没广播 / 广播漏了 / 容器里没 systemd 时也能自愈
RECHECK_INTERVAL = max(3.0, float(os.getenv("PXE_SERVICE_RECHECK", "15")))

# dbus-monitor 掉了以后多久重挂一次（秒）
DBUS_RETRY_INTERVAL = max(10.0, float(os.getenv("PXE_SERVICE_DBUS_RETRY", "30")))

# 收到 dbus 事件后稍等一下再查 systemctl，避开状态切换的中间态
EVENT_SETTLE = 0.4

# HTTP 接口的短缓存：多个标签页同时开时避免重复敲 systemctl
HTTP_CACHE_TTL = int(os.getenv("PXE_SERVICE_CACHE_TTL", "5"))

# systemd 可能出现的 ActiveState。解出来的东西不在这个集合里，就说明解析错位了
_VALID_STATES = frozenset((
    "active", "inactive", "failed", "activating", "deactivating",
    "reloading", "maintenance",
))

# dbus-monitor 输出里的 signal 首行，形如：
# signal time=... sender=:1.0 -> destination=... path=/org/freedesktop/systemd1/unit/nginx_2eservice; interface=...; member=PropertiesChanged
_DBUS_SIGNAL_RE = re.compile(r"path=(/org/freedesktop/systemd1/unit/[^;\s]+)")

_state_lock = threading.RLock()
_clients: set = set()                 # 已连接的 WebSocket
_loop: "asyncio.AbstractEventLoop | None" = None
_thread: "threading.Thread | None" = None
_stop = threading.Event()
_snapshot: dict[str, dict] = {}       # name(规范名) -> 最近一次查询结果
_sig: dict[str, tuple] = {}           # name -> 状态签名
_http_cache: dict = {"at": 0.0, "payload": None}
_dbus_proc: "subprocess.Popen | None" = None
_dbus_mode = "poll"                  # event / poll：当前用的是哪种模式，给日志和接口看


def watched() -> list[str]:
    raw = os.getenv("PXE_WATCH_SERVICES", ",".join(DEFAULT_SERVICES))
    names = [s.strip() for s in raw.split(",") if s.strip()]
    return names or list(DEFAULT_SERVICES)


def _norm(name: str) -> str:
    """nginx.service / NGINX -> nginx；统一作快照键。"""
    n = (name or "").strip()
    low = n.lower()
    return low[: -len(".service")] if low.endswith(".service") else low


def _watched_set() -> set:
    return {_norm(n) for n in watched()}


def _signature(item: dict) -> tuple:
    """状态签名：只有这个变了才推送。PID 也在内——重启了也该让页面知道。"""
    return (item["state"], item["sub"], item["installed"], item["pid"])


# ---------------------------------------------------------------- systemctl

def _systemctl(args: list[str], timeout: int = 4) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["systemctl"] + args, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "未找到 systemctl（非 systemd 系统？）"
    except subprocess.TimeoutExpired:
        return 124, "", "systemctl 查询超时"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _parse_show(out: str, keys: tuple[str, ...]) -> dict:
    """兼容两种输出：`--value` 的一行一值，和老式 `Key=value`。"""
    vals: dict[str, str] = {}
    lines = [l for l in out.splitlines() if l.strip()]
    if lines and len(lines) == len(keys) and not any("=" in l for l in lines):
        for k, v in zip(keys, lines):
            vals[k] = v.strip()
        return vals
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except Exception:  # noqa: BLE001
        return None


def query_one(name: str) -> dict:
    """查询单个服务状态（阻塞 ~几十 ms）。"""
    item = {
        "name": _norm(name),
        "ok": False,          # 只有 active 才算 OK
        "active": False,
        "state": "unknown",   # active / inactive / failed / activating / not-found ...
        "sub": "",            # running / dead / exited / failed ...
        "pid": 0,
        "since": "",          # 已运行多久（人类可读，备用）
        "installed": True,
        "detail": "",         # 出错时的补充信息
        "raw": "",            # systemctl 原始输出（截断），排查用，页面悬停可见
        "rc": 0,
    }

    if config.DRY_RUN:
        item["state"] = "dry-run"
        item["sub"] = "DRY_RUN"
        item["installed"] = False
        item["detail"] = "DRY_RUN 模式，未实际查询 systemctl"
        return item

    rc, out, err = _systemctl(
        ["show", name, "--property=" + ",".join(_PROPS), "--value"]
    )
    v = _parse_show(out, _PROPS)

    # --value 是按行给值，两种情况下会解析错，症状都是"服务明明 active，页面却 dead"：
    #   ① 某个属性返回空行，被过滤掉后行数对不上，整段解析作废
    #   ② 输出顺序跟 _PROPS 不一致，值被错位塞给别的字段
    # 只要解出来的 ActiveState 不像个正常状态，就换成 Key=value 再问一次
    # （Key=value 按名字取值，不依赖行数和顺序）。
    if rc == 0 and (v.get("ActiveState") or "").strip() not in _VALID_STATES:
        rc2, out2, err2 = _systemctl(["show", name] + _P_ARGS)
        if rc2 == 0 and out2.strip():
            v2 = _parse_show(out2, _PROPS)
            if (v2.get("ActiveState") or "").strip() in _VALID_STATES:
                rc, out, err, v = rc2, out2, err2, v2

    item["rc"] = rc
    item["raw"] = ((out or "") + (" |STDERR: " + err if err else "")).strip()[:300]

    if rc != 0 or (v.get("ActiveState") or "").strip() not in _VALID_STATES:
        # show 失败 / 解析不出来（unit 不存在、权限不足、没有 systemd）→ 退到 is-active
        rc2, out2, err2 = _systemctl(["is-active", name])
        st = (out2 or "").strip().splitlines()[-1] if out2.strip() else "unknown"
        item["state"] = st
        item["sub"] = st
        item["active"] = st == "active"
        item["ok"] = item["active"]
        item["installed"] = st != "unknown"
        item["detail"] = (err or err2 or out2 or "查询失败").strip()[:300]
        item["rc"] = rc2
        item["raw"] = (item["raw"] + " || is-active: " + (out2 or err2 or "").strip())[:300]
        if st == "unknown":
            log.warning("服务状态查不到：%s（rc=%s）raw=%r", name, rc, item["raw"][:200])
        return item

    load = (v.get("LoadState") or "").strip()
    state = (v.get("ActiveState") or "unknown").strip() or "unknown"
    sub = (v.get("SubState") or "").strip()
    if load == "not-found":
        state = "not-found"
        sub = sub or "not-found"

    item["state"] = state
    item["sub"] = sub
    item["active"] = state == "active"
    item["ok"] = item["active"]
    item["installed"] = load != "not-found"
    try:
        item["pid"] = int(v.get("MainPID") or 0)
    except ValueError:
        item["pid"] = 0

    # 运行时长：ActiveEnterTimestampMonotonic(微秒) 对比开机秒数
    up = _uptime_seconds()
    try:
        mono_us = int(v.get("ActiveEnterTimestampMonotonic") or 0)
    except ValueError:
        mono_us = 0
    if item["active"] and mono_us > 0 and up is not None:
        ago = up - mono_us / 1_000_000.0
        if 0 < ago < up + 1:
            d, rem = divmod(int(ago), 86400)
            h, rem = divmod(rem, 3600)
            m, _ = divmod(rem, 60)
            item["since"] = (f"{d} 天 {h} 小时" if d
                             else f"{h} 小时 {m} 分" if h else f"{m} 分钟")
    return item


# ---------------------------------------------------------------- 快照与推送

def _payload(items: list[dict], source: str, changed: list[str] | None = None) -> dict:
    bad = [i["name"] for i in items if not i["ok"]]
    return {
        "items": items,
        "ok": not bad,
        "bad": bad,
        "checked_at": time.strftime("%H:%M:%S"),
        "source": source,
        "mode": _dbus_mode,          # event = dbus 事件驱动；poll = 巡检兜底
        "changed": changed or [],
        "dry_run": bool(config.DRY_RUN),
    }


def _refresh_one(name: str) -> bool:
    """刷新一个服务进快照，返回签名是否有变化。"""
    item = query_one(name)
    key = _norm(name)
    s = _signature(item)
    old = _sig.get(key)
    _snapshot[key] = item
    _sig[key] = s
    return old is not None and old != s


def _refresh_all(source: str) -> None:
    """全部刷一遍，有变化就广播。"""
    with _state_lock:
        changed = [n for n in watched() if _refresh_one(n)]
        payload = _payload(list(_snapshot.values()), source, changed) if changed else None
    if payload:
        log.info("服务状态变化（%s / %s）：%s", source, _dbus_mode,
                 "、".join("%s→%s" % (n, _snapshot[n]["state"]) for n in changed))
        for n in changed:
            i = _snapshot[n]
            if not i["ok"]:
                log.warning("  %s 非 active：state=%s sub=%s rc=%s raw=%r",
                            n, i["state"], i["sub"], i["rc"], i["raw"][:200])
        _broadcast(payload)


def status(force: bool = False) -> dict:
    """给 HTTP 接口用：有快照给快照，没有就现查。"""
    if config.DRY_RUN:
        return _payload([query_one(n) for n in watched()], "dry-run")

    now = time.time()
    with _state_lock:
        cached = _http_cache["payload"]
        if not force and cached and now - _http_cache["at"] < HTTP_CACHE_TTL:
            p = dict(cached)
            p["cached"] = True
            return p
        items = list(_snapshot.values())
        if not items or force:
            for n in watched():
                _refresh_one(n)
            items = list(_snapshot.values())
        payload = _payload(items, "http")
        _http_cache["at"] = now
        _http_cache["payload"] = payload
        return dict(payload)


# ---------------------------------------------------------------- WebSocket

def _register(ws) -> None:
    with _state_lock:
        _clients.add(ws)


def _unregister(ws) -> None:
    with _state_lock:
        _clients.discard(ws)


async def _broadcast_async(text: str) -> None:
    with _state_lock:
        clients = list(_clients)
    for ws in clients:
        try:
            await ws.send_text(text)
        except Exception:  # noqa: BLE001
            _unregister(ws)


def _broadcast(payload: dict) -> None:
    """监听线程里调用：把变化推给所有页面。"""
    if _loop is None:
        return
    text = json.dumps(payload, ensure_ascii=False)
    try:
        asyncio.run_coroutine_threadsafe(_broadcast_async(text), _loop)
    except RuntimeError:
        pass


async def ws_attach(ws) -> None:
    """WS 连接：先发一份当前快照，然后挂着收变化，直到客户端断开。"""
    _register(ws)
    try:
        with _state_lock:
            items = list(_snapshot.values())
        if not items:
            items = [query_one(n) for n in watched()]
        await ws.send_text(json.dumps(_payload(items, "snapshot"), ensure_ascii=False))
        while True:
            # 浏览器不会主动发消息；收包只为感知断开
            await ws.receive_text()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _unregister(ws)


# ---------------------------------------------------------------- 监听线程

def _unit_object_path(name: str) -> str:
    """nginx -> /org/freedesktop/systemd1/unit/nginx_2eservice（systemd 总线路径转义）。"""
    unit = name if "." in name else name + ".service"
    esc = "".join(ch if ch.isalnum() else "_%02x" % ord(ch) for ch in unit)
    return "/org/freedesktop/systemd1/unit/" + esc


def _unit_from_object_path(path: str) -> str:
    esc = path.rstrip("/").rsplit("/", 1)[-1]
    dec = re.sub(r"_([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), esc)
    return _norm(dec)


def _dbus_session() -> str:
    """挂一次 dbus-monitor，阻塞到它退出。返回 event / missing / error。"""
    global _dbus_proc, _dbus_mode
    cmd = [
        "dbus-monitor", "--system",
        "type='signal',sender='org.freedesktop.systemd1',"
        "interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="ignore", bufsize=1,
        )
    except (FileNotFoundError, OSError):
        return "missing"

    time.sleep(1.5)          # 起不来（没有 dbus-monitor / 权限）的话早就退了
    if proc.poll() is not None:
        return "error"
    _dbus_proc = proc
    _dbus_mode = "event"
    log.info("服务状态：已挂上 dbus-monitor（systemd 事件驱动）")

    watched_set = _watched_set()
    try:
        for line in proc.stdout:
            if _stop.is_set():
                break
            m = _DBUS_SIGNAL_RE.search(line or "")
            if not m:
                continue
            unit = _unit_from_object_path(m.group(1))
            if unit not in watched_set:
                continue
            # 状态切换有中间态（deactivating/activating），稍等一下再查，读到的是落定值
            time.sleep(EVENT_SETTLE)
            with _state_lock:
                payload = None
                if _refresh_one(unit):
                    payload = _payload(list(_snapshot.values()), "event", [unit])
            if payload:
                log.info("服务状态变化（dbus 事件）：%s→%s",
                         unit, _snapshot[unit]["state"])
                _broadcast(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("dbus-monitor 读取异常：%s", exc)
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        _dbus_proc = None
        _dbus_mode = "poll"
    return "event"


def _dbus_supervisor() -> None:
    """dbus-monitor 的监督线程：掉了就重挂，没装就干脆不重试（交给巡检）。"""
    while not _stop.is_set():
        rc = _dbus_session()
        if rc == "missing":
            log.warning("没找到 dbus-monitor（apt install -y dbus-x），"
                        "服务状态走 %.0f 秒巡检兜底", RECHECK_INTERVAL)
            return
        if _stop.wait(DBUS_RETRY_INTERVAL):
            break
        log.warning("dbus-monitor 会话结束（%s），%.0f 秒后重挂，期间走巡检兜底",
                    rc, DBUS_RETRY_INTERVAL)
    log.info("dbus 监督线程已退出")


def _watch_loop() -> None:
    """主监听线程：dbus 事件 + 定时巡检双保险，只有状态变了才推。"""
    log.info("服务状态监听启动：%s（dbus 事件优先 + %.0f 秒巡检兜底）",
             "、".join(watched()), RECHECK_INTERVAL)
    _refresh_all("startup")
    with _state_lock:
        shot = "，".join("%s=%s/%s(rc=%s)" % (k, i["state"], i["sub"] or "-", i["rc"])
                         for k, i in _snapshot.items())
    log.info("服务状态启动快照：%s", shot)
    with _state_lock:
        for k, i in list(_snapshot.items()):
            if not i["ok"]:
                log.warning("服务状态非 active：%s state=%s sub=%s rc=%s raw=%r",
                            k, i["state"], i["sub"], i["rc"], i["raw"][:200])
    threading.Thread(target=_dbus_supervisor, name="pxe-svc-dbus", daemon=True).start()
    while not _stop.wait(RECHECK_INTERVAL):
        _refresh_all("recheck")
    log.info("服务状态监听已停止")


def start_watch(loop: "asyncio.AbstractEventLoop | None") -> None:
    """app startup 时调用；DRY_RUN 下不启动。"""
    global _loop, _thread
    if config.DRY_RUN:
        log.info("DRY_RUN 模式：不启动服务状态监听线程")
        return
    _loop = loop
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_watch_loop, name="pxe-svc-watch", daemon=True)
    _thread.start()


def stop_watch() -> None:
    """app shutdown 时调用。"""
    _stop.set()
    if _dbus_proc:
        try:
            _dbus_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
