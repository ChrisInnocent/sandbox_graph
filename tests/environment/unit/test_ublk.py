from types import SimpleNamespace
import pytest


def test_current_module_parameter_is_distinct_from_requested_value(tmp_path):
    from sandbox_graph.environment.probes.ublk import inspect_ublk
    sys=tmp_path/'sys';dev=tmp_path/'dev';dev.mkdir()
    path=sys/'module/ublk_drv/parameters';path.mkdir(parents=True);(path/'ublks_max').write_text('512\n')
    value=inspect_ublk(sys_root=sys,dev_root=dev)
    assert value['loaded'] and value['ublks_max']==512
    assert value['evidence_level']=='runtime_effective' and not value['control_device_exists']


def test_live_module_parameter_mismatch_never_unloads_module():
    from sandbox_graph.environment.products.host_tuning import activate_ublk
    calls=[]
    def run(argv,**kwargs):calls.append(argv);return SimpleNamespace(returncode=0,stdout='ublks_max:int\n')
    with pytest.raises(ValueError,match='maintenance'):
        activate_ublk(1024,probe=lambda:{'loaded':True,'ublks_max':512},runner=run)
    assert calls==[]
    assert activate_ublk(512,probe=lambda:{'loaded':True,'ublks_max':512},runner=run)['reused']


def test_absent_module_requires_parameter_support_then_independent_readback():
    from sandbox_graph.environment.products.host_tuning import activate_ublk
    calls=[];observations=iter([{'loaded':False,'ublks_max':None},{'loaded':True,'ublks_max':1024}])
    def run(argv,**kwargs):calls.append(argv);return SimpleNamespace(returncode=0,stdout='ublks_max:maximum devices\n')
    assert activate_ublk(1024,probe=lambda:next(observations),runner=run)['status']=='completed'
    assert calls==[['modinfo','-p','ublk_drv'],['modprobe','ublk_drv','ublks_max=1024']]
    for target in (0,-1,True,1.5,2**32):
        with pytest.raises(ValueError):activate_ublk(target,runner=run)
    with pytest.raises(ValueError,match='unsupported'):
        activate_ublk(1024,probe=lambda:{'loaded':False},runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout='other:int'))


def test_ublk_recipe_child_preserves_original_operation_and_unknown_exit(tmp_path,monkeypatch):
    from tests.environment.unit.test_recipe_executor import executor
    from sandbox_graph.environment.products.common import RecipeStep
    service=executor(tmp_path,(RecipeStep('ublk:activate','ublk-limit',(),target='1024'),))
    calls=[]
    def run(step,lock,*,operation_id=None):
        calls.append((step,operation_id));return {'status':'failed','returncode':75}
    monkeypatch.setattr(service,'_command',run)
    result=service.execute('original','test',expected_before={})
    assert result['status']=='unknown'
    assert calls[0][0].argv[-1]=='1024' and calls[0][1].startswith('recipe-step:')


@pytest.mark.asyncio
async def test_ublk_target_uses_remote_kernel_value_without_management_endpoint():
    from sandbox_graph.environment.verification import ProductVerification
    from sandbox_graph.environment.contracts import TargetPolicy,TargetValue
    from tests.environment.unit.test_planner import declaration
    d=declaration().model_copy(update={'product':'agentenv','targets':TargetPolicy(host_attributes={
        'ublks_max':TargetValue(mode='exact',value=1024,unit='count')})})
    node=d.nodes[0].model_copy(update={'roles':('server',)})
    class Probe:
        def commit(self,value):return 'artifact://check/'+('a'*64)
        async def request(self,node,action,data):
            if action=='probe.ublk.v1':return {'loaded':True,'ublks_max':1024,'evidence_level':'runtime_effective'}
            return {'components':{}}
    verify=ProductVerification(probe=Probe(),scope='env',store=None)
    rows=await verify.check(d,node,fresh=True,boot_id='boot')
    match=[r for r in rows if r.check_id=='effective:host_attributes:ublks_max']
    assert len(match)==1 and match[0].status=='satisfied'


def test_unknown_module_presence_never_loads():
    from sandbox_graph.environment.products.host_tuning import activate_ublk
    def forbidden(*args,**kwargs):raise AssertionError('unknown is not absent')
    with pytest.raises(ValueError,match='unknown'):
        activate_ublk(1024,probe=lambda:{'loaded':None},runner=forbidden)
