"""镜像管理。

目录约定（IMAGES_DIR 默认 <项目>/var/www/html）

    var/www/html/
      x86/                          新系统类型（自己起名）
        <镜像名>/
          <原文件名>.iso
          <原文件名 vmlinuz>        不改名，实际文件名记在 .pxe-boot 里
          <原文件名 initrd>
          autoinstall/meta-data
          autoinstall/user-data
      arm/                          同 x86
      UKI/                          内置 vmlinuz / initrd 的现成系统类型
        x86/
          ubuntu-24.04.4-desktop/
            vmlinuz  initrd         自己放进去，平台不改名
            <上传的 ISO>.iso
            autoinstall/meta-data   （可选）
            autoinstall/user-data   （可选）
        arm/
          ...

并自动维��� tftpboot/boot.ipxe（x86）与 tftpboot/grub.cfg（arm）里的菜单项。

URL 路径规则（HTTP 安装源根 = var/www/html）：
  UKI 内置类型 → UKI/<arch>/<类型>/<文件名>
                例如 /var/www/html/UKI/x86/ubuntu-24.04.4-desktop/vmlinuz
  新系统类型   → <arch>/<类型>/<文件名>
                例如 /var/www/html/x86/ubuntu-desk-25/vmlinuz
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Optional

import config
from logger import log

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ITEM_RE = re.compile(r"^\s*item\s+(?:--key\s+\S+\s+)?(\S+)")
LABEL_RE = re.compile(r"^:\S+\s*$")

# 架构：x86 / arm。新布局下由父目录名决定，老镜像读 .pxe-arch 兜底
ARCHES = ("x86", "arm")
DEFAULT_ARCH = "x86"
ARCH_FILE = ".pxe-arch"
# 记录真实上传的内核 / initrd 文件名（不改名，写菜单时要按这个名字拼 URL）
BOOT_FILE = ".pxe-boot"


def uki_root(arch: str) -> str:
    """内置内核（UKI）目录，例如 <www>/x86/UKI。"""
    arch = (arch or DEFAULT_ARCH).lower()
    return os.path.abspath(
        config.UKI_LAYOUT.replace("{www}", os.path.abspath(config.IMAGES_DIR)).replace("{arch}", arch)
    )


def list_uki_types(arch: str) -> list[str]:
    """UKI 目录下已经内置好的系统类型（目录名），按名称排序。"""
    root = uki_root(arch)
    if not os.path.isdir(root):
        return []
    return sorted(
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and NAME_RE.match(d)
    )


def _uki_dir(arch: str, uki_type: str) -> str:
    root = uki_root(arch)
    path = os.path.abspath(os.path.join(root, uki_type or ""))
    if os.path.dirname(path) != root or not NAME_RE.match(uki_type or ""):
        raise ValueError("非法系统类型")
    return path


def _read_arch(path: str) -> str:
    # 新布局：父目录名就是架构
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if parent in ARCHES:
        return parent
    try:
        with open(os.path.join(path, ARCH_FILE), "r", encoding="utf-8", errors="ignore") as fh:
            value = fh.read().strip().lower()
    except OSError:
        return DEFAULT_ARCH
    return value if value in ARCHES else DEFAULT_ARCH


def _image_dir(name: str, arch: Optional[str] = None) -> str:
    """镜像目录。给 arch 时用对应架构目录；不给就自动找（兼容老扁平目录）。

    同名目录在 x86 / arm 下都存在时（UKI 骨架很常见，比如两边都有 dgxos-7.5.0），
    优先返回「真正当过镜像用」的那个 —— 即带 .pxe-arch / .pxe-boot 标记或含 ISO
    的目录，空的骨架目录不会被误选。
    """
    if not NAME_RE.match(name or ""):
        raise ValueError("镜像名只能包含字母、数字、点、下划线和中划线")
    base = os.path.abspath(config.IMAGES_DIR)

    def _is_real(d: str) -> bool:
        """目录是否被当成镜像用过（上传过 ISO 或写过标记）。"""
        try:
            files = os.listdir(d)
        except OSError:
            return False
        if ARCH_FILE in files or BOOT_FILE in files:
            return True
        return any(f.lower().endswith(".iso") for f in files)

    if arch:
        arch = (arch or DEFAULT_ARCH).lower()
        if arch not in ARCHES:
            raise ValueError(f"架构必须是 {ARCHES[0]} 或 {ARCHES[1]}")
        root = os.path.abspath(os.path.join(base, arch))
        path = os.path.abspath(os.path.join(root, name))
        if os.path.dirname(path) != root:
            raise ValueError("非法镜像名")
        # 选的是 UKI 里已有的类型时，目录在 UKI/<架构>/<类型>
        if not os.path.isdir(path):
            uki = os.path.join(uki_root(arch), name)
            if os.path.isdir(uki):
                return uki
        return path

    # 无架构：按"用过 > 空骨架"挑，都一样就取第一个
    candidates: list[str] = []
    for a in ARCHES:
        p = os.path.join(base, a, name)
        if os.path.isdir(p):
            candidates.append(p)
        uki = os.path.join(uki_root(a), name)
        if os.path.isdir(uki):
            candidates.append(uki)
    real = [c for c in candidates if _is_real(c)]
    if real:
        return real[0]
    if candidates:
        return candidates[0]
    path = os.path.abspath(os.path.join(base, name))
    if os.path.dirname(path) != base:
        raise ValueError("非法镜像名")
    return path


def _url_path(name: str, arch: str) -> str:
    """HTTP 上访问该镜像的路径（相对 DocumentRoot）。

    - UKI 内置类型（系统类型选的是 UKI 里已有的）：
        UKI/<arch>/<类型>/<filename>
        例如 /var/www/html/UKI/x86/ubuntu-24.04.4-desktop/vmlinuz
    - 新系统类型（自己起名）：
        <arch>/<类型>/<filename>
        例如 /var/www/html/x86/ubuntu-desk-25/vmlinuz
    """
    try:
        uki_root_dir = uki_root(arch)
    except Exception:  # noqa: BLE001
        uki_root_dir = ""
    target = _image_dir(name, arch)
    if uki_root_dir and os.path.dirname(os.path.abspath(target)) == uki_root_dir:
        return config.UKI_URL_PATH_TPL.replace("{arch}", arch).replace(
            "{name}", os.path.basename(target)
        )
    return config.URL_PATH_TPL.replace("{arch}", arch).replace("{name}", name)


def _read_boot_meta(path: str) -> dict:
    try:
        with open(os.path.join(path, BOOT_FILE), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_boot_meta(path: str, data: dict) -> None:
    try:
        with open(os.path.join(path, BOOT_FILE), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("写入 %s 失败：%s", BOOT_FILE, exc)


# 内核 / initrd 的常见文件名前缀。不改名策略下什么都可能出现，
# ARM 的内核经常就叫 Image / Image.gz，x86 也有 bzImage 的老叫法，都得认。
KERNEL_PREFIXES = ("vmlinuz", "vmlinux", "bzimage", "image", "kernel", "linux")
INITRD_PREFIXES = ("initrd", "initramfs", "initrd.img")


def kernel_names(path: str) -> tuple[str, str]:
    """目录里 vmlinuz / initrd 的真实文件名（不改名，所以不能写死）。"""
    meta = _read_boot_meta(path)
    vmlinuz, initrd = meta.get("vmlinuz") or "", meta.get("initrd") or ""

    # 缓存里记的名字如果文件已经不在了（手工删过 / 换过），就当没记过，
    # 否则列表会显示 vmlinuz_ok=True 但菜单指向一个不存在的 URL
    if vmlinuz and not os.path.isfile(os.path.join(path, vmlinuz)):
        vmlinuz = ""
    if initrd and not os.path.isfile(os.path.join(path, initrd)):
        initrd = ""

    def pick(cached: str, *candidates: str) -> str:
        for c in (cached,) + candidates:
            if c and os.path.isfile(os.path.join(path, c)):
                return c
        return ""

    if not vmlinuz:
        try:
            names = os.listdir(path)
        except OSError:
            names = []
        vmlinuz = _first_match(names, KERNEL_PREFIXES)
    if not initrd:
        try:
            names = os.listdir(path)
        except OSError:
            names = []
        initrd = _first_match(names, INITRD_PREFIXES)
    return pick(vmlinuz, "vmlinuz"), pick(initrd, "initrd")


def _first_match(names, prefixes) -> str:
    """按前缀找文件：先精确匹配再前缀匹配；ISO 不是内核，跳过。"""
    files = [n for n in sorted(names) if not n.lower().endswith(".iso")]
    for p in prefixes:
        for n in files:
            if n.lower() == p:
                return n
    for p in prefixes:
        for n in files:
            if n.lower().startswith(p):
                return n
    return ""


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TB"


# ------------------------------------------------------------------ 列表

def list_images() -> list[dict]:
    base = os.path.abspath(config.IMAGES_DIR)
    if not os.path.isdir(base):
        return []

    # 新布局：<www>/<arch>/<name> 与 <www>/<arch>/UKI/<type>
    # 老的扁平布局（<www>/<name>）也一并兼容
    found: list[tuple[str, str]] = []      # (架构, 目录)
    for arch in ARCHES:
        root = os.path.join(base, arch)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path) or name == config.UKI_FOLDER:
                continue
            found.append((arch, path))
        for uki in list_uki_types(arch):
            found.append((arch, os.path.join(uki_root(arch), uki)))
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        # UKI 是目录骨架不是镜像；x86 / arm 是架构目录，也跳过
        if os.path.isdir(path) and name not in ARCHES and name != config.UKI_FOLDER:
            found.append((DEFAULT_ARCH, path))

    items, seen = [], set()
    for arch, path in found:
        name = os.path.basename(path)
        key = (arch, name)
        if key in seen:
            continue
        seen.add(key)
        try:
            files = set(os.listdir(path))
        except OSError:
            continue
        # UKI 目录里没放 ISO 时，说明还没当成镜像用，列表里先不显示
        if os.path.dirname(os.path.abspath(path)) == uki_root(arch) \
                and not any(f.lower().endswith(".iso") for f in files):
            continue
        iso = next((f for f in sorted(files) if f.lower().endswith(".iso")), "")
        ai_dir = os.path.join(path, config.AUTOINSTALL_DIRNAME)
        ai_exists = os.path.isdir(ai_dir)
        ai_files = []
        if ai_exists:
            ai_files = sorted(f for f in os.listdir(ai_dir) if os.path.isfile(os.path.join(ai_dir, f)))
        vmlinuz, initrd = kernel_names(path)
        # 只算一次：原来这里调了两遍 _dir_size，大镜像目录遍历不便宜
        size = _dir_size(path)
        items.append(
            {
                "name": name,
                "iso": iso,
                "arch": _read_arch(path) or arch,
                "uki": os.path.dirname(os.path.abspath(path)) == uki_root(arch),
                "vmlinuz": vmlinuz,
                "initrd": initrd,
                "vmlinuz_ok": bool(vmlinuz),
                "initrd_ok": bool(initrd),
                "autoinstall": ai_files,
                "autoinstall_dir": ai_exists,
                "size": size,
                "size_human": human_size(size),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))),
            }
        )
    items.sort(key=lambda r: (r["arch"], r["name"].lower()))
    return items


# ------------------------------------------------------------------ 新增

def _save(upload, dest: str) -> str:
    """存上传文件，返回落盘路径（供回滚用）。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
    except Exception:
        # 传输中断时文件已经建出来了（哪怕是空的），自己先清干净：
        # 残留的半截 ISO 会让 UKI 类型下次上传被"已经有 ISO 了"永久挡住
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    log.info("  写入文件 %s（%s）", dest, human_size(written))
    return dest


