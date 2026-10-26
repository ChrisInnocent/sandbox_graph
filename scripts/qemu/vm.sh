#!/usr/bin/env bash
# 一键创建并配置 QEMU aarch64 沙箱测试虚机（TCG + 嵌套 KVM + ublk）。
#
# 用途：在没有宿主 /dev/kvm 的开发机上，起一台 aarch64 Linux 客体，在客体内用
#       嵌套 KVM 跑微虚机沙箱，用于验证图工程/循环工程与三产品选型用例。
# 详见：docs/qemu-sandbox-harness.md
#
# 用法：
#   scripts/qemu/vm.sh fetch     # 拉取内核/rootfs/工具到缓存
#   scripts/qemu/vm.sh build     # 造客体 initramfs（glibc 客体 + ublk）
#   scripts/qemu/vm.sh up        # 后台起虚机（串口落日志）
#   scripts/qemu/vm.sh console   # 跟串口
#   scripts/qemu/vm.sh status    # 看进程/日志
#   scripts/qemu/vm.sh down      # 停虚机（保留产物）
#   scripts/qemu/vm.sh clean     # 停虚机 + 回收缓存/产物（RM_ALL=1 全删）
#   scripts/qemu/vm.sh ps        # 看虚机进程与占用端口
#
# 环境变量：SANDBOX_QEMU_CACHE / QEMU_BIN / QEMU_SHARE / VM_MEM / VM_SMP / VM_SSH_PORT
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CACHE="${SANDBOX_QEMU_CACHE:-$ROOT/tmp/qemu}"
QEMU="${QEMU_BIN:-qemu-system-aarch64}"
MEM="${VM_MEM:-1024}"
SMP="${VM_SMP:-2}"
SSH_PORT="${VM_SSH_PORT:-2223}"

ALPINE_VER="${ALPINE_VER:-3.22}"
ALPINE_BASE="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER}/releases/aarch64"
ALPINE_MAIN="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER}/main/aarch64"
UBUNTU_KVER="${UBUNTU_KVER:-7.0.0-39-generic}"
UBUNTU_KREL="${UBUNTU_KREL:-7.0.0-39.39}"
UBUNTU_POOL="http://ports.ubuntu.com/ubuntu-ports/pool/main/l/linux"
UBUNTU_BASE="${UBUNTU_BASE:-https://cdimage.ubuntu.com/ubuntu-base/releases/noble/release/ubuntu-base-24.04.5-base-arm64.tar.gz}"

log(){ printf '[vm.sh] %s\n' "$*" >&2; }
die(){ printf '[vm.sh] ERROR: %s\n' "$*" >&2; exit 1; }

qemu_share(){
  [ -n "${QEMU_SHARE:-}" ] && { echo "$QEMU_SHARE"; return; }
  local d; d="$(command -v "$QEMU" || true)"; [ -n "$d" ] || die "找不到 $QEMU"
  d="$(cd "$(dirname "$d")/.." && pwd)"
  local f; f="$(ls -d "$d"/Cellar/qemu/*/share/qemu 2>/dev/null | tail -1 || true)"
  [ -n "$f" ] && { echo "$f"; return; }
  f="$(ls -d /usr/local/share/qemu /usr/share/qemu 2>/dev/null | head -1 || true)"
  [ -n "$f" ] || die "找不到 QEMU share 目录，请设 QEMU_SHARE"
  echo "$f"
}

get(){ # get <url> <out>
  local url="$1" out="$2"
  [ -s "$out" ] && { log "已存在 $(basename "$out")"; return; }
  log "下载 $(basename "$out")"
  curl -fsSL --retry 5 --retry-delay 5 -m 900 -o "$out.part" "$url" || die "下载失败: $url"
  mv "$out.part" "$out"
}

