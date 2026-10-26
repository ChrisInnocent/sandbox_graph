# QEMU 沙箱测试台（aarch64 / TCG + 嵌套 KVM）

在**没有宿主 `/dev/kvm`** 的开发机上，用 harmonybrew QEMU 起一台 aarch64 Linux 虚机，
在**客体内**用嵌套 KVM 跑微虚机沙箱，用来验证图工程/循环工程与三产品选型用例。

## 0. 现状（实测，2026-10-24）

| 能力 | 状态 | 证据 |
|---|---|---|
| harmonybrew QEMU 11.1.2 aarch64（TCG）启动 Linux 客体 | ✅ 已验证 | 见 `tmp/qemu/*-serial.log` |
| 客体 `/dev/kvm` + **嵌套 KVM 真执行 L2 指令** | ✅ 已验证 | 探针输出 `PASS KVM_API_VERSION=12 / CREATE_VM / VCPU_INIT / KVM_RUN: L2 executed store(42)` |
| 换用**有 ublk 的内核**（Ubuntu 7.0.0-39） | ✅ 已验证 | `insmod ublk_drv.ko` → `/dev/ublk-control` |
| **AgentENV aarch64** 发布包下载并解包 | ✅ 已验证 | `server`、`ublk/uvm-ublk-daemon`、`deps/{firecracker 1.15.1-patch-v1, vmlinux-6.1.175, overlaybd, regctl}` |
| AgentENV `--setup-only/--setup-host` + 起 server + 建沙箱 | ⏳ 阻塞 | 需 `curl ip iptables jq umoci zstd`；宿主网络 DNS 中断，无法补齐 |

> 关键事实：**Alpine 全系内核都没开 `CONFIG_BLK_DEV_UBLK`**；AgentENV 的 server 是 **glibc C++** 二进制，**musl 的 Alpine 跑不了**（`libstdc++.so.6`/`__res_init` 缺失）。所以要 **glibc 客体 + 自带 ublk 的内核**。

## 1. 前提

- `qemu-system-aarch64`（harmonybrew，11.1.2）
- QEMU 自带固件目录（含 `edk2-aarch64-code.fd`、`edk2-arm-vars.fd`）
- 宿主有网（拉 Alpine/Ubuntu 内核 + AgentENV 发布包）
- 宿主**无需** root（不需要 mkfs/loop/UEFI 磁盘：本方案用 `-kernel`/`-initrd` 直接引导）

## 2. 一键脚本

```bash
SANDBOX_QEMU_CACHE=/path/to/cache scripts/qemu/vm.sh fetch   # 拉取内核/rootfs/工具
SANDBOX_QEMU_CACHE=/path/to/cache scripts/qemu/vm.sh build   # 造客体 initramfs
scripts/qemu/vm.sh up        # 起虚机（前台/后台，串口落 serial.log）
scripts/qemu/vm.sh console   # 跟串口
scripts/qemu/vm.sh status    # 看进程与日志尾
scripts/qemu/vm.sh ps        # 看 qemu 进程与占用端口
scripts/qemu/vm.sh down      # 停虚机（保留产物）
scripts/qemu/vm.sh clean     # 停虚机 + 回收缓存/产物（RM_ALL=1 clean 连 tarball 一起删）
```

`vm.sh` 默认复用 `tmp/qemu/` 作为缓存与产物目录（已被 `.gitignore` 忽略）。

### 2.1 创建方法（Create）

1. **拉取资产**（`fetch`）：Alpine netboot（备用）+ **Ubuntu 内核/模块**（带 `ublk`）+
   `ubuntu-base`（glibc 客体）+ Alpine `busybox-static`（客体静态 init）。
2. **造客体**（`build`）：把 `ubuntu-base` 解成 rootfs，放入静态 busybox、`ublk_drv.ko`，
   写客体 `init`（挂载 /proc /sys /dev、校时、`insmod ublk`、静态网络 10.0.2.15/24），打包成 `ub-guest.cpio.gz`。
3. **起虚机**（`up`）：`qemu-system-aarch64 -machine virt,virtualization=on -cpu max -accel tcg`
   + Ubuntu 内核 + 上一步 initramfs；串口写 `serial.log`。