def _orig_name(upload, fallback: str) -> str:
    """上传文件的原始文件名，不改写、不重命名（只做路径穿越防护）。"""
    raw = os.path.basename((getattr(upload, "filename", "") or "").replace("\\", "/").strip())
    return raw or fallback


def save_autoinstall(upload, ai: str, kind: str, arch: str) -> list[str]:
    """存 meta-data / user-data：保留原文件名，同时补一份 cloud-init 认的标准名副本。

    为什么还要副本：cloud-init 的 nocloud-net 数据源只认 autoinstall/ 下**字面叫**
    `meta-data` / `user-data` 的文件（`ds=nocloud-net;s=<目录>` 指向的是目录，不是文件）；
    ARM（DGX）的 `force-ai` 又固定去读 `server.yaml`。用户要求"上传时叫什么就存什么"，
    所以原名照存，另外复制一份标准名过去，两边都照顾到。文件通常只有几 KB，代价可忽略。
    """
    dest = _save(upload, os.path.join(ai, _orig_name(upload, kind)))
    written = [dest]

    def copy_as(std_name: str) -> None:
        std = os.path.join(ai, std_name)
        if os.path.abspath(std) == os.path.abspath(dest):
            return                      # 上传时用的就是标准名，不用复制
        try:
            shutil.copyfile(dest, std)
            written.append(std)
            log.info("  同步 %s 副本（cloud-init 需要）", std)
        except OSError as exc:
            log.warning("复制 %s 失败：%s", std, exc)

    copy_as(kind)                                       # meta-data / user-data
    if kind == "user-data" and arch == "arm":
        copy_as("server.yaml")                          # DGX 的 force-ai 固定读这个
    return written


