import pytest

def test_guest_nonce_remains_memory_only_and_touch_real_pages(tmp_path):
    from sandbox_bench.guest_probe import GuestProbe
    probe=GuestProbe(tmp_path,max_bytes=16384)
    before=probe.inspect()
    touched=probe.handle({'action':'touch','bytes':8193})
    assert touched['allocated_bytes']==8193
    assert touched['touched_pages']==3
    assert probe.inspect()['nonce_sha256']==before['nonce_sha256']
    assert list(tmp_path.iterdir())==[]
    assert GuestProbe(tmp_path,max_bytes=16384).inspect()['nonce_sha256']!=before['nonce_sha256']
    with pytest.raises(ValueError):probe.handle({'action':'touch','bytes':16385})
    with pytest.raises(ValueError):probe.handle({'action':'execute','command':'anything'})

def test_fixture_continuity_distinguishes_memory_and_file(tmp_path):
    from sandbox_bench.guest_probe import GuestProbe
    from sandbox_bench.fixtures import verify_continuity,verify_effective_spec,FirstCommandLedger
    p=GuestProbe(tmp_path,max_bytes=4096)
    a=p.handle({'action':'write_file','name':'fixture','content':'disk'})
    before=p.inspect();after=GuestProbe(tmp_path,max_bytes=4096).inspect()
    assert verify_continuity(before,after,mode='disk')['valid'] is True
    assert verify_continuity(before,after,mode='memory')['valid'] is False
    assert verify_effective_spec({'vcpus':2,'memory_bytes':2048},{'vcpus':None,'memory_bytes':None})['status']=='unverified'
    first=FirstCommandLedger(tmp_path/'commands.json')
    first.reserve('s','generation-1')
    with pytest.raises(ValueError):FirstCommandLedger(tmp_path/'commands.json').reserve('s','generation-1')
    first.reserve('s','generation-2')

def test_file_probe_rejects_escape_and_symlink(tmp_path):
    from sandbox_bench.guest_probe import GuestProbe
    p=GuestProbe(tmp_path,max_bytes=4096)
    for name in ('../escape','/tmp/escape'):
        with pytest.raises(ValueError):p.handle({'action':'write_file','name':name,'content':'x'})
    (tmp_path/'link').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError):p.handle({'action':'write_file','name':'link','content':'x'})