# 从 .deb 提取（ar -> data.tar -> tar）
deb_extract(){ # deb_extract <deb> <outdir>
  local deb="$1" out="$2"
  rm -rf "$out"; mkdir -p "$out"
  ( cd "$out"
    python3 - "$deb" <<'PY'
import sys
data=open(sys.argv[1],'rb').read()
assert data[:8]==b'!<arch>\n', 'not an ar archive'
i=8
while i < len(data):
    hdr=data[i:i+60]; i+=60
    name=hdr[:16].decode().strip(); size=int(hdr[48:58].decode().strip())
    body=data[i:i+size]; i+=size+(size%2)
    if name.startswith('data.tar'):
        open('data.tar','wb').write(body); break
PY
    tar -xf data.tar
  )
}

cmd_fetch(){
  mkdir -p "$CACHE"; cd "$CACHE"
  get "$ALPINE_BASE/netboot/vmlinuz-virt"  vmlinuz-virt
  get "$ALPINE_BASE/netboot/initramfs-virt" initramfs-virt
  get "$ALPINE_BASE/alpine-minirootfs-${ALPINE_VER}.0-aarch64.tar.gz" alpine-minirootfs.tar.gz
  get "$UBUNTU_BASE" ubuntu-base.tar.gz
  # 带 ublk 的 Ubuntu 内核 + 模块
  get "$UBUNTU_POOL/linux-image-unsigned-${UBUNTU_KVER}_${UBUNTU_KREL}_arm64.deb" uimg.deb
  get "$UBUNTU_POOL/linux-modules-${UBUNTU_KVER}_${UBUNTU_KREL}_arm64.deb" umod.deb
  deb_extract uimg.deb uimg
  deb_extract umod.deb umod
  KSRC="uimg/boot/vmlinuz-${UBUNTU_KVER}"; [ -s "$KSRC" ] || die "内核未取到: $KSRC"
  USRC="umod/usr/lib/modules/${UBUNTU_KVER}/kernel/drivers/block/ublk_drv.ko.zst"
  [ -s "$USRC" ] || die "ublk 模块未取到: $USRC"
  command -v zstd >/dev/null || die "需要 zstd 解 ublk 模块"
  zstd -d -q -f "$USRC" -o ublk_drv.ko
  # 静态 busybox（用于 glibc 客体挂载/网络）
  local bv
  bv="$(curl -fsSL -m 60 "$ALPINE_MAIN/APKINDEX.tar.gz" | tar -xzO APKINDEX | awk -v RS='' '/^P:busybox-static$/{for(i=1;i<=NF;i++) if($i ~ /^V:/){sub(/^V:/,"",$i); print $i}}')"
  [ -n "$bv" ] || die "查不到 busybox-static 版本"
  get "$ALPINE_MAIN/busybox-static-${bv}.apk" busybox-static.apk
  tar -xzf busybox-static.apk bin/busybox.static
  log "fetch 完成：$CACHE"
}

cmd_build(){
  cd "$CACHE"
  [ -s uimg/boot/vmlinuz-${UBUNTU_KVER} ] || die "请先 fetch"
  [ -s ublk_drv.ko ] || die "缺 ublk_drv.ko（fetch 失败？）"
  rm -rf ubroot; mkdir -p ubroot
  tar --no-same-owner --no-same-permissions -xzf ubuntu-base.tar.gz -C ubroot 2>/dev/null || true
  mkdir -p ubroot/bin ubroot/usr/local/bin
  cp bin/busybox.static ubroot/bin/busybox.static
  cp ublk_drv.ko ubroot/ublk_drv.ko
  date +%s > ubroot/.host_epoch
  # minimal sudo（处理 VAR=value 前缀）
  cat > ubroot/usr/local/bin/sudo <<'EOF'
#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in [A-Za-z_]*=*) export "$1"; shift ;; *) break ;; esac
done
exec "$@"
EOF
  chmod +x ubroot/usr/local/bin/sudo ubroot/bin/busybox.static
  # 客体 init：挂载 + 时钟 + ublk + 静态网络（QEMU user-net 固定地址）
  cat > ubroot/init <<'EOF'
#!/bin/busybox.static sh
BB=/bin/busybox.static
$BB mount -t proc none /proc; $BB mount -t sysfs none /sys; $BB mount -t devtmpfs none /dev 2>/dev/null
$BB mkdir -p /run /tmp /dev/pts; $BB mount -t tmpfs none /tmp; $BB mount -t devpts none /dev/pts 2>/dev/null
$BB date -u -s "@$($BB cat /.host_epoch)" >/dev/null 2>&1
echo "=== GUEST $(uname -r) $(date -u) ==="
$BB insmod /ublk_drv.ko 2>/dev/null
echo "kvm:  $(ls -l /dev/kvm 2>&1)"
echo "ublk: $(ls -l /dev/ublk-control 2>&1)"
$BB ip link set eth0 up 2>/dev/null
$BB ip addr add 10.0.2.15/24 dev eth0 2>/dev/null
$BB ip route add default via 10.0.2.2 2>/dev/null
printf 'nameserver 8.8.8.8\n' > /etc/resolv.conf
echo "ip:   $($BB ip -4 addr show eth0 2>/dev/null | $BB grep -o 'inet [0-9.]*')"
echo "=== ready (guest initramfs shell) ==="
exec /bin/sh
EOF
  chmod +x ubroot/init
  ( cd ubroot && find . | cpio -o -H newc 2>/dev/null | gzip -1 > ../ub-guest.cpio.gz )
  log "build 完成：$CACHE/ub-guest.cpio.gz"
}

