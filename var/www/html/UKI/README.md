# UKI 内置系统类型目录

每个子目录 = 一种「现成系统类型」，里面放该系统从 ISO 里抽出来的引导文件：

    var/www/html/UKI/
      x86/
        ubuntu-24.04.5-server/vmlinuz   initrd
        dgxos-7.5.0/vmlinuz             initrd
      arm/
        ubuntu-24.04.4-server/vmlinuz   initrd
        ubuntu-24.04.5-server/vmlinuz   initrd
        dgxos-7.5.0/vmlinuz             initrd

## 使用方式

1. 把对应系统的 `vmlinuz` / `initrd`（文件名随意，平台不强制改名）
   放进上面的目录里；
2. 网页「镜像管理 → 添加镜像」：
   - 「系统类型」选对应的目录名（例如 ubuntu-24.04.5-server）；
   - 只需要上传 ISO（meta-data / user-data 可选）；
   - ISO 与可选的 autoinstall 文件会存进本目录；
3. 菜单（boot.ipxe / grub.cfg）自动生成，内核指向
   `http://<server>/UKI/<arch>/<系统类型>/vmlinuz`。

## 与「新系统类型」的区别

- UKI 已有类型：内核 / initrd 用这里内置的，只传 ISO；
  URL 路径 = `UKI/<arch>/<类型>/`。
- 新系统类型：自己起镜像名，vmlinuz / initrd / ISO 全要上传，
  目录在 `var/www/html/<arch>/<镜像名>/`；URL 路径 = `<arch>/<镜像名>/`。
