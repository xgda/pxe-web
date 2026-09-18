# PXE Web Console

给 Linux PXE 装机服务器用的轻量管理面板：**登录 → 节点信息（表格）→ 网页 SSH 终端 → 写回 dhcpd 配置**。

技术栈：后端 FastAPI + paramiko，前端 Vue3 + Element Plus（CDN，无需 npm 打包）。



## 1. 放到服务器上

```bash
sudo mkdir -p /opt/pxe-web
sudo cp -r pxe-web/* /opt/pxe-web/
cd /opt/pxe-web


前提依赖设置
sudo apt install -y nginx wget isc-dhcp-server tftpd-hpa

1.1、修改tftp存储文件夹，指向/opt/pxe-web/tftpboot/
# cat /etc/default/tftpd-hpa

TFTP_USERNAME="tftp"
TFTP_DIRECTORY="/opt/pxe-web/tftpboot/"
TFTP_ADDRESS=":69"
TFTP_OPTIONS="--secure --verbose"


sudo systemctl enable tftpd-hpa.service
sudo systemctl status tftpd-hpa.service

1.2、修改nginx根文件夹，指向/opt/pxe-web/var/www/html
```bash
# 1) 先干掉 Ubuntu 自带的默认站点 —— 它也占了 80 端口的 default_server，
#    不删的话下一步会报 "a duplicate default server for 0.0.0.0:80"
sudo rm -f /etc/nginx/sites-enabled/default

# 2) 拷配置
sudo cp deploy/nginx/pxe-web.conf       /etc/nginx/conf.d/
sudo cp deploy/nginx/sysctl-90-pxe.conf /etc/sysctl.d/90-pxe.conf
sudo sysctl --system

# 3) 检查并生效
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx.service
sudo systemctl status nginx.service
```

根目录已经在 `pxe-web.conf` 里写死成 `/opt/pxe-web/var/www/html`，与平台的
`PXE_WWW_ROOT` / `PXE_IMAGES_DIR` 默认值一致。想改只改那一行的 `root`。

1.3、创建hosts.conf文件
```bash
# sudo mkdir /etc/dhcp/hosts 
# sudo touch /etc/dhcp/hosts.conf
```

1.4、修改isc-dhcp-server配置文件 
```bash
# mv /etc/dhcp/dhcpd.conf /etc/dhcp/dhcpd.conf.back
# vim /etc/dhcp/dhcpd.conf，添加以下内容，地址等信息根据需求修改
option domain-name "example.org";
option domain-name-servers ns1.example.org, ns2.example.org;

default-lease-time 600;
max-lease-time 7200;

# The ddns-updates-style parameter controls whether or not the server will
# attempt to do a DNS update when a lease is confirmed. We default to the
# behavior of the version 2 packages ('none', since DHCP v2 didn't
# have support for DDNS.)
ddns-update-style none;

option arch code 93 = unsigned integer 16;
# ========== iPXE 自定义选项声明 ==========
option ipxe-menu-item code 224  = text;
#option grub-menu-item code 225  = text;

# ========= 引入外部映射文件 =========
include "/etc/dhcp/hosts.conf";

subnet 192.168.1.0 netmask 255.255.255.0 {
    authoritative;

    default-lease-time 1800;
    max-lease-time 3600;

    range 192.168.1.101 192.168.1.130;
    option routers 192.168.1.254;
    option domain-name-servers 8.8.8.8;

    next-server 192.168.1.110;
    append dhcp-parameter-request-list 224;

    # ========= PXE 配置 =========
    class "pxeclients" {
        match if substring (option vendor-class-identifier, 0, 9) = "PXEClient";

        if exists user-class and option user-class = "iPXE" {
            filename "boot.ipxe";
        } elsif option arch = 00:00 {
            filename "undionly.kpxe";
        } elsif option arch = 00:06 {
            filename "ipxe.efi";
        } elsif option arch = 00:07 {
            filename "ipxe.efi";
        } elsif option arch = 00:0b {
            filename "grub2.efi";
        } else {
            filename "undionly.kpxe";
        }
    }
}

sudo systemctl restart isc-dhcp-server
sudo systemctl enable isc-dhcp-server
sudo systemctl status isc-dhcp-server

```

## 2. 安装依赖并试运行

```bash
cd /opt/pxe-web
python3 -m venv /opt/pxe-web/venv
/opt/pxe-web/venv/bin/pip install -r /opt/pxe-web/requirements.txt
/opt/pxe-web/venv/bin/pip install uvicorn[standard]

# 以 root 运行（需要读 /etc/dhcp、执行 dhcpd -t 与 systemctl reload）
sudo /opt/pxe-web/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

