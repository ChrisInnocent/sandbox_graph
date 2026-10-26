import json
import pytest
from sandbox_graph.testing.host_acceptance import run_host_acceptance
from sandbox_graph.environment.transport import SSHResult


def contract(tmp_path):
    return {'schema_version':1,'ssh':{'address':'host.example','port':22,'user':'tester',
        'known_hosts':str(tmp_path/'known_hosts'),'identity_file':str(tmp_path/'identity')},
        'helper_path':'/opt/helper','machine_id':'host-a','role':'compute','require_pss':True,
        'output':str(tmp_path/'result'),'timeout_seconds':5}


class Transport:
    def __init__(self,fault=None):self.actions=[];self.fault=fault
    async def request(self,request):
        action=request.payload['action'];self.actions.append(action)
        if action=='probe.access.v1':
            row={'machine_id':'host-a','boot_id':'boot','uid':0,'can_exec':True}
            if len(self.actions)>1 and self.fault=='boot':row['boot_id']='rebooted'
        else:
            row={'facts':{'architecture':'aarch64','kernel':'test-fixture','cpu_count':4,'memory_bytes':4096,
                'pss_readable':True,'systemd_available':True,'kvm_device_exists':True,'kvm_module_loaded':True,
                'kvm_openable':True,'cgroup_version':'v2','container_runtimes':[], 'network_interfaces':['eth0'],
                'disk_bytes':100,'virtualization_supported':None},'role':{'role':'compute','requirements_met':True}}
            if self.fault=='kvm':row['facts']['kvm_openable']=False
            if self.fault=='pss':row['facts']['pss_readable']=False
        return SSHResult(0,json.dumps(row).encode(),b'')


@pytest.mark.asyncio
async def test_host_acceptance_uses_readonly_real_protocol_and_seals_private_output(tmp_path):
    value=contract(tmp_path);transport=Transport()
    result=await run_host_acceptance(value,transport=transport)
    assert result['status']=='passed' and result['evidence_scope']=='host_primitives_only'
    assert transport.actions==['probe.access.v1','probe.host.v1','probe.access.v1']
    assert (tmp_path/'result/observations.json').stat().st_mode&0o777==0o600
    assert 'identity_file' not in (tmp_path/'result/summary.json').read_text()
    with pytest.raises(FileExistsError):await run_host_acceptance(value,transport=transport)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['boot','kvm','pss'])
async def test_host_acceptance_rejects_drift_and_missing_primitives(tmp_path,fault):
    result=await run_host_acceptance(contract(tmp_path),transport=Transport(fault))
    assert result['status']=='blocked'
    assert result['reason'] in {'identity_changed','host_requirements_not_met','pss_unavailable'}
