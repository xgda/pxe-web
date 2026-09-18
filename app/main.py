"""PXE 装机管理平台 · 后端入口

启动：  uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import os
import sys

# 允许以 `uvicorn app.main:app` 或 `python app/main.py` 两种方式启动
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from typing import Optional  # noqa: E402

import paramiko  # noqa: E402
from fastapi import (  # noqa: E402
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import config  # noqa: E402
import dhcp_conf as dhcp_conf_mod  # noqa: E402
import discovery as discovery_mod  # noqa: E402
import hosts_store as store  # noqa: E402
import images as images_mod  # noqa: E402
import services as services_mod  # noqa: E402
import tasks as tasks_mod  # noqa: E402
from auth import create_token, require_auth, verify_token  # noqa: E402
from logger import log  # noqa: E402

STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="PXE Web Console", version="0.1.0")


@app.on_event("startup")
async def on_startup() -> None:
    """启动时幂等补齐两边的兜底菜单配置（脚本随包提供，不再自动生成）。"""
    fallback = images_mod.ensure_fallback_menus()
    services_mod.start_watch(asyncio.get_running_loop())
    log.info("PXE Web Console %s 启动完成（兜底菜单 grub=%s / ipxe=%s）",
             config.APP_VERSION, fallback["grub"], fallback["ipxe"])


@app.on_event("shutdown")
def on_shutdown() -> None:
    services_mod.stop_watch()


@app.middleware("http")
async def access_log(request, call_next):
    """记录每个请求的方法、路径、状态码和耗时。"""
    start = time.time()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:  # noqa: BLE001
        status = 500
        log.exception("请求异常 %s %s：%s", request.method, request.url.path, exc)
        raise
    finally:
        cost = (time.time() - start) * 1000
        client = request.client.host if request.client else "-"
        log.info("%s %s -> %s %.0fms [%s]", request.method, request.url.path, status, cost, client)
    return response


# ------------------------------------------------------------------ 登录

class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(data: LoginIn):
    if data.username == config.ADMIN_USER and data.password == config.ADMIN_PASS:
        log.info("用户登录成功：%s", data.username)
        return {"token": create_token(data.username), "user": data.username}
    log.warning("用户登录失败：%s", data.username)
    raise HTTPException(status_code=401, detail="用户名或密码错误")


@app.get("/api/me")
def api_me(user: str = Depends(require_auth)):
    return {"user": user}


# ------------------------------------------------------------------ 节点

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@app.get("/api/nodes")
async def api_nodes(probe: str = "", _: str = Depends(require_auth)):
    # probe=force 强制重新探测；默认走 PROBE_CACHE_TTL 秒缓存，列表秒开
    nodes = store.load_nodes()
    probes = await store.probe_nodes(nodes, force=(probe == "force"))
    items = []
    for node, p in zip(nodes, probes):
        merged = node.to_dict()
        merged.update(p)
        items.append(merged)
    return {"items": items, "total": len(items), "probe_ttl": config.PROBE_CACHE_TTL}


class NodeIn(BaseModel):
    name: str
    mac: Optional[str] = ""
    ip: Optional[str] = ""
    system: Optional[str] = ""
    arch: Optional[str] = "x86"


@app.post("/api/nodes")
def api_save_node(data: NodeIn, _: str = Depends(require_auth)):
    if not NAME_RE.match(data.name or ""):
        raise HTTPException(status_code=400, detail="节点名只能包含字母、数字、点、下划线和中划线")
    if data.ip and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", data.ip):
        raise HTTPException(status_code=400, detail="IP 格式不正确")
    if data.mac and not re.match(r"^[0-9A-Fa-f:]{11,17}$", data.mac):
        raise HTTPException(status_code=400, detail="MAC 格式不正确")

    arch = (data.arch or "x86").lower()
    if arch not in store.ARCHES:
        raise HTTPException(status_code=400, detail="架构必须是 x86 或 arm")

    res = store.save_node(data.name, data.mac or "", data.ip or "", data.system or "", arch)
    if not res.get("ok"):
        log.error("保存节点 %s 失败：%s", data.name, res.get("message"))
        raise HTTPException(status_code=400, detail={"message": res.get("message"), "detail": res.get("detail")})
    log.info("保存节点 %s 成功（ip=%s arch=%s system=%s）", data.name, data.ip, arch, data.system)
    return res


@app.delete("/api/nodes/{name}")
def api_delete_node(name: str, _: str = Depends(require_auth)):
    res = store.delete_node(name)
    if not res.get("ok"):
        log.error("删除节点 %s 失败：%s", name, res.get("message"))
        raise HTTPException(status_code=400, detail=res.get("message"))
    log.info("删除节点 %s 成功", name)
    return res


# ------------------------------------------------------------------ 镜像

@app.get("/api/images")
def api_images(_: str = Depends(require_auth)):
    items = images_mod.list_images()
    return {"items": items, "total": len(items), "dir": config.IMAGES_DIR}


@app.get("/api/uki-types")
def api_uki_types(arch: str = "x86", _: str = Depends(require_auth)):
    """UKI 目录里已内置的系统类型（供"添加镜像"下拉使用）。"""
    # arch 会被拼进路径，必须限定取值，否则 ../.. 能跳出 www 根目录去列别的目录
    arch = (arch or "x86").lower()
    if arch not in store.ARCHES:
        raise HTTPException(status_code=400, detail="架构必须是 x86 或 arm")
    return {
        "items": images_mod.list_uki_types(arch),
        "arch": arch,
        "dir": images_mod.uki_root(arch),
        "new_type_label": config.NEW_TYPE_LABEL,
    }


@app.post("/api/images")
async def api_add_image(
    name: str = Form(...),
    iso: UploadFile = File(...),
    arch: str = Form("x86"),
    # 选了 UKI 里已有的系统类型时，下面这些都不需要（界面会隐藏对应上传框）
    initrd: Optional[UploadFile] = File(None),
    vmlinuz: Optional[UploadFile] = File(None),
    uki_type: str = Form(""),
    meta_data: Optional[UploadFile] = File(None),
    user_data: Optional[UploadFile] = File(None),
    _: str = Depends(require_auth),
):
    log.info("开始上传镜像 name=%s arch=%s uki=%s iso=%s initrd=%s vmlinuz=%s meta=%s user=%s",
             name, arch, uki_type or "-", getattr(iso, "filename", "-"),
             getattr(initrd, "filename", "-") if initrd else "无",
             getattr(vmlinuz, "filename", "-") if vmlinuz else "无",
             getattr(meta_data, "filename", "-") if meta_data else "无",
             getattr(user_data, "filename", "-") if user_data else "无")
    try:
        result = images_mod.add_image(
            name, iso, initrd, vmlinuz, meta_data, user_data, arch, uki_type
        )
    except ValueError as exc:
        log.error("创建镜像 %s 失败：%s", name, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("创建镜像 %s 异常", name)
        raise HTTPException(status_code=500, detail=f"创建镜像失败：{exc}")
    log.info("镜像 %s 上传完成：%s", name, result.get("path"))
    return result


class MenuIn(BaseModel):
    content: str


@app.get("/api/images/{name}/menu")
def api_get_image_menu(name: str, allow_create: int = 0, _: str = Depends(require_auth)):
    try:
        return images_mod.get_image_menu(name, allow_create=bool(allow_create))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("读取镜像启动配置失败")
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}")


@app.put("/api/images/{name}/menu")
def api_update_image_menu(name: str, data: MenuIn, _: str = Depends(require_auth)):
    try:
        return images_mod.update_image_menu(name, data.content or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("更新镜像启动配置失败")
        raise HTTPException(status_code=500, detail=f"更新失败：{exc}")


@app.post("/api/images/{name}/prewarm")
def api_prewarm_image(name: str, deep: int = 0, _: str = Depends(require_auth)):
    """把镜像的 ISO / vmlinuz / initrd 预热进内核页缓存，批量装机前跑一次能削峰。"""
    try:
        return images_mod.prewarm_image(name, deep=bool(deep))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("预热镜像 %s 失败", name)
        raise HTTPException(status_code=500, detail=f"预热失败：{exc}")


@app.delete("/api/images/{name}")
def api_delete_image(name: str, _: str = Depends(require_auth)):
    try:
        result = images_mod.delete_image(name)
    except ValueError as exc:
        log.error("删除镜像 %s 失败：%s", name, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("删除镜像 %s 异常", name)
        raise HTTPException(status_code=500, detail=f"删除镜像失败：{exc}")
    log.info("镜像 %s 已删除", name)
    return result


# ------------------------------------------------------------------ 部署任务

@app.get("/api/scripts")
def api_scripts(_: str = Depends(require_auth)):
    return {"items": tasks_mod.list_scripts(), "dir": config.SCRIPTS_DIR}


@app.post("/api/scripts")
def api_add_script(file: UploadFile = File(...), _: str = Depends(require_auth)):
    try:
        return tasks_mod.save_script(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("上传脚本失败")
        raise HTTPException(status_code=500, detail=f"上传脚本失败：{exc}")


@app.delete("/api/scripts/{name}")
def api_delete_script(name: str, _: str = Depends(require_auth)):
    try:
        return tasks_mod.delete_script(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除脚本失败：{exc}")


class TaskIn(BaseModel):
    script: str
    nodes: list[str]
    user: Optional[str] = ""
    password: str = ""
    port: Optional[int] = 22
    timeout: Optional[int] = 0
    # 并发数 / 每批间隔：几十台一起上时用来错峰，别把 HTTP 装机源打满
    concurrency: Optional[int] = 0
    batch_delay: Optional[int] = 0


def _chunk(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


@app.post("/api/tasks/run")
async def api_run_task(data: TaskIn, _: str = Depends(require_auth)):
    if not data.script:
        raise HTTPException(status_code=400, detail="请选择要执行的脚本")
    if not data.nodes:
        raise HTTPException(status_code=400, detail="请至少选择一台节点")

    known = {n.name: n for n in store.load_nodes()}
    missing = [n for n in data.nodes if n not in known]
    if missing:
        raise HTTPException(status_code=400, detail=f"节点不存在：{', '.join(missing)}")
    no_ip = [n for n in data.nodes if not known[n].ip]
    if no_ip:
        raise HTTPException(status_code=400, detail=f"节点未绑定 IP：{', '.join(no_ip)}")

    user = data.user or config.SSH_USER
    port = int(data.port or config.SSH_PORT)
    concurrency = max(1, int(data.concurrency or config.TASK_CONCURRENCY))
    delay = max(0, int(data.batch_delay or config.TASK_BATCH_DELAY))
    log.info(
        "执行任务：脚本=%s，节点=%s，用户=%s，并发=%s，批间隔=%ss",
        data.script, ",".join(data.nodes), user, concurrency, delay,
    )

    async def one(node_name: str):
        node = known[node_name]
        return await asyncio.to_thread(
            tasks_mod.run_on_node,
            node.ip,
            data.script,
            user,
            data.password,
            port,
            int(data.timeout or 0),
            node_name,
            node.mac or "",
        )

    # 分批 + 信号量双重限流：先按 concurrency 切批，批与批之间等 delay 秒
    results: list = []
    for idx, batch in enumerate(_chunk(data.nodes, concurrency)):
        if idx and delay:
            await asyncio.sleep(delay)
        results.extend(await asyncio.gather(*[one(n) for n in batch]))

    items = []
    for name, res in zip(data.nodes, results):
        res = dict(res)
        res["node"] = name
        items.append(res)
        log.info("节点 %s(%s) 执行 %s：%s（退出码 %s，%.1fs）日志 %s",
                 name, res.get("host"), data.script,
                 "成功" if res.get("ok") else "失败", res.get("exit_code"),
                 res.get("duration", 0), res.get("log_file") or "-")

    return {
        "items": items,
        "script": data.script,
        "concurrency": concurrency,
        "batch_delay": delay,
    }


@app.get("/api/task-logs")
def api_task_logs(_: str = Depends(require_auth)):
    return {"items": tasks_mod.list_task_logs(), "dir": tasks_mod.log_root()}


@app.get("/api/task-logs/content")
def api_task_log_content(sn: str = Query(...), file: str = Query(...), _: str = Depends(require_auth)):
    try:
        return {"content": tasks_mod.read_task_log(sn, file)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/task-logs/download")
def api_task_log_download(sn: str = Query(...), file: str = Query(...), _: str = Depends(require_auth)):
    try:
        content = tasks_mod.read_task_log(sn, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PlainTextResponse(content, headers={"Content-Disposition": f'attachment; filename="{file}"'})


# ------------------------------------------------------------------ 节点发现

@app.get("/api/discovery")
def api_discovery(
    pxe_only: int = 1,
    pending_only: int = 0,
    fresh: int = 0,
    _: str = Depends(require_auth),
):
    """pxe_only=1（默认）只看拿到过 filename / 走了 PXE 启动的租约；
    pending_only=1 只看待处理；fresh=1 跳过 syslog 解析缓存重新扫。"""
    return discovery_mod.discover(
        pxe_only=bool(pxe_only), pending_only=bool(pending_only), fresh=bool(fresh)
    )


@app.delete("/api/discovery/lease")
def api_release_lease(
    ip: str = Query(...), mac: str = Query(default=""), _: str = Depends(require_auth)
):
    ok, out = discovery_mod.release_lease(ip, mac)
    if not ok:
        raise HTTPException(status_code=400, detail=out)
    return {"ok": True, "message": f"已释放 {ip} 的租约", "detail": out}


@app.post("/api/menus/ensure-fallback")
def api_ensure_fallback(_: str = Depends(require_auth)):
    """手动补齐兜底菜单（正常在启动时自动做）。"""
    return images_mod.ensure_fallback_menus()


# ------------------------------------------------------------------ DHCP 子网

class SubnetIn(BaseModel):
    subnet: str
    netmask: str
    range_start: Optional[str] = ""
    range_end: Optional[str] = ""
    routers: Optional[str] = ""
    dns: Optional[str] = ""
    next_server: Optional[str] = ""


@app.get("/api/dhcp/subnet")
def api_get_subnet(_: str = Depends(require_auth)):
    return dhcp_conf_mod.parse_subnets()


@app.post("/api/dhcp/subnet")
def api_update_subnet(data: SubnetIn, original: str = Query(default=""), _: str = Depends(require_auth)):
    for field in ("subnet", "netmask"):
        value = getattr(data, field)
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value or ""):
            raise HTTPException(status_code=400, detail=f"{field} 不是合法 IP：{value}")
    res = dhcp_conf_mod.update_subnet(original or data.subnet, data.model_dump())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail={"message": res.get("message"), "detail": res.get("detail")})
    return res


# ------------------------------------------------------------------ 运行日志

@app.get("/api/logs")
def api_logs(lines: int = 300, _: str = Depends(require_auth)):
    path = config.LOG_FILE
    if not os.path.exists(path):
        return {"items": [f"（日志文件尚未生成：{path}）"], "file": path}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            data = fh.read().splitlines()
    except Exception as exc:  # noqa: BLE001
        return {"items": [f"读取日志失败：{exc}"], "file": path}
    return {"items": data[-max(1, lines):], "file": path}


# ------------------------------------------------------------------ 系统 / 配置

@app.get("/api/systems")
def api_systems(arch: str = "x86", _: str = Depends(require_auth)):
    items, source = store.load_systems((arch or "x86").lower())
    return {"items": items, "source": source, "arch": arch}


@app.get("/api/services")
def api_services(force: int = 0, _: str = Depends(require_auth)):
    """nginx / isc-dhcp-server / tftpd-hpa 的运行状态（只读，手动刷新用）。"""
    return services_mod.status(force=bool(force))


@app.websocket("/ws/services")
async def ws_services(ws: WebSocket, token: str = Query(default="")):
    """服务状态推送：连上先给一份快照，之后只在状态变化时推。"""
    if not verify_token(token):
        await ws.close(code=1008)
        return
    await ws.accept()
    await services_mod.ws_attach(ws)


@app.get("/api/settings")
def api_settings(_: str = Depends(require_auth)):
    return {
        "version": config.APP_VERSION,
        "hosts_dir": config.HOSTS_DIR,
        "hosts_glob": config.HOSTS_GLOB,
        "dhcpd_conf": config.DHCPD_CONF,
        "boot_ipxe": config.BOOT_IPXE,
        "images_dir": config.IMAGES_DIR,
        "ssh_user": config.SSH_USER,
        "ssh_port": config.SSH_PORT,
        "dry_run": config.DRY_RUN,
        "admin_user": config.ADMIN_USER,
    }


@app.post("/api/dhcp/validate")
def api_validate(_: str = Depends(require_auth)):
    ok, out = store.validate_conf()
    return {"ok": ok, "detail": out}


@app.post("/api/dhcp/reload")
def api_reload(_: str = Depends(require_auth)):
    ok, out = store.reload_dhcp()
    return {"ok": ok, "detail": out}


# ------------------------------------------------------------------ 网页 SSH

@app.websocket("/ws/ssh")
async def ws_ssh(ws: WebSocket, token: str = Query(default="")):
    if not verify_token(token):
        await ws.close(code=1008)
        return

    await ws.accept()
    try:
        cfg = await asyncio.wait_for(ws.receive_json(), timeout=30)
    except Exception:
        await ws.close(code=1008)
        return

    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or config.SSH_USER).strip()
    password = cfg.get("password") or ""
    port = int(cfg.get("port") or config.SSH_PORT)
    cols = int(cfg.get("cols") or 120)
    rows = int(cfg.get("rows") or 32)

    if not host:
        await ws.send_text(json.dumps({"type": "error", "message": "缺少目标主机 IP"}))
        await ws.close()
        return

    log.info("建立 SSH 连接：%s@%s:%s", user, host, port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = {
            "port": port,
            "username": user,
            "timeout": config.SSH_TIMEOUT,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if password:
            connect_kwargs["password"] = password
        elif os.path.exists(config.SSH_KEY_FILE or ""):
            connect_kwargs["key_filename"] = config.SSH_KEY_FILE
        await asyncio.to_thread(client.connect, host, **connect_kwargs)
        chan = await asyncio.to_thread(
            lambda: client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        )
    except Exception as exc:  # noqa: BLE001
        log.error("SSH 连接失败 %s@%s：%s", user, host, exc)
        await ws.send_text(json.dumps({"type": "error", "message": f"SSH 连接失败：{exc}"}))
        await ws.close()
        client.close()
        return

    loop = asyncio.get_running_loop()

    def reader() -> None:
        try:
            while True:
                if chan.recv_ready():
                    data = chan.recv(8192)
                    if not data:
                        break
                    asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop)
                elif chan.closed or chan.exit_status_ready():
                    # 排空剩余输出
                    try:
                        tail = chan.recv(8192)
                        while tail:
                            asyncio.run_coroutine_threadsafe(ws.send_bytes(tail), loop)
                            tail = chan.recv(8192)
                    except Exception:
                        pass
                    break
                else:
                    time.sleep(0.02)
        except Exception:
            pass
        finally:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            payload = msg.get("bytes")
            if payload:
                await asyncio.to_thread(chan.send, payload)
                continue
            text = msg.get("text")
            if text:
                try:
                    ctl = json.loads(text)
                except Exception:
                    continue
                if ctl.get("type") == "resize":
                    await asyncio.to_thread(
                        chan.resize_pty,
                        width=int(ctl.get("cols", cols)),
                        height=int(ctl.get("rows", rows)),
                    )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 前端

# 登录页配图：扫 static/img/ 下有没有图片，有就返回 URL，没有返回空串。
# 前端拿不到就不渲染，避免出现裂图和 alt 文字占位。
_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif")
_LOGO_PREFERRED = ("login", "mascot", "logo", "brand")


def find_login_logo() -> str:
    img_dir = os.path.join(STATIC_DIR, "img")
    try:
        names = sorted(
            n for n in os.listdir(img_dir)
            if n.lower().endswith(_LOGO_EXTS) and os.path.isfile(os.path.join(img_dir, n))
        )
    except OSError:
        return ""
    if not names:
        return ""
    # 优先用 login / mascot / logo / brand 开头的，其次才是目录里随便一张图
    for n in names:
        if n.lower().startswith(_LOGO_PREFERRED):
            return "/static/img/" + n
    return "/static/img/" + names[0]


@app.get("/api/branding", include_in_schema=False)
def api_branding():
    """登录页配图。无需登录——登录页本身就要用。"""
    return {"logo": find_login_logo()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
