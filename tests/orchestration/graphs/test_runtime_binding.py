import asyncio
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sandbox_graph.domain.models import NodeResult,RunRecord
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.services.workflows import WorkflowHandlers
from sandbox_graph.graphs.executor import NodeContext


def test_default_bindings_require_pinned_version_and_preserve_token(repository):
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    seen=[]
    async def service(context): return NodeResult(execution_status='succeeded')
    def services(token,activation):
        seen.append((token,activation))
        return {name:service for name in ('E1','E2','C1','C2','C3','T2','A3','A4','A7','F1','F4')}|{f'N{i:02}':service for i in range(11)}
    handlers=build_job_handlers(repository,graph_ref='g',saver_factory=lambda token:InMemorySaver(),node_service_factory=services,review_service=None,deadline=1100,lease_seconds=30,segment_seconds=20)
    assert {'run','group','work','analysis','report','resume'}<=set(handlers)
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='other',contract_ref='c',active_plan_ref='p',auth_ref='a',lineage_id='r'))
    job={'job_id':'j','job_kind':'analysis','scope_ref':'r','run_id':'r','thread_id':'t','activation_id':'act','payload':{'plan_ref':'p','graph_ref':'other'}}
    with pytest.raises(ValueError,match='version'): asyncio.run(handlers['analysis'](job))
    assert not seen


def test_f2_reads_exact_f1_receipt_without_latest_fact_lookup(repository):
    with repository.transaction() as tx:
        tx.put('report_facts','facts:1',{'analysis_ref':'analysis:1'},immutable=True)
        tx.put('receipts','receipt:f1',{'result':{'output':{'facts_ref':'facts:1','analysis_ref':'analysis:1'}}},immutable=True)
    context=NodeContext('F2','owner','t','r','r','p','g','a','v',{'analysis_ref':'analysis:1'},{'F1':'receipt:f1'},1,'lease')
    result=asyncio.run(WorkflowHandlers(repository,deadline=1100).verify_report_facts(context))
    assert result.execution_status=='succeeded' and result.output['facts_ref']=='facts:1'
