"""Linux host facts and role-aware prerequisite assessment."""
from __future__ import annotations

import os
import errno
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class HostFacts:
    cpu_count: int
    memory_bytes: int
    architecture: str
    kernel: str
    kvm_device_exists: bool
    kvm_module_loaded: bool
    kvm_openable: bool
    cgroup_version: str
    systemd_available: bool
    container_runtimes: tuple[str, ...]
    network_interfaces: tuple[str, ...]
    pss_readable: bool
    disk_bytes: int
    virtualization_supported: bool | None = None
    kvm_open_error: str | None = None


@dataclass(frozen=True)
class RoleCheck:
    status: Literal["satisfied", "missing", "mismatch", "unknown", "not_applicable"]
    reason: str
    remediation: str | None = None


def classify_kvm(*, device_exists: bool, module_loaded: bool, openable: bool,
                 virtualization_supported: bool | None = None, open_error: str | None = None) -> RoleCheck:
    if virtualization_supported is False:
        return RoleCheck("missing", "cpu_virtualization_not_supported")
    if openable and device_exists:
        return RoleCheck("satisfied", "kvm_device_openable")
    if open_error and open_error not in {"EACCES", "EPERM"}:
        return RoleCheck("unknown", "kvm_open_failed:"+open_error)
    if not module_loaded:
        return RoleCheck("missing", "module_not_loaded_and_device_absent" if not device_exists else "module_not_loaded",
                         remediation="load_kvm_module")
    if not device_exists:
        return RoleCheck("missing", "kvm_device_absent", remediation="inspect_kvm_device_setup")
    return RoleCheck("mismatch", "permission_denied", remediation="grant_scoped_device_access")


_COMPUTE_ROLES = {"compute", "runtime", "server", "worker"}


def assess_role(role: str, facts: HostFacts) -> dict:
    if role in _COMPUTE_ROLES:
        kvm = classify_kvm(device_exists=facts.kvm_device_exists,
                           module_loaded=facts.kvm_module_loaded,
                           openable=facts.kvm_openable,
                           virtualization_supported=facts.virtualization_supported,
                           open_error=facts.kvm_open_error)
    else:
        kvm = RoleCheck("not_applicable", "role_does_not_launch_virtual_machines")
    checks = {
        "cpu": RoleCheck("satisfied" if facts.cpu_count > 0 else "unknown", "cpu_count_observed"),
        "memory": RoleCheck("satisfied" if facts.memory_bytes > 0 else "unknown", "memory_observed"),
        "systemd": RoleCheck("satisfied" if facts.systemd_available else "missing", "systemd_available" if facts.systemd_available else "systemd_missing"),
        "kvm": kvm,
    }
    required = [checks["cpu"], checks["memory"], checks["systemd"]]
    if role in _COMPUTE_ROLES:
        required.append(kvm)
    met = all(check.status in {"satisfied", "not_applicable"} for check in required)
    return {"role": role, "checks": checks, "kvm": kvm, "requirements_met": met}


def collect_host_facts(*, proc_root: str | Path = "/proc", sys_root: str | Path = "/sys",
                       dev_root: str | Path = "/dev", systemd_root: str | Path = "/run/systemd/system") -> HostFacts:
    if platform.system() != "Linux":
        raise OSError("host inventory requires Linux procfs and sysfs")
    proc, sysfs, dev = Path(proc_root), Path(sys_root), Path(dev_root)
    meminfo = (proc / "meminfo").read_text(encoding="ascii")
    memory_kib = next((int(line.split()[1]) for line in meminfo.splitlines()
                       if line.startswith("MemTotal:") and len(line.split()) >= 2), 0)
    cpuinfo = (proc / "cpuinfo").read_text(encoding="ascii", errors="replace").lower()
    flags = " ".join(line.partition(":")[2] for line in cpuinfo.splitlines()
                     if line.strip().startswith(("flags", "features")))
    architecture = platform.machine()
    virtualization_supported = (any(flag in f" {flags} " for flag in (" vmx ", " svm "))
        if architecture.lower() in {"x86_64", "amd64", "i386", "i686"} and flags.strip() else None)
    kvm_path = dev / "kvm"
    device_exists = False
    try:
        device_exists = stat.S_ISCHR(kvm_path.stat().st_mode)
    except FileNotFoundError:
        pass
    module_loaded = any((sysfs / "module" / name).exists() for name in ("kvm", "kvm_intel", "kvm_amd"))
    openable = False
    open_error = None
    if device_exists:
        try:
            fd = os.open(kvm_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            os.close(fd)
            openable = True
        except OSError as exc:
            open_error = errno.errorcode.get(exc.errno, "UNKNOWN")
    cgroup_version = "v2" if (proc / "self" / "cgroup").exists() and "0::" in (proc / "self" / "cgroup").read_text(errors="replace") else "v1"
    systemd_available = Path(systemd_root).is_dir()
    runtimes = tuple(name for name in ("containerd", "docker", "podman") if shutil.which(name))
    net_root = sysfs / "class" / "net"
    interfaces = tuple(sorted(path.name for path in net_root.iterdir())) if net_root.is_dir() else ()
    try:
        with (proc / "self" / "smaps_rollup").open("rb"):
            pss_readable = True
    except OSError:
        pss_readable = False
    disk_bytes = shutil.disk_usage("/").free
    return HostFacts(max(os.cpu_count() or 0, 0), memory_kib * 1024,
        architecture, platform.release(), device_exists, module_loaded, openable,
        cgroup_version, systemd_available, runtimes, interfaces, pss_readable, disk_bytes,
        virtualization_supported, open_error)