4. **确认就绪**：串口出现 `kvm: /dev/kvm`、`ublk: /dev/ublk-control`、`ip: 10.0.2.15`。
5. （AgentENV，按官方指导）客体里：铺 `server`/`deps`/`ublk-daemon`，`groupadd kvm`，
   `server --setup-only` / `--setup-host`，再起 server 并用 `aenv start --cold ...` 建沙箱。

> 需要**正常发行版客体**（systemd + 磁盘根）时，用宿主 root 造一块 Ubuntu ARM64 ext4 盘（debootstrap/mkfs），
> 走 UEFI 引导；`vm.sh` 的 `-kernel/-initrd` 路径不需要 root。

### 2.2 回收方法（Reclaim/Cleanup）

按“从内到外”依次回收，保证不残留沙箱与进程：

1. **客体侧**（若已起 server）：
   ```sh
   aenv ls                       # 列出现存沙箱
   aenv rm <sandbox-id>          # 或 aenv stop；逐个删，确认 aenv ls 为空
   pkill -f /usr/local/bin/server   # 停 server
   ublk 设备/netns 由 server 退出时回收；异常时 `ip netns ls` 逐个 `ip netns del`
   ```
2. **虚机侧**：
   ```bash
   scripts/qemu/vm.sh down       # 停 qemu（保留产物，便于复跑）
   scripts/qemu/vm.sh ps         # 确认无 qemu 残留、SSH 端口已释放
   ```
3. **产物侧**：
   ```bash
   scripts/qemu/vm.sh clean      # 删 initramfs/rootfs/overlay/日志（保留下载的 tarball/deb）
   RM_ALL=1 scripts/qemu/vm.sh clean   # 连 tarball/apk/deb 一起删
   ```
4. **确认干净**：`pgrep -af qemu-system-aarch64` 为空；`tmp/qemu/` 下无 `*.cpio.gz`/`*.img`/`serial.log`；
   宿主无残留 `hostfwd` 端口占用（默认 `2223`）。

> 红线：回收只删**本次虚拟环境与产物**，不触碰仓库代码、不删用户已有证据；
> 真实三产品数据只在其各自部署环境回收。

## 3. 手动步骤（脚本等价）

### 3.1 起一台能跑嵌套 KVM 的 aarch64 客体

```sh
qemu-system-aarch64 -machine virt,virtualization=on,gic-version=3 \
  -cpu max -accel tcg -m 1024 -smp 2 -display none -monitor none \
  -serial file:guest-serial.log -nic user,model=virtio-net-pci \
  -kernel <kernel> -initrd <guest-initramfs.cpio.gz> \
  -append 'console=ttyAMA0 rdinit=/init'
```

客体 init 里已验证：

```sh
insmod /ublk_drv.ko          # 需要带 ublk 的内核（见 3.2）
ls -l /dev/kvm /dev/ublk-control
```

嵌套 KVM 探针（预编译 static aarch64）：`kvm-smoke`，输出见 0 节。

### 3.2 换内核（拿 ublk）

Alpine 全系 `# CONFIG_BLK_DEV_UBLK is not set`。取 **Ubuntu arm64 内核 + 模块**：

```sh
# linux-image-unsigned-<ver>-generic_*_arm64.deb + linux-modules-<ver>-generic_*_arm64.deb
# 用 python 解 ar，取 data.tar（Ubuntu 为未压缩 tar）
#   boot/vmlinuz-<ver>                        -> 客体内核（EFI zboot，QEMU -kernel 直接吃）
#   usr/lib/modules/<ver>/kernel/drivers/block/ublk_drv.ko.zst
zstd -d ublk_drv.ko.zst -o ublk_drv.ko        # 客体 init 里 insmod
```

配置确认：`CONFIG_BLK_DEV_UBLK=m`、`CONFIG_MODULE_DECOMPRESS=y`。

### 3.3 glibc 客体（AgentENV 需要）

用 `ubuntu-base-24.04.*-base-arm64.tar.gz` 当 rootfs（**glibc**），放一个**静态 busybox** 负责挂载/网络/insmod：

```sh
tar --no-same-owner --no-same-permissions -xzf ubuntu-base.tar.gz -C ubroot
cp bin/busybox.static ubroot/bin/            # Alpine busybox-static，aarch64 static
```

客体 init（要点，已验证）：