def rollback(path: str, written: list[str], using_uki: bool) -> None:
    """上传中途失败时的清理。

    UKI 分支不能整个删目录（vmlinuz / initrd 是用户放进去的），只能按清单删掉
    本次写的文件——否则残留的半截 ISO 会让下次上传被"已经有 ISO 了"永久挡住。
    """
    if not using_uki:
        shutil.rmtree(path, ignore_errors=True)
        log.info("创建失败，已删除目录 %s", path)
        return
    for fp in written:
        try:
            os.remove(fp)
        except OSError:
            pass
    if written:
        log.info("创建失败，已回滚 %s 个文件", len(written))
    ai = os.path.join(path, config.AUTOINSTALL_DIRNAME)
    if os.path.isdir(ai) and not _safe_listdir(ai):
        shutil.rmtree(ai, ignore_errors=True)


def add_image(
    name: str,
    iso,
    initrd=None,
    vmlinuz=None,
    meta_data=None,
    user_data=None,
    arch: str = DEFAULT_ARCH,
    uki_type: str = "",
) -> dict:
    """新增镜像。

    uki_type 非空 = 选了 UKI 里已有的系统类型：内核 / initrd 用 UKI/<类型>/ 下内置的那份，
    只收 ISO（vmlinuz / initrd 不允许上传），但 meta-data / user-data 可选上传；
    uki_type 空 = 新系统类型：自己建目录，vmlinuz / initrd / meta-data / user-data 全都要，
    meta-data / user-data 可选。

    文件名策略：**一律保留原文件名**（上传时叫什么就存什么），平台不再重命名为
    vmlinuz/initrd/meta-data/user-data；真实文件名记在 .pxe-boot 里，菜单按这个拼 URL。
    """
    arch = (arch or DEFAULT_ARCH).lower()
    if arch not in ARCHES:
        raise ValueError(f"架构必须是 {ARCHES[0]} 或 {ARCHES[1]}")

    uki_type = (uki_type or "").strip()
    using_uki = bool(uki_type) and uki_type != config.NEW_TYPE_LABEL

    if using_uki:
        if uki_type not in list_uki_types(arch):
            raise ValueError(f"{arch} 的 UKI 目录里没有「{uki_type}」这个系统类型")
        if vmlinuz is not None or initrd is not None:
            raise ValueError(
                "选择已有系统类型时不能再传 vmlinuz / initrd，内核由 UKI 目录自带"
            )
        name = uki_type
        path = _uki_dir(arch, uki_type)
        if any(f.lower().endswith(".iso") for f in _safe_listdir(path)):
            raise ValueError(f"系统类型「{uki_type}」已经有 ISO 了，先删除再重新上传")
    else:
        if not NAME_RE.match(name or ""):
            raise ValueError("镜像名只能包含字母、数字、点、下划线和中划线")
        if name in list_uki_types(arch):
            raise ValueError(f"「{name}」是已存在的系统类型，请直接在上面选中它")
        path = os.path.join(os.path.abspath(config.IMAGES_DIR), arch, name)
        if os.path.exists(path):
            raise ValueError(f"镜像 {name} 已存在")
        # 新类型没有内置内核可依赖，缺了就装不起来，别等到装机时才炸
        if vmlinuz is None or initrd is None:
            raise ValueError("新系统类型必须上传 vmlinuz 和 initrd")

    iso_name = _orig_name(iso, f"{name}.iso")
    written: list[str] = []      # 本次真正写进去的文件，出错时按清单回滚
    try:
        os.makedirs(path, exist_ok=True)
        meta = _read_boot_meta(path)
        meta["iso"] = iso_name
        written.append(_save(iso, os.path.join(path, iso_name)))

        if not using_uki:
            # 新系统类型：vmlinuz / initrd 必传，不改名存原文件名
            if vmlinuz is not None:
                vname = _orig_name(vmlinuz, "vmlinuz")
                written.append(_save(vmlinuz, os.path.join(path, vname)))
                meta["vmlinuz"] = vname
            if initrd is not None:
                iname = _orig_name(initrd, "initrd")
                written.append(_save(initrd, os.path.join(path, iname)))
                meta["initrd"] = iname

        # autoinstall/ 不管哪种类型都建，meta / user 是可选的
        ai = os.path.join(path, config.AUTOINSTALL_DIRNAME)
        os.makedirs(ai, exist_ok=True)
        if meta_data is not None:
            written += save_autoinstall(meta_data, ai, "meta-data", arch)
        if user_data is not None:
            written += save_autoinstall(user_data, ai, "user-data", arch)
        if using_uki:
            meta.pop("vmlinuz", None)
            meta.pop("initrd", None)

        meta["arch"] = arch
        meta["uki"] = uki_type if using_uki else ""
        _write_boot_meta(path, meta)
        with open(os.path.join(path, ARCH_FILE), "w", encoding="utf-8") as fh:
            fh.write(arch + "\n")
    except Exception:
        rollback(path, written, using_uki)
        raise

    if arch == "arm":
        changed = append_to_grub_cfg(name, iso_name, arch)
    else:
        changed = append_to_boot_ipxe(name, iso_name, arch)
    return {
        "ok": True,
        "message": f"镜像 {name} 已创建",
        "path": path,
        "arch": arch,
        "uki": uki_type if using_uki else "",
        "menu": changed,
    }


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