浏览器打开 `http://<服务器IP>:8080`，用 `admin / admin` 登录。

## 3. 配置成系统服务

```bash
sudo cp /opt/pxe-web/deploy/pxe-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pxe-web
sudo systemctl restart pxe-web
sudo systemctl status pxe-web
```
***
在web里面根据自己的系统情况，在系统设置里面修改对应dhcp设置
***
---

## 环境变量（全部可选，写在 service 的 Environment 里）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PXE_ADMIN_USER` / `PXE_ADMIN_PASS` | `admin` / `admin` | **上线必须改密码** |
| `PXE_SECRET_KEY` | 内置默认值 | 令牌签名密钥，建议改 |
| `PXE_HOSTS_DIR` | `/etc/dhcp/hosts` | 节点映射文件目录（一个节点一个 .conf） |
| `PXE_HOSTS_GLOB` | `/etc/dhcp/hosts/*.conf` | 读取规则 |
| `PXE_DHCPD_CONF` | `/etc/dhcp/dhcpd.conf` | 校验用的主配置 |
| `PXE_BOOT_IPXE` | `/var/lib/tftpboot/boot.ipxe` | 自动解析里面的 `item` 作为「节点系统」下拉项 |
| `PXE_IMAGES_DIR` | `/var/www/html` | 镜像根目录（HTTP 安装源根） |
| `PXE_AUTOINSTALL_DIR` | `autoinstall` | 镜像目录下存放 meta-data / user-data 的子目录名 |
| `PXE_SSH_USER` / `PXE_SSH_PORT` | `root` / `22` | 网页终端默认账号端口 |
| `PXE_DRY_RUN` | `0` | 设为 `1` 时只写文件、不校验不重载（调试用） |
| `PXE_VALIDATE_CMD` | `dhcpd -t -cf <conf>` | 自定义校验命令 |
| `PXE_RELOAD_CMD` | `systemctl reload isc-dhcp-server` | 自定义重载命令 |
| `PXE_BACKUP_DIR` | `/var/backups/pxe-web` | 每次写回前自动备份 |
| `PXE_LOG_FILE` | `/var/log/pxe-web/app.log` | 应用日志文件（滚动：5MB × 3） |
| `PXE_LOG_LEVEL` | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR） |
| `PXE_LOG_MAX_BYTES` | `5242880` | 单文件滚动阈值 |
| `PXE_LOG_BACKUPS` | `3` | 保留的历史文件数 |

## 查看日志

三种方式，任选：

**① systemd 模式（推荐）**

```bash
journalctl -u pxe-web -f              # 实时跟踪
journalctl -u pxe-web -n 200 --no-pager
journalctl -u pxe-web --since today
systemctl status pxe-web -n 50
```

**② 日志文件**

```bash
tail -f /var/log/pxe-web/app.log
grep -i '镜像' /var/log/pxe-web/app.log
```

**③ 网页端**：左侧「运行日志」，默认显示最近 300 行，可开 5 秒自动刷新。

日志内容包含：每条请求（方法/路径/状态码/耗时/来源 IP）、登录成功与失败、
节点保存与删除、镜像上传开始与每个文件的落盘大小、镜像删除、SSH 连接成功与失败。

## 常见问题

**保存成功但提示“重载失败”**

Debian/Ubuntu 的 `isc-dhcp-server.service` 通常没有定义 `ExecReload`，systemd 会直接报
`Job type reload is not applicable for unit isc-dhcp-server.service`。
配置其实**已经写进文件了**，只是最后一步通知服务失败。解决办法是把重载命令改成 restart：

```conf
Environment="PXE_RELOAD_CMD=systemctl restart isc-dhcp-server"
```

程序自带回退链：主命令失败后会依次尝试
`systemctl restart isc-dhcp-server` → `service isc-dhcp-server restart` → `systemctl restart dhcpd`
→ `service dhcpd restart`，任一成功即视为成功，界面上会弹出完整尝试记录。
`restart` 会带来 1~2 秒 DHCP 中断；若完全不能中断，需改用 OMAPI（omshell）动态下发。

## 注意事项

- 服务需要 **root** 权限（读 /etc/dhcp、reload/restart dhcpd）。
- Ubuntu 若提示 `Permission denied` 读不到 `/etc/dhcp/hosts/*`，在
  `/etc/apparmor.d/usr.sbin.dhcpd` 加一行 `/etc/dhcp/hosts/** r,` 再 `systemctl reload apparmor`。
- 网页 SSH 走 WebSocket，反向代理需要放行 `Upgrade` 头。
- 默认口令 admin/admin 仅用于内网调试，生产环境务必通过 `PXE_ADMIN_PASS` 修改。