```sh
BB=/bin/busybox.static
$BB mount -t proc none /proc; $BB mount -t sysfs none /sys; $BB mount -t devtmpfs none /dev
$BB date -u -s "@$($BB cat /.host_epoch)"     # 关键：否则 TLS 证书 "not yet valid"
$BB insmod /ublk_drv.ko
$BB ip link set eth0 up
$BB ip addr add 10.0.2.15/24 dev eth0         # QEMU user-net 固定地址
$BB ip route add default via 10.0.2.2
printf 'nameserver 8.8.8.8\n' > /etc/resolv.conf
```

### 3.4 装 AgentENV（按官方指导）

官方指导：`curl -fsSL .../install.sh | sudo AENV_HOME_PATH=/path bash`。
客体里**没有 systemd 也可**（install.sh 会打印手动 `setpriv` 启动命令）。

```sh
# glibc 客体里 bash install.sh（需 curl jq coreutils tar gzip shadow util-linux）
AENV_HOME_PATH=/var/lib/aenv bash install.sh
# 或离线：解开 aenv-server-linux-aarch64.tar.gz，按 install.sh 的路径铺：
#   server -> /usr/local/bin/server
#   ublk/uvm-ublk-daemon -> /var/lib/aenv/ublk/uvm-ublk-daemon
#   deps/. -> /var/lib/aenv/deps/
#   default.toml -> /var/lib/aenv/config/config.toml
groupadd --system kvm        # setup-host 要求 kvm 组存在
AENV_CONFIG_PATH=/var/lib/aenv/config/config.toml AENV_HOME_PATH=/var/lib/aenv \
  AENV_VIRTUALIZATION_MODE=kvm server --setup-only
AENV_CONFIG_PATH=/var/lib/aenv/config/config.toml AENV_HOME_PATH=/var/lib/aenv \
  AENV_VIRTUALIZATION_MODE=kvm server --setup-host --runtime-user aenv --runtime-group aenv
```

`--setup-only` 目前会要求这些命令齐备：**`curl ip iptables-restore iptables-save jq umoci zstd`**。

### 3.5 起 server + 建第一个沙箱

```sh
setpriv --reuid=aenv --regid=aenv --init-groups \
  --inh-caps=+net_admin,+sys_admin --ambient-caps=+net_admin,+sys_admin \
  --bounding-set=-all,+net_admin,+sys_admin --nnp \
  env AENV_CONFIG_PATH=/var/lib/aenv/config/config.toml AENV_HOME_PATH=/var/lib/aenv \
      AENV_RUNTIME_PATH=/run/aenv AENV_VIRTUALIZATION_MODE=kvm API_ADDR=127.0.0.1:8000 \
      /usr/local/bin/server &

curl -sS http://127.0.0.1:8000/health
aenv auth                       # 写入 server URL + API key（/var/lib/aenv/secrets/api-key）
aenv start --cold ubuntu:24.04 --detach    # 建第一个沙箱（拉 OCI 镜像）
aenv exec <sandbox-id> -- uname -a
```

## 4. 三产品对照（在 aarch64 客体里的可行性）

| 产品 | 指导 | aarch64 客体可行性 |
|---|---|---|
| **AgentENV** | `docs/src/deployment/*`、`scripts/install.sh` | ✅ 支持 aarch64；需 glibc + ublk + /dev/kvm（本方案已备） |
| **CubeSandbox** | `deploy/one-click/USER_GUIDE_zh.md` | ❌ one-click 偏 x86（cube-agent 走 `x86_64-unknown-linux-musl`、s3lvol 要 AVX2） |
| **Kuasar** | `orchestrator/docs/node-build_zh.md` | ⚠️ 需从源码构建 Go+Rust 全套 |

## 5. 实测结论与当前阻塞

### 5.1 “AgentENV 需要 root” 是误解（已证伪）

真实根因链（2026-10-24 实测）：
1. 本机 **HarmonyOS HAP 文件系统**把新建文件**强制归属 `20001006:1006`**，且**宿主上 `chmod` 基本不生效**
   （把 `770` 改 `a+rX` 反而变 `660`）；
2. 解包若用 `--no-same-permissions`，镜像里文件变成 **`rwxrwx---`（仅 group 可读/执行）**；
3. 客体里 `aenv` 用户不在该组 → 读不到 loader/libc、执行不了 helper → `execve` **EACCES**
   （现象：`failed to spawn ublk daemon ... Permission denied`）。