# ------------------------------------------------------------------ 删除

def delete_image(name: str) -> dict:
    path = _image_dir(name)
    if not os.path.isdir(path):
        raise ValueError(f"镜像 {name} 不存在")
    arch = _read_arch(path)

    if os.path.dirname(os.path.abspath(path)) == uki_root(arch):
        # UKI 目录里的 vmlinuz / initrd 是内置的，只清掉上传进去的 ISO / autoinstall / 标记文件
        removed = []
        for f in _safe_listdir(path):
            fp = os.path.join(path, f)
            low = f.lower()
            if (
                low.endswith(".iso")
                or f in (ARCH_FILE, BOOT_FILE)
                # 顺带清掉跟这张 ISO 配套的 meta / user-data（如果有按原名上传进来的话）
                or low in ("meta-data", "user-data", "metadata", "userdata")
            ):
                try:
                    os.remove(fp)
                    removed.append(f)
                except OSError:
                    pass
        ai = os.path.join(path, config.AUTOINSTALL_DIRNAME)
        if os.path.isdir(ai):
            for f in _safe_listdir(ai):
                try:
                    os.remove(os.path.join(ai, f))
                except OSError:
                    pass
            shutil.rmtree(ai, ignore_errors=True)
        log.info("清理 UKI 类型 %s：%s", name, removed or "无文件")
        tip = "（UKI 类型只清 ISO / autoinstall，内置内核保留）"
    else:
        shutil.rmtree(path)
        tip = ""

    # 两种菜单都清理一遍，避免架构变更后残留
    remove_from_boot_ipxe(name)
    remove_from_grub_cfg(name)
    _grub_drop_autoselect_file(name)
    return {"ok": True, "message": f"镜像 {name} 已删除（{arch}），菜单已同步{tip}"}


def _grub_drop_autoselect_file(name: str) -> bool:
    lines = _grub_lines()
    if not lines:
        return False
    out = _grub_drop_autoselect(lines, name)
    if out == lines:
        return False
    _grub_write(out)
    return True


# ------------------------------------------------------------------ GRUB（ARM）

GRUB_TEMPLATE = """menuentry '{name}' {
    echo 'Loading Linux'
    linux (http,$server_ip)/{path}/{vmlinuz} fsck.mode=skip ip=dhcp earlycon nouveau.modeset=0 force-platform=dgx_baseos nooemconfig rebuild-raid no-reorder-uefi nopersistent  systemd.mask=cloud-init-local.service  systemd.mask=cloud-init.service  systemd.mask=cloud-config.service  systemd.mask=cloud-final.service  systemd.mask=systemd-networkd-wait-online.service  systemd.debug-shell=1  console=tty0,tty1 console=ttyS0,921600 console=ttyAMA0,115200  loglevel=7 systemd.log_level=debug offwhendone  no-nvsm-enable autoinstall url=http://$server_ip/{path}/{iso} no-cloudinit force-ai=http://$server_ip/{path}/autoinstall/server.yaml ---
    echo 'Loading initrd'
    initrd (http,$server_ip)/{path}/{initrd}
    boot
}"""


