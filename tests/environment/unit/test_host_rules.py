from sandbox_graph.environment.probes.host import HostFacts, assess_role, classify_kvm


def facts(**updates):
    base = dict(cpu_count=16, memory_bytes=64 * 1024**3, architecture="x86_64", kernel="6.8.0",
        kvm_device_exists=False, kvm_module_loaded=False, kvm_openable=False, cgroup_version="v2",
        systemd_available=True, container_runtimes=("containerd",), network_interfaces=("eth0",),
        pss_readable=False, disk_bytes=100 * 1024**3)
    base.update(updates)
    return HostFacts(**base)


def test_management_role_does_not_require_kvm():
    result = assess_role("control", facts())
    assert result["kvm"].status == "not_applicable"
    assert result["requirements_met"] is True


def test_compute_role_distinguishes_missing_device_and_module():
    result = assess_role("compute", facts())
    assert result["kvm"].status == "missing"
    assert result["kvm"].reason == "module_not_loaded_and_device_absent"


def test_kvm_permission_failure_is_not_reported_as_missing_hardware():
    state = classify_kvm(device_exists=True, module_loaded=True, openable=False)
    assert state.status == "mismatch"
    assert state.reason == "permission_denied"
    assert assess_role("compute", facts(kvm_device_exists=True, kvm_module_loaded=True))["requirements_met"] is False


def test_compute_role_passes_when_kvm_is_openable():
    result = assess_role("runtime", facts(kvm_device_exists=True, kvm_module_loaded=True, kvm_openable=True))
    assert result["requirements_met"] is True
    assert result["kvm"].status == "satisfied"
