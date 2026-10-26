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


def _proc_fixture(tmp_path,monkeypatch,architecture):
    from sandbox_graph.environment.probes import host
    proc=tmp_path/'proc';(proc/'self').mkdir(parents=True)
    (proc/'meminfo').write_text('MemTotal: 1024 kB\n')
    (proc/'cpuinfo').write_text('Features: fp asimd\n' if architecture=='aarch64' else 'flags: fpu vmx\n')
    (proc/'self/cgroup').write_text('0::/\n');(proc/'self/smaps_rollup').write_text('Pss: 32 kB\n')
    sysfs=tmp_path/'sys';(sysfs/'module/kvm').mkdir(parents=True)
    dev=tmp_path/'dev';dev.mkdir()
    monkeypatch.setattr(host.platform,'system',lambda:'Linux')
    monkeypatch.setattr(host.platform,'machine',lambda:architecture)
    return host,dict(proc_root=proc,sys_root=sysfs,dev_root=dev,systemd_root=tmp_path)


def test_arm_missing_x86_flags_does_not_claim_virtualization_is_absent(tmp_path,monkeypatch):
    host,inputs=_proc_fixture(tmp_path,monkeypatch,'aarch64')
    observed=host.collect_host_facts(**inputs)
    assert observed.virtualization_supported is None
    assert host.assess_role('compute',observed)['kvm'].reason=='kvm_device_absent'


def test_unusable_kvm_device_is_reported_without_crashing_or_permission_repair(tmp_path,monkeypatch):
    import errno
    import os
    host,inputs=_proc_fixture(tmp_path,monkeypatch,'x86_64')
    # Character-device type comes from /dev/null; its operation is replaced,
    # so this is an explicit observation fixture, not live KVM evidence.
    (inputs['dev_root']/'kvm').symlink_to('/dev/null')
    original=os.open
    def unavailable(path,*args,**kwargs):
        if str(path).endswith('/dev/kvm'):raise OSError(errno.ENODEV,'unavailable fixture device')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(host.os,'open',unavailable)
    observed=host.collect_host_facts(**inputs)
    assert observed.kvm_openable is False and observed.kvm_open_error=='ENODEV'
    check=host.assess_role('compute',observed)['kvm']
    assert check.status=='unknown' and check.reason=='kvm_open_failed:ENODEV'
    assert check.remediation is None