def _grub_lines() -> list[str]:
    if not os.path.exists(config.GRUB_CFG):
        return []
    with open(config.GRUB_CFG, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read().splitlines()


def _grub_write(lines: list[str]) -> None:
    directory = os.path.dirname(config.GRUB_CFG)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(config.GRUB_CFG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def remove_from_grub_cfg(name: str) -> bool:
    lines = _grub_lines()
    if not lines:
        return False
    out, i, changed = [], 0, False
    while i < len(lines):
        if re.match(rf"^\s*menuentry\s+'{re.escape(name)}'\s*\{{", lines[i]):
            changed = True
            depth = 0
            while i < len(lines):
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
                if depth <= 0:
                    break
            continue
        out.append(lines[i])
        i += 1
    if changed:
        _grub_write(out)
    return changed


def _grub_autoselect_start(lines: list[str]) -> Optional[int]:
    """grub.cfg 里 `if net_get_dhcp_option ipxe_menu_item ...` 判断块的起始行。"""
    for i, line in enumerate(lines):
        if re.match(r"^\s*if\s+net_get_dhcp_option\s+ipxe_menu_item\b", line):
            return i
    return None


def _grub_insert_menuentry(lines: list[str], block: list[str]) -> list[str]:
    """把完整的 menuentry 块插到**自动选择判断块之前**（也就是

        else
            set default="Boot-from-next-volume"
            set timeout=-1

    那一整段的前面），保证兜底逻辑始终留在文件末尾。
    """
    idx = _grub_autoselect_start(lines)
    if idx is None:
        lines += [""] + list(block)
    else:
        lines[idx:idx] = list(block) + [""]
    return lines


def append_to_grub_cfg(name: str, iso_name: str, arch: Optional[str] = None) -> bool:
    remove_from_grub_cfg(name)  # 已存在则先替换

    path = _image_dir(name, arch)
    arch = _read_arch(path)
    vmlinuz, initrd = kernel_names(path)
    lines = _grub_lines()
    block = (
        GRUB_TEMPLATE
        .replace("{name}", name)
        .replace("{path}", _url_path(name, arch))
        .replace("{iso}", iso_name)
        .replace("{vmlinuz}", vmlinuz or "vmlinuz")
        .replace("{initrd}", initrd or "initrd")
    ).split("\n")
    _grub_insert_menuentry(lines, block)
    _grub_add_autoselect(lines, name)
    _grub_write(lines)
    return True


AUTOSELECT_TEMPLATE = """if net_get_dhcp_option ipxe_menu_item ${net_default_interface} 224 string; then
    echo "DHCP menu item: ${ipxe_menu_item}"
    # 只对已知菜单项启用自动启动
    if [ "${ipxe_menu_item}" = "{name}" ]; then
        set default="{name}"
        set timeout=3
    fi
else
    echo "DHCP Option 224 unavailable; select an entry manually."
fi"""

COND_RE = r'^\s*(?:if|elif)\s+\[\s*"\$\{ipxe_menu_item\}"\s*=\s*"__NAME__"\s*\]\s*;\s*then\s*$'


def _grub_drop_autoselect(lines: list[str], name: str) -> list[str]:
    """删除某个镜像对应的 if/elif 分支。"""
    pattern = re.compile(COND_RE.replace("__NAME__", re.escape(name)))
    out, i = [], 0
    while i < len(lines):
        if pattern.match(lines[i]):
            i += 1
            while i < len(lines) and not re.match(r"^\s*(?:elif|else|fi)\b", lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1

    # 若删掉的是首个 if 分支，把紧跟的 elif 提升为 if
    seen_if = False
    for idx, line in enumerate(out):
        if re.match(r'^\s*if\s+\[\s*"\$\{ipxe_menu_item\}"', line):
            seen_if = True
        elif re.match(r'^\s*elif\s+\[\s*"\$\{ipxe_menu_item\}"', line):
            if not seen_if:
                out[idx] = line.replace("elif", "if", 1)
                seen_if = True
        elif re.match(r"^\s*fi\s*$", line):
            seen_if = False

    # 一个分支都不剩 → 删掉整个 net_get_dhcp_option 判断块
    if not any(re.search(r'(?:if|elif)\s+\[\s*"\$\{ipxe_menu_item\}"', line) for line in out):
        start = next(
            (idx for idx, line in enumerate(out)
             if re.match(r"^\s*if\s+net_get_dhcp_option\s+ipxe_menu_item\b", line)),
            None,
        )
        if start is not None:
            end = start + 1
            depth = 1
            while end < len(out):
                if re.match(r"^\s*if\b", out[end]):
                    depth += 1
                elif re.match(r"^\s*fi\s*$", out[end]):
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            out = out[:start] + out[end + 1 :]
    return out


def _grub_add_autoselect(lines: list[str], name: str) -> None:
    """在 grub.cfg 的 ipxe_menu_item 判断块里插入（或替换）elif 分支。"""
    lines[:] = _grub_drop_autoselect(lines, name)

    branch = [
        f'    elif [ "${{ipxe_menu_item}}" = "{name}" ]; then',
        f'        set default="{name}"',
        "        set timeout=3",
    ]

    start = next(
        (idx for idx, line in enumerate(lines)
         if re.match(r'^\s*if\s+\[\s*"\$\{ipxe_menu_item\}"\s*=', line)),
        None,
    )
    if start is None:
        # 还没有判断块，整块追加
        lines.extend(["", AUTOSELECT_TEMPLATE.replace("{name}", name)])
        return

    # 必须插在 else 之前：grub 的 if/elif/else 顺序错了整段判断就失效
    end, depth = start + 1, 0
    while end < len(lines):
        if re.match(r"^\s*if\b", lines[end]):
            depth += 1
        elif re.match(r"^\s*fi\s*$", lines[end]):
            if depth == 0:
                break
            depth -= 1
        elif re.match(r"^\s*else\s*$", lines[end]) and depth == 0:
            break
        end += 1
    lines[end:end] = branch


# ------------------------------------------------------------------ 兜底菜单

FALLBACK_ENTRY = "Boot from next volume"
FALLBACK_BLOCK = "menuentry 'Boot from next volume' {\n\texit 1\n}"
FALLBACK_SHELL_ITEM = "item shell          Enter iPXE Shell（未指定系统时停在这里）"
FALLBACK_NORM = "bootfromnextvolume"


def _norm(text: str) -> str:
    """归一化菜单标题：忽略空格、连字符、下划线和大小写。"""
    return re.sub(r"[\s_\-]+", "", text or "").lower()


def _iter_menuentries(lines: list[str]):
    """产出 (起始行, 结束行, 标题)。"""
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*menuentry\s+'([^']+)'\s*\{", lines[i])
        if not m:
            i += 1
            continue
        depth, k = 0, i
        while k < len(lines):
            depth += lines[k].count("{") - lines[k].count("}")
            k += 1
            if depth <= 0:
                break
        yield i, k, m.group(1)
        i = k


def _is_fallback_entry(title: str, body: list[str]) -> bool:
    if _norm(title) == FALLBACK_NORM:
        return True
    # 兜底项特征：标题带 next，且内容只有一行 exit
    compact = [line.strip() for line in body if line.strip()]
    return "next" in title.lower() and len(compact) == 1 and compact[0].startswith("exit")


def find_fallback_title(lines: list[str] | None = None) -> str | None:
    """找到 grub.cfg 里已有的兜底菜单项，返回它的**真实标题**。"""
    lines = lines if lines is not None else _grub_lines()
    for start, end, title in _iter_menuentries(lines):
        if _is_fallback_entry(title, lines[start + 1 : end - 1]):
            return title
    return None


def _dedupe_fallback_entries(lines: list[str]) -> tuple[list[str], int]:
    """兜底项重复时只保留一个：优先保留**非平台默认名**的那个（即用户自己写的）。"""
    entries = [
        (start, end, title)
        for start, end, title in _iter_menuentries(lines)
        if _is_fallback_entry(title, lines[start + 1 : end - 1])
    ]
    if len(entries) <= 1:
        return lines, 0

    keep = next(
        (e for e in entries if _norm(e[2]) != _norm(FALLBACK_ENTRY)),
        entries[0],
    )
    for start, end, _title in reversed(entries):
        if (start, end) == (keep[0], keep[1]):
            continue
        while start > 0 and not lines[start - 1].strip():
            start -= 1  # 顺带删掉分隔空行
        lines = lines[:start] + lines[end:]
    return lines, len(entries) - 1


def ensure_grub_fallback() -> bool:
    """让"没匹配到系统"的机器停在兜底项上。

    - 默认**不新增** menuentry（由用户自己维护），只有 PXE_GRUB_FALLBACK_ADD=1 时才补；
    - 已存在兜底项时，保证 else 分支的 set default 用的是它**真实的标题**
      （避免 'Boot from next volume' 与 'Boot-from-next-volume' 对不上）；
    - 兜底项重复时只保留第一个。
    """
    lines = _grub_lines()
    if not lines:
        return False
    changed = False

    lines, removed = _dedupe_fallback_entries(lines)
    if removed:
        changed = True
        log.info("grub.cfg 发现 %s 个重复兜底项，已只保留第一个", removed)

    title = find_fallback_title(lines)
    if title is None and config.GRUB_FALLBACK_ADD:
        lines += ["", FALLBACK_BLOCK.replace(FALLBACK_ENTRY, config.GRUB_FALLBACK)]
        title = config.GRUB_FALLBACK
        changed = True
        log.info("grub.cfg 已补充兜底菜单项：%s", title)

    if title is not None:
        start = next(
            (idx for idx, line in enumerate(lines)
             if re.match(r'^\s*if\s+\[\s*"\$\{ipxe_menu_item\}"\s*=', line)),
            None,
        )
        if start is not None:
            end = start + 1
            while end < len(lines) and not re.match(r"^\s*fi\s*$", lines[end]):
                end += 1
            else_idx = next(
                (i for i in range(start + 1, end) if re.match(r"^\s*else\s*$", lines[i])), None
            )
            if else_idx is None:
                if end < len(lines):
                    lines[end:end] = [
                        "    else",
                        f'        set default="{title}"',
                        "        set timeout=-1",
                    ]
                    changed = True
            else:
                for i in range(else_idx + 1, end):
                    m = re.match(r"^(\s*set\s+default\s*=\s*)(.*)$", lines[i])
                    if m:
                        # grub 的 default 必须与 menuentry 标题**逐字符一致**，
                        # 所以这里不能用归一化比较（'Boot from next volume' ≠ 'Boot-from-next-volume'）
                        current = m.group(2).strip().strip('"')
                        if current != title:
                            lines[i] = f'{m.group(1)}"{title}"'
                            changed = True
                    if re.match(r"^\s*set\s+timeout\s*=", lines[i]) and not re.match(
                        r"^\s*set\s+timeout\s*=\s*-1\s*$", lines[i]
                    ):
                        lines[i] = "        set timeout=-1"
                        changed = True

    if changed:
        _grub_write(lines)
        log.info("grub.cfg 兜底项已对齐：%s（不倒计时）", title)
    return changed


def ensure_ipxe_fallback() -> bool:
    """boot.ipxe：确保有 shell 菜单项、choose 不倒计时、224 未匹配时回退到菜单。"""
    lines = _boot_lines()
    if not lines:
        return False
    changed = False

    if not any(re.match(r"^\s*item\s+(?:--key\s+\S+\s+)?shell\b", line) for line in lines):
        idxs = [i for i, line in enumerate(lines) if line.strip().startswith("item")]
        if idxs:
            pos = idxs[-1] + 1
        else:
            pos = next(
                (i for i, line in enumerate(lines) if line.strip().startswith("choose")),
                len(lines),
            )
        lines.insert(pos, FALLBACK_SHELL_ITEM)
        changed = True

    for i, line in enumerate(lines):
        if re.match(r"^\s*choose\b", line) and "--timeout" in line:
            lines[i] = re.sub(r"\s*--timeout\s+\d+", "", line)
            changed = True
        if "goto ${menu-item}" in line and "||" not in line:
            lines[i] = line.rstrip() + " || goto show_menu"
            changed = True

    if changed:
        _boot_write(lines)
        log.info("boot.ipxe 已补充 shell 兜底项并关闭菜单倒计时")
    return changed


def ensure_fallback_menus() -> dict:
    """幂等补齐两边的兜底配置；已有正确配置不会重复写入。"""
    result = {"grub": False, "ipxe": False}
    try:
        result["grub"] = ensure_grub_fallback()
    except Exception as exc:  # noqa: BLE001
        log.warning("处理 grub.cfg 兜底项失败：%s", exc)
    try:
        result["ipxe"] = ensure_ipxe_fallback()
    except Exception as exc:  # noqa: BLE001
        log.warning("处理 boot.ipxe 兜底项失败：%s", exc)
    return result


# ------------------------------------------------------------------ 菜单内容编辑

def _boot_label_range(lines: list[str], name: str):
    """boot.ipxe 里 `:name` 标签块的内容区间 (start, end)，不含标签行本身。"""
    for i, line in enumerate(lines):
        if re.match(rf"^:{re.escape(name)}\s*$", line.strip()):
            j = i + 1
            while j < len(lines) and not LABEL_RE.match(lines[j].strip()):
                j += 1
            return i + 1, j
    return None


def _grub_menuentry_range(lines: list[str], name: str):
    """grub.cfg 里 menuentry 'name' 花括号内部的区间 (start, end)。"""
    for start, end, title in _iter_menuentries(lines):
        if title == name:
            return start + 1, end - 1
    return None


# 注意：这里用 .replace() 而不是 .format()，所以 iPXE 自己的 ${...} 只写单花括号
DEFAULT_X86_BLOCK = """echo "Starting the installation of {name} ..."
set base-url http://${server-ip}/{path}
kernel ${base-url}/{vmlinuz} ip=dhcp url=${base-url}/{iso} autoinstall ds=nocloud-net;s=${base-url}/autoinstall/ --- || goto failed
initrd ${base-url}/{initrd} || goto failed
boot
goto booted"""


def _iso_of(path: str) -> str:
    try:
        for f in sorted(os.listdir(path)):
            if f.lower().endswith(".iso"):
                return f
    except OSError:
        pass
    return "your-image.iso"


# ------------------------------------------------------------------ 页缓存预热


def _is_kernel_like(low: str) -> bool:
    """文件名看着像内核 / initrd 吗（不改名策略下的兜底判断）。"""
    return any(low.startswith(p) for p in KERNEL_PREFIXES + INITRD_PREFIXES)


def _prewarm_file(path: str, deep: bool = False) -> dict:
    """把文件塞进内核页缓存。

    POSIX_FADV_WILLNEED 是异步的、立刻返回，内核会在后台把内容读进来；
    deep=True 时再同步顺序读一遍，确保返回时已经真正热了（大 ISO 会慢一些）。
    """
    info = {
        "file": os.path.basename(path),
        "size": os.path.getsize(path),
        "size_human": human_size(os.path.getsize(path)),
        "advised": False,
        "read": False,
    }
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(os, "posix_fadvise"):
            for advice in ("POSIX_FADV_SEQUENTIAL", "POSIX_FADV_WILLNEED"):
                value = getattr(os, advice, None)
                if value is None:
                    continue
                try:
                    os.posix_fadvise(fd, 0, 0, value)
                    info["advised"] = True
                except OSError:
                    pass
        if deep:
            step = 8 * 1024 * 1024
            while os.read(fd, step):
                pass
            info["read"] = True
    finally:
        os.close(fd)
    return info


def prewarm_image(name: str, deep: bool = False) -> dict:
    """预热某个镜像的 ISO / vmlinuz / initrd，批量装机前先跑一次能明显削峰。"""
    path = _image_dir(name)
    if not os.path.isdir(path):
        raise ValueError(f"镜像 {name} 不存在")

    # 内核 / initrd 可能是任意文件名（不改名策略），所以按 kernel_names() 取真名，
    # 不能再拿 "vmlinuz" / "initrd" 去完全匹配，否则 vmlinuz-6.14-custom 这种会被漏掉
    kernels = {n for n in kernel_names(path) if n}
    targets = []
    for f in sorted(os.listdir(path)):
        fp = os.path.join(path, f)
        if not os.path.isfile(fp):
            continue
        low = f.lower()
        if low.endswith((".iso", ".squashfs")) or f in kernels or _is_kernel_like(low):
            targets.append(fp)
    if not targets:
        raise ValueError(f"镜像 {name} 下没有可预热的 ISO / vmlinuz / initrd")

    files, total = [], 0
    for fp in targets:
        try:
            info = _prewarm_file(fp, deep=deep)
        except OSError as exc:
            files.append({"file": os.path.basename(fp), "error": str(exc)})
            continue
        total += info["size"]
        files.append(info)

    log.info("预热镜像 %s：%s 个文件，%s（deep=%s）", name, len(files), human_size(total), deep)
    return {
        "ok": True,
        "message": f"已提交预热：{len(files)} 个文件，共 {human_size(total)}",
        "files": files,
        "total": total,
        "total_human": human_size(total),
    }


def _default_block(name: str, path: str) -> str:
    iso = _iso_of(path)
    arch = _read_arch(path)
    vmlinuz, initrd = kernel_names(path)
    if arch == "arm":
        body = (
            GRUB_TEMPLATE
            .replace("{name}", name)
            .replace("{path}", _url_path(name, arch))
            .replace("{iso}", iso)
            .replace("{vmlinuz}", vmlinuz or "vmlinuz")
            .replace("{initrd}", initrd or "initrd")
        )
        return "\n".join(body.splitlines()[1:-1])  # 只要花括号内部
    return (
        DEFAULT_X86_BLOCK
        .replace("{name}", name)
        .replace("{path}", _url_path(name, arch))
        .replace("{iso}", iso)
        .replace("{vmlinuz}", vmlinuz or "vmlinuz")
        .replace("{initrd}", initrd or "initrd")
    )


def _scan_menu(name: str, kind: str) -> dict:
    """在指定菜单文件里找镜像的启动配置块。

    kind: 'grub' → grub.cfg 的 menuentry；'ipxe' → boot.ipxe 的 `:name` 标签块。
    """
    if kind == "grub":
        source, marker = config.GRUB_CFG, f"menuentry '{name}' {{"
        lines, exists = _grub_lines(), os.path.exists(config.GRUB_CFG)
        rng = _grub_menuentry_range(lines, name)
        header, footer = marker, "}"
    else:
        source, marker = config.BOOT_IPXE, f":{name}"
        lines, exists = _boot_lines(), os.path.exists(config.BOOT_IPXE)
        rng = _boot_label_range(lines, name)
        header, footer = marker, ""

    if not exists:
        detail = "文件不存在"
    elif not lines:
        detail = "文件为空"
    elif rng is None:
        detail = f"未找到 `{marker}`" + ("...}" if kind == "grub" else " 标签块")
    else:
        detail = "已找到"

    return {
        "kind": kind, "source": source, "lines": lines, "range": rng,
        "header": header, "footer": footer, "detail": detail,
    }


def _menu_missing_message(name: str, arch: str, reports: list[dict]) -> str:
    text = [f"未找到镜像「{name}」的启动配置（架构：{arch}）。", "", "已按顺序检查以下菜单文件："]
    for i, r in enumerate(reports, 1):
        text.append(f"  {i}. {r['source']}  →  {r['detail']}")
    text += [
        "",
        "可能原因：",
        "  1. 该镜像由旧版本创建，菜单项当时没写进文件；",
        "  2. 菜单文件被手工改过，菜单项名称与镜像名不一致；",
        "  3. 镜像架构与菜单文件不匹配（x86 写在 boot.ipxe，arm 写在 grub.cfg）。",
        "",
        "处理办法：删除该镜像后重新上传（会自动写入菜单项），"
        "或直接在对应菜单文件里手工补一段同名配置块。",
    ]
    return "\n".join(text)


def get_image_menu(name: str, allow_create: bool = False) -> dict:
    """读取镜像启动配置。

    先查架构对应的菜单文件（arm→grub.cfg / x86→boot.ipxe），再兜底查另一个；
    两个文件里都没有时**直接抛异常**（allow_create=True 时改成返回默认模板）。
    """
    path = _image_dir(name)
    arch = _read_arch(path)
    order = ("grub", "ipxe") if arch == "arm" else ("ipxe", "grub")
    reports = [_scan_menu(name, k) for k in order]

    hit = next((r for r in reports if r["range"] is not None), None)
    if hit is None:
        if not allow_create:
            raise ValueError(_menu_missing_message(name, arch, reports))
        hit = reports[0]
        return {
            "name": name,
            "arch": arch,
            "source": hit["source"],
            "header": hit["header"],
            "footer": hit["footer"],
            "content": _default_block(name, path),
            "exists": False,
            "mismatch": False,
            "expected": hit["source"],
            "checked": [{"source": r["source"], "detail": r["detail"]} for r in reports],
        }

    start, end = hit["range"]
    expected = config.GRUB_CFG if arch == "arm" else config.BOOT_IPXE
    return {
        "name": name,
        "arch": arch,
        "source": hit["source"],
        "header": hit["header"],
        "footer": hit["footer"],
        "content": "\n".join(hit["lines"][start:end]),
        "exists": True,
        # 架构对应的文件里没有、却在另一个文件里找到了 → 提示一下
        "mismatch": os.path.abspath(hit["source"]) != os.path.abspath(expected),
        "expected": expected,
        "checked": [{"source": r["source"], "detail": r["detail"]} for r in reports],
    }


def update_image_menu(name: str, content: str) -> dict:
    path = _image_dir(name)
    arch = _read_arch(path)
    if arch == "arm":
        lines, source, write = _grub_lines(), config.GRUB_CFG, _grub_write
        rng = _grub_menuentry_range(lines, name)
    else:
        lines, source, write = _boot_lines(), config.BOOT_IPXE, _boot_write
        rng = _boot_label_range(lines, name)
    if not lines:
        raise ValueError(f"菜单文件不存在或为空：{source}")

    body = content.replace("\r\n", "\n").split("\n")
    if rng is None:
        # 菜单文件里还没这一项 → 追加创建
        if arch == "arm":
            _grub_insert_menuentry(lines, [f"menuentry '{name}' {{"] + body + ["}"])
            _grub_add_autoselect(lines, name)
        else:
            lines += ["", f":{name}"] + body
            if not any(re.match(rf"^\s*item\s+(?:--key\s+\S+\s+)?{re.escape(name)}(\s|$)", l) for l in lines):
                idxs = [i for i, l in enumerate(lines) if l.strip().startswith("item")]
                pos = idxs[-1] + 1 if idxs else next(
                    (i for i, l in enumerate(lines) if l.strip().startswith("choose")), len(lines)
                )
                lines.insert(pos, f"item {name}    {name}")
        write(lines)
        log.info("为镜像 %s（%s）新建启动配置：%s", name, arch, source)
        return {"ok": True, "message": f"已在 {source} 中创建 {name} 的启动配置", "source": source, "created": True}

    start, end = rng
    lines[start:end] = body
    write(lines)
    log.info("更新镜像 %s（%s）的启动配置：%s", name, arch, source)
    return {"ok": True, "message": f"已更新 {name} 的启动配置", "source": source, "created": False}


def is_fallback_name(name: str) -> bool:
    """菜单项名是不是兜底项（Boot from next volume / Boot-from-next-volume 等写法都算）。"""
    return _norm(name) == FALLBACK_NORM or _norm(name) == _norm(config.GRUB_FALLBACK)


def list_grub_entries(include_fallback: bool = False) -> list[str]:
    """grub.cfg 里的 menuentry 名称，供 ARM 节点选择安装系统。

    兜底项（Boot from next volume）只是"没匹配到系统时的出口"，对用户不可见，默认剔除。
    """
    items = []
    lines = _grub_lines()
    for start, end, title in _iter_menuentries(lines):
        if title in items:
            continue
        if not include_fallback and (
            _is_fallback_entry(title, lines[start + 1 : end - 1]) or is_fallback_name(title)
        ):
            continue
        items.append(title)
    return items


# ------------------------------------------------------------------ boot.ipxe

def _boot_lines() -> list[str]:
    if not os.path.exists(config.BOOT_IPXE):
        return []
    with open(config.BOOT_IPXE, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read().splitlines()


def _boot_write(lines: list[str]) -> None:
    os.makedirs(os.path.dirname(config.BOOT_IPXE) or ".", exist_ok=True)
    with open(config.BOOT_IPXE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def remove_from_boot_ipxe(name: str) -> bool:
    lines = _boot_lines()
    if not lines:
        return False
    out, i, changed = [], 0, False
    while i < len(lines):
        stripped = lines[i].strip()
        # 兼容 `item name`、`item name 描述`、`item --key x name 描述` 三种写法
        if re.match(rf"^\s*item\s+(?:--key\s+\S+\s+)?{re.escape(name)}(\s|$)", stripped):
            changed = True
            i += 1
            continue
        if re.match(rf"^:{re.escape(name)}\s*$", stripped):
            changed = True
            i += 1
            while i < len(lines) and not LABEL_RE.match(lines[i].strip()):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if changed:
        _boot_write(out)
    return changed


def append_to_boot_ipxe(name: str, iso_name: str, arch: Optional[str] = None) -> bool:
    remove_from_boot_ipxe(name)  # 已存在则先替换

    lines = _boot_lines()
    item_line = f"item {name}    {name}"

    path = _image_dir(name, arch)
    arch = _read_arch(path)
    vmlinuz, initrd = kernel_names(path)

    item_idx = [i for i, l in enumerate(lines) if l.strip().startswith("item")]
    if item_idx:
        pos = item_idx[-1] + 1
    else:
        choose_idx = [i for i, l in enumerate(lines) if l.strip().startswith("choose")]
        pos = choose_idx[0] if choose_idx else len(lines)
    lines.insert(pos, item_line)

    lines += ["", f":{name}"] + (
        DEFAULT_X86_BLOCK
        .replace("{name}", name)
        .replace("{path}", _url_path(name, arch))
        .replace("{iso}", iso_name)
        .replace("{vmlinuz}", vmlinuz or "vmlinuz")
        .replace("{initrd}", initrd or "initrd")
    ).split("\n")
    _boot_write(lines)
    return True