**在客体里（root + ext4，chmod 正常）执行 `chmod -R a+rX` 后，EACCES 消失** →
AgentENV **不要求 `root:root` 属主，只要求文件“世界可读/可执行”**；这一步**无需宿主 root**。

> 因此：普通用户**可以**建镜像、甚至建分区表并引导（`truncate` + `sfdisk` + `mke2fs -d`）；
> 宿主做不到的只有 `root:root` 属主、`mknod`、挂载——而这三者**不是** AgentENV 的硬需求。

### 5.2 剩余阻塞（性质不同：AgentENV 需要“能拥有的宿主网络”）

`RUST_LOG=debug/trace` 定位（2026-10-24）：补上 `nftables`（命令 `nft`）与 191 个 netfilter 模块、并在客体里修好权限后，server **走得很深**：
```
setup complete
→ firecracker pool primed warm=2          # 真的预热了 2 个 microVM 进程
→ loaded paused records → access-token seed → image cache maintenance pass complete
→ Error: Cannot assign requested address (os error 99)   # 一次 bind() 失败
→ panic (TLS destructor) → abort → API 未监听
```
trace 显示它**网络已建好**：`veth-1/2/3` + `10.12.0.x/31`（宿侧）+ 到 `10.11.0.x` 的路由 + `agentenv-ns-*` 命名空间。

**根因**：AgentENV 假设自己能**“拥有”宿主网络**——它用 `host_interaction_cidr=10.11.0.0/16`、`veth_cidr=10.12.0.0/16`、
**固定 VM tap link `169.254.0.20/30`**，并在启动收尾时 bind 这些地址。我们的客体只有 **QEMU slirp 的单张 `eth0`**，
没有它要 bind 的 host-side 地址/tap → `EADDRNOTAVAIL`。

→ 所以：**不是“必须 systemd”，而是“必须让 AgentENV 能拥有它期望的宿主网络”**（带桥/tap 与可分配地址段）。
在无 root 的本机上，最省事的就是用**最小 Ubuntu root**（完整网络栈）跑。

### 5.3 两条继续路线

| | 非特权沙箱（Firecracker microVM） | 完整镜像（Ubuntu + systemd） |
|---|---|---|
| 是什么 | 客体里用 `/dev/kvm` 直接起 microVM | 正常发行版根，让 **AgentENV** 按官方方式跑 |
| 需要 | 只要 `/dev/kvm` + 内核 + rootfs | systemd + 完整 `/proc /sys /cgroup` + netns/iptables + OCI |
| 是否待测产品 | 否（底层沙箱原语，Cube/AgentENV 的底座） | 是（真 AgentENV，可测图工程的适配器） |
| 现状 | ✅ 已实测能起（`Successfully started microvm`） | ⏳ 装到 server，卡在 5.2 |
| 体积 | 极小（内核+initramfs，几十 MB） | 不大：ubuntu-base 28MB→展开~105MB；+systemd/工具 ≈300–600MB；+AgentENV 77MB ≈ 合计 **0.5–1GB**（12G 稀疏盘实占 1–2GB） |

### 5.4 其它已排除的小问题

- 客体 QEMU slirp 的 **glibc 解析用 `10.0.2.3` 不稳**；改用 `8.8.8.8` 后 `getent`/curl 正常。
- `aenv` 的凭据是 **TOML**（不是 JSON）：`~/.config/aenv/credentials`。
- iptables-nft 需 `nft_compat/nft_ct/...`；直接 `modprobe` 整个 `net/netfilter` 树最省事。

## 6. 产物位置（当前会话）

`tmp/qemu/`（gitignored）：
- 内核：`uimg/boot/vmlinuz-7.0.0-39-generic`（带 ublk）、`vmlinuz-virt`（Alpine 备用）
- 模块：`umod/usr/lib/modules/7.0.0-39-generic/`、`ublk_drv.ko`
- 客体根：`ubuntu-base.tar.gz`、`ubroot/`（glibc + 工具 + AgentENV）、`bin/busybox.static`
- 系统盘：`disk.img`（`mke2fs -d ubroot` 生成，`-kernel ... root=/dev/vda` 引导）
- AgentENV：`aenv-*-aarch64.tar.gz`（v0.2.4）+ 30+ 个 Ubuntu deb（`debs/`、`debs-x/`）
- 启动日志：`*-serial.log`
