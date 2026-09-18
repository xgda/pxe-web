# nginx 装机源调优说明

配套文件：

| 文件 | 放到哪 | 作用 |
| --- | --- | --- |
| `pxe-web.conf` | `/etc/nginx/conf.d/pxe-web.conf` | 站点 + 传输层参数（含根目录设置） |
| `sysctl-90-pxe.conf` | `/etc/sysctl.d/90-pxe.conf` | 内核 TCP / 内存参数 |

## 一、最快上手

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
```

根目录已经在 `pxe-web.conf` 里写死成 `/opt/pxe-web/var/www/html`，与平台的
`PXE_WWW_ROOT` / `PXE_IMAGES_DIR` 默认值一致。想改只改那一行的 `root`。

### 常见报错

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `"sendfile" directive is duplicate` | Ubuntu 默认 `/etc/nginx/nginx.conf` 的 http 块里已有 `sendfile on`，而旧版配置在 conf.d 顶层又写了一遍 | 换新版 `pxe-web.conf`：性能参数已移进 `server{}`，同层不再重复 |
| `a duplicate default server for 0.0.0.0:80` | `/etc/nginx/sites-enabled/default` 还在 | `sudo rm -f /etc/nginx/sites-enabled/default` |
| `Address family not supported by protocol` | 环境没开 IPv6 | 新版配置里 IPv6 那行默认已注释，无需处理 |
| `unknown directive "aio"` | nginx 没编译 `--with-threads` | `nginx -V 2>&1 \| grep with-threads`，没有就装 nginx.org 官方包 |

## 二、nginx.conf 里还需要手工加的（main / events 层）

`conf.d` 里写不了这两块，需要改 `/etc/nginx/nginx.conf`：

```nginx
user www-data;
worker_processes auto;          # 按 CPU 核数起 worker
worker_rlimit_nofile 65535;     # 一个连接一个 fd，必须给够

events {
    use epoll;
    worker_connections 10240;   # 几十台机器绰绰有余，留大点无妨
    multi_accept on;            # 一次 accept 把所有待处理连接收完
}
```

如果用 systemd 管 nginx，也顺手加上（否则 `worker_rlimit_nofile` 可能被 1024 卡住）：

```bash
sudo systemctl edit nginx
# 写入：
# [Service]
# LimitNOFILE=65535
sudo systemctl daemon-reload && sudo systemctl restart nginx
```

确认线程支持（`aio threads` 依赖它，Ubuntu/Debian 官方包默认带）：

```bash
nginx -V 2>&1 | grep -o with-threads || echo "没有 --with-threads，aio threads 用不了"
```

## 三、磁盘与网卡

```bash
# 1) 顺序读大文件，把内核预读调大（默认 128KB 太小）
sudo blockdev --setra 8192 /dev/sdX        # 8192 × 512B = 4MB
#   持久化：/etc/udev/rules.d/60-scheduler.rules 或写进 rc.local

# 2) I/O 调度器：SSD/NVMe 用 none 或 mq-deadline，机械盘用 deadline
cat /sys/block/sdX/queue/scheduler
echo deadline | sudo tee /sys/block/sdX/queue/scheduler

# 3) 挂载去掉 atime，省掉每次读的元数据写
#    /etc/fstab:  defaults,noatime,nodiratime 0 2

# 4) 万兆网卡：加大 ring buffer、确认 offload 打开
sudo ethtool -G eth0 rx 4096 tx 4096
sudo ethtool -k eth0 | grep -E 'tcp-segmentation|generic-segmentation|generic-receive'  # 都该是 on
sudo ip link set eth0 txqueuelen 10000
```

## 四、权限（很容易踩）

平台用 root 跑（systemd `User=root`），上传出来的 ISO 权限可能是 `600`，
nginx（www-data）读不了会直接 403：

```bash
sudo chmod -R a+rX /opt/pxe-web/var/www/html
# 或者让上传的文件默认 644：在 pxe-web.service 里加
#   Environment="PXE_FILE_MODE=0644"    （若版本支持）
# 最省事：umask 0022
```

## 五、验证

```bash
# 单流能不能跑满
time wget -O /dev/null http://<server>/x86/<镜像名>/<ISO>

# 看是否真的走零拷贝 —— 灌满带宽时 nginx worker 的 CPU 应该很低
top -H -p $(pgrep -d, -f 'nginx: worker')

# 页缓存命中情况（第二台之后应该几乎全命中）
vmtouch /opt/pxe-web/var/www/html/x86/<镜像名>/<ISO>   # apt install vmtouch

# 实时带宽
nload eth0    # 或 iftop、sar -n DEV 1
```

**预期**：第一台拉 ISO 时受限于磁盘速度，第二台开始应该能打到网卡上限，
nginx worker 的 CPU 占用很低（这就是 sendfile 生效的标志）。

## 六、几个反直觉的坑

1. **不要开 `directio`**。它会绕过页缓存让每次读都落到磁盘。
   装机场景几十台拉的是**同一个** ISO，页缓存正是最大的加速器 ——
   第一台读盘，后面几十台全从内存发。开 directio 等于把这个优势扔掉。
   （网上不少"nginx 大文件优化"文章会推荐 directio，那是单用户下载站的场景。）

2. **不要对 ISO 开 gzip**。ISO 本身是压缩格式，压了也压不动，白白烧 CPU。

3. **Apache 用户注意**：`EnableSendfile` 自 2.3.9 起默认是 **Off**，必须显式打开，
   否则吞吐差一个数量级。

4. **`vm.swappiness` 调低**。ISO 的页缓存一旦被换出，后面每台机器都得重新读盘。

5. **symlink 跨设备**：`root` 指向的路径如果某级是符号链接且跨文件系统，
   nginx 仍能工作（sendfile 是文件→socket，不要求同设备），
   但 FUSE/NFS 上的 sendfile 行为异常，装机源务必放本地盘。

6. **平台侧配合**：镜像管理页的「预热」按钮就是把 ISO 提前读进页缓存
   （`posix_fadvise(WILLNEED)`），批量装机前点一下，第一台也能很快。
   调度上用「台并发 / 批间隔」错峰，别让几十台同一秒一起打。

## 七、还想再快

| 手段 | 说明 |
| --- | --- |
| ISO 放 NVMe / tmpfs | 内存够（ISO < 可用内存的 60%）时，直接放 `/dev/shm` 或 tmpfs，彻底消除磁盘 I/O |
| 组播下发 | `udpcast` / `flamethrower`，一份数据发给所有机器，上百台最合适 |
| P2P | 先装一台做种子，其余 BT 互传，机器数 > 50 且带宽紧张时有效 |
| 加带宽 | 双口 bond / 上万兆，最直接 |
| 本地软件源 | 装完还要拉包的话，配 `apt-cacher-ng` 私服，别让每台都去公网 |
