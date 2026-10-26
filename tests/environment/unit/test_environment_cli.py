import json
import pytest
from sandbox_graph.environment.cli import parser,build_bundle,load_bundle
from sandbox_graph.artifacts.store import FileArtifactStore
from tests.environment.unit.test_planner import declaration,report,catalog
from sandbox_graph.environment.planner import RepairPlanner
from sandbox_graph.domain.models import canonical_hash

@pytest.mark.parametrize('command',['check','plan','apply','worker','status','reconcile','report'])
def test_all_environment_commands_are_real_parser_entries(command):
    with pytest.raises(SystemExit) as caught:parser().parse_args([command,'--help'])
    assert caught.value.code==0


def test_frozen_local_plan_rejects_tampered_report_or_runtime(tmp_path):
    store=FileArtifactStore(tmp_path/'artifacts');d=declaration()
    r=report().model_copy(update={'valid_until':'2100-01-01T00:00:00Z'})
    ref=store.commit([r.model_dump_json().encode()],scope='check',media_type='application/json',producer='environment-probe.v1')
    runtime={'schema_version':1,'references':{},'actions':[],'bindings':[]}
    bundle=build_bundle(d,r,ref,runtime,planner=RepairPlanner(catalog()),node_ids=['n1'],
        actions=['cube.service.start.v1'],deadline=2000.,now=1000.)
    path=tmp_path/'plan.json';path.write_text(json.dumps(bundle))
    assert load_bundle(path,store=store,runtime=runtime,now=1000.)['repair_plan']['steps']
    with pytest.raises(ValueError,match='runtime'):load_bundle(path,store=store,runtime=runtime|{'extra':True},now=1000.)
    target=store.path_for(ref);target.write_bytes(b'{}')
    with pytest.raises(ValueError,match='digest|size'):load_bundle(path,store=store,runtime=runtime,now=1000.)


def test_plan_execution_budget_is_independent_of_initial_report_freshness(tmp_path):
    from datetime import datetime,timezone
    store=FileArtifactStore(tmp_path/'artifacts');d=declaration()
    r=report().model_copy(update={'valid_until':datetime.fromtimestamp(1100,timezone.utc).isoformat()})
    ref=store.commit([r.model_dump_json().encode()],scope='check',media_type='application/json',producer='environment-probe.v1')
    bundle=build_bundle(d,r,ref,{},planner=RepairPlanner(catalog()),node_ids=['n1'],actions=['cube.service.start.v1'],deadline=2000.,now=1000.)
    assert bundle['deadline']==2000.
    path=tmp_path/'plan.json';path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError,match='expired'):load_bundle(path,store=store,runtime={},now=1200.)
    assert load_bundle(path,store=store,runtime={},now=2100.,historical_replay=True)==bundle


def test_worker_configuration_failure_does_not_leak_a_connection_pool(tmp_path,monkeypatch):
    from sandbox_graph.environment.cli import prepare_worker
    from sandbox_graph.persistence.checkpointer import FencedPostgresSaver  # import real generic before replacing constructor
    from types import SimpleNamespace
    opened=[]
    class Pool:
        def __init__(self,*args,**kwargs):self.closed=False;opened.append(self)
        def close(self):self.closed=True
    monkeypatch.setattr('psycopg_pool.ConnectionPool',Pool)
    with pytest.raises((KeyError,ValueError)):
        prepare_worker(SimpleNamespace(dsn='not-used'),{},FileArtifactStore(tmp_path/'artifacts'),'worker')
    assert all(pool.closed for pool in opened)