cmd_up(){
  cd "$CACHE"
  [ -s ub-guest.cpio.gz ] || die "请先 build"
  local ks="uimg/boot/vmlinuz-${UBUNTU_KVER}"; [ -s "$ks" ] || die "缺内核 $ks"
  local share; share="$(qemu_share)"
  [ -s "$share/edk2-aarch64-code.fd" ] || log "警告: 缺 edk2 固件（-kernel 引导不需要）"
  exec "$QEMU" -machine virt,virtualization=on,gic-version=3 -cpu max -accel tcg \
    -m "$MEM" -smp "$SMP" -display none -monitor none -serial file:serial.log \
    -nic "user,model=virtio-net-pci,hostfwd=tcp::${SSH_PORT}-:22" \
    -kernel "$ks" -initrd ub-guest.cpio.gz -append 'console=ttyAMA0 rdinit=/init'
}

cmd_down(){ for p in $(pgrep -f "qemu-system-aarch64 .*ub-guest.cpio.gz" 2>/dev/null || true); do kill "$p" 2>/dev/null || true; done; log "已停止"; }

# 回收：停虚机 + 删缓存/产物（不碰仓库代码）
cmd_clean(){
  cmd_down || true
  local keep="${KEEP:-}"
  [ -n "$keep" ] && log "KEEP=$keep 指定保留，仅停虚机" && return 0
  log "删除缓存与产物：$CACHE"
  rm -rf "$CACHE"/{ubroot,uimg,umod,debs,debs-x,zs,rootfs,sboxroot,seed,CIDATA} 2>/dev/null || true
  rm -f "$CACHE"/{ub-guest.cpio.gz,guest-initramfs.cpio.gz,noble.qcow2,ubuntu-arm64.img,vars.fd,serial.log,ub-serial.log,guest-serial.log,qemu-console.log} 2>/dev/null || true
  log "回收完成（保留下载的 tarball/apk/deb 以便再次 build；要全删用 RM_ALL=1 clean）"
  [ "${RM_ALL:-}" = "1" ] && rm -rf "$CACHE" && log "已全删 $CACHE"
}

# 列出当前虚机与占用端口（便于确认无残留）
cmd_ps(){ pgrep -af "qemu-system-aarch64" || echo "(无 qemu 进程)"; log "SSH 转发端口: $SSH_PORT"; }
cmd_console(){ tail -f "$CACHE/serial.log"; }
cmd_status(){ pgrep -af "qemu-system-aarch64" || echo "(无虚机在跑)"; [ -f "$CACHE/serial.log" ] && tail -5 "$CACHE/serial.log"; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  fetch) cmd_fetch "$@";; build) cmd_build "$@";; up) cmd_up "$@";;
  down) cmd_down "$@";; console) cmd_console "$@";; status) cmd_status "$@";;
  clean) cmd_clean "$@";; ps) cmd_ps "$@";;
  *) sed -n '2,25p' "$0";;
esac
