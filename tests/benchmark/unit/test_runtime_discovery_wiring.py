import json
from pathlib import Path
import pytest
from tests.benchmark.unit import test_wiring
from tests.benchmark.scenarios.test_scenario_density import density_case
from tests.environment.unit.test_dynamic_inventory import process
from sandbox_bench.wiring import configured_session


def test_sampler_discovers_new_runtime_and_preserves_snapshot_identity(tmp_path):
    bound=test_wiring.WiringTests().observed_config(tmp_path);bound['product']='kuasar'
    runtime=tmp_path/'runtime';(runtime/'sandboxes/s1').mkdir(parents=True)
    machine=tmp_path/'machine';machine.write_text('machine')
    binding=bound['wiring']['sampler'];proc=Path(binding['proc_root'])
    # A real management executable in the controlled proc tree is separately attributed.
    process(proc,42,'/bin/node-ctl',['node-ctl','conductor'],ticks=7)
    process(proc,43,'/bin/cloud-hypervisor',['ch','--api-socket',str(runtime/'sandboxes/s1/ch.sock')],ticks=8)
    binding.update(kind='collector_procfs',snapshot_path=str(tmp_path/'dynamic.json'),discovery={
        'product':'kuasar','roles':['conductor','runtime'],'runtime_root':str(runtime),'machine_id_file':str(machine)})
    bound['wiring']['cohort']={'kind':'owned_api','node':'node','node_field':'node','scope_ref':'owned',
        'complete_listing_evidence_ref':'test-list-proof'}
    resources={'interval_s':1,'window_s':2,'max_wait_s':5,'max_gap_s':2,'inventory_ref':'inventory'}
    session=configured_session(density_case('B10.GROWTH_LIST',product='kuasar'),bound,tmp_path/'session')
    session.adapter.list_owned=lambda work,timeout:[{'sandbox_id':'s1'}]
    session.adapter.info=lambda handle,timeout:{'node':'node','metadata':{'run_id':'work'},'state':'running'}
    try:
        first=session._capture_sample()
        assert {p['identity']['pid'] for p in first['processes']}=={42,43}
        assert first['snapshot_cohort_ids']==['s1']
        session._capture_sample()
        doc=json.loads((tmp_path/'dynamic.json').read_text());assert doc['snapshot_version']==2
        assert doc['work_id']=='work' and doc['complete']
        session.adapter.info=lambda handle,timeout:{'node':'foreign','metadata':{'run_id':'work'},'state':'running'}
        with pytest.raises(ValueError,match='placement'):session._capture_sample()
        assert not session.sampler.safe_to_submit
    finally:session.sampler.close();session._sampler_pool.shutdown(wait=True)
