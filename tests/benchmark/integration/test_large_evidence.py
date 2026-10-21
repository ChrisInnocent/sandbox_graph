"""BTC-052: real deterministic graphs keep 10,000-line evidence out of node context."""
import json
from pathlib import Path
import pytest
from sandbox_bench.analysis import export_case, recompute_case, verify_case
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.agents import AgentGateway
from tests.benchmark.integration import test_analysis_graph as analysis_graph
from tests.benchmark.integration import test_work_graph_session as work_graph


class ModelSpy:
    def __init__(self):self.calls=[]
    async def diagnose(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        raise AssertionError('deterministic measurement/analysis/report must never invoke a model')


@pytest.mark.asyncio
async def test_btc052_large_raw_is_archived_but_node_outputs_stay_bounded_and_model_free(tmp_path,monkeypatch):
    raw=''.join(f'PRIVATE_RAW_SENTINEL {i:05d} '+('x'*80)+'\n' for i in range(10000)).encode()
    spy=ModelSpy()
    monkeypatch.setattr(AgentGateway,'diagnose',spy.diagnose)
    observed=[]
    real_execute=NodeExecutor.execute
    async def checked_execute(self,node,state):
        result,receipt=await real_execute(self,node,state)
        payload=result.model_dump_json().encode()
        assert len(payload)<=65536, (node,len(payload))
        assert b'PRIVATE_RAW_SENTINEL' not in payload
        observed.append(node)
        return result,receipt
    monkeypatch.setattr(NodeExecutor,'execute',checked_execute)
    real_operation=work_graph.Scenario.execute
    async def noisy_operation(self,index):
        result=await real_operation(self,index)
        result.value['raw_log']=raw.decode()
        return result
    monkeypatch.setattr(work_graph.Scenario,'execute',noisy_operation)
    await work_graph.test_real_work_graph_runs_receipts_analysis_and_replay(tmp_path/'measurement')
    measurement=tmp_path/'measurement/evidence'
    refs=json.loads((measurement/'raw_refs.json').read_text())
    load_ref=next(ref for ref in refs if ref['logical_name']=='load.json')
    assert b'PRIVATE_RAW_SENTINEL' in (measurement/load_ref['path']).read_bytes()
    before=(measurement/'summary.json').read_bytes()
    assert recompute_case(measurement)==recompute_case(measurement)
    assert (measurement/'summary.json').read_bytes()==before
    captured=[]
    def seal_with_log(records,destination,**kwargs):
        manifest=export_case(records,destination,raw_payloads={'observer.log':raw},**kwargs)
        captured.append(Path(destination))
        return manifest
    monkeypatch.setattr(analysis_graph,'export_case',seal_with_log)
    await analysis_graph.test_frozen_cases_flow_through_analysis_excel_report_and_archive(tmp_path/'analysis',False)
    for directory in captured:
        verify_case(directory)
        references=json.loads((directory/'raw_refs.json').read_text())
        reference=next(ref for ref in references if ref['logical_name']=='observer.log')
        assert (directory/reference['path']).read_bytes()==raw
        assert b'PRIVATE_RAW_SENTINEL' not in (directory/'summary.json').read_bytes()
    assert set(f'N{i:02}' for i in range(11))<=set(observed)
    assert {'A3','A4','A7','F1','F4'}<=set(observed)
    assert spy.calls==[]
