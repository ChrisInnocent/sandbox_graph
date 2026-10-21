import asyncio
import pytest
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.domain.models import DeliveryManifest,NodeResult
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.closure import ClosureService
from sandbox_graph.runtime.agents import AgentGateway,ContextBudget,DiagnosisProposal
from sandbox_graph.planning.changes import diff
from sandbox_graph.persistence.repository import Conflict


def delivery(store):
    evidence=store.commit([b'{"samples":[1,2]}'],scope='G_A',media_type='application/json')
    return DeliveryManifest(delivery_id='d',version=1,scope='G_A',plan_ref='p',contract_ref='c',work_refs=('w',),evidence=(evidence,),result=NodeResult(execution_status='succeeded',data_status='valid'))


def test_review_rereads_evidence_and_freezes_exact_versions(repository,tmp_path):
    store=FileArtifactStore(tmp_path); service=ReviewService(repository,store)
    first=delivery(store)
    assert service.review_delivery(first,expected_scope='G_A',plan_ref='p',contract_ref='c')['decision']=='accepted'
    frozen=service.freeze_analysis('p',{'G_A':('d',1)},rules_ref='rules:1')
    second=first.model_copy(update={'version':2,'gaps':('late gap',)})
    service.review_delivery(second,expected_scope='G_A',plan_ref='p',contract_ref='c')
    assert service.get_analysis(frozen)['deliveries']['G_A']==['d',1]
    store.path_for(first.evidence[0]).unlink()
    rejected=service.review_delivery(first,expected_scope='G_A',plan_ref='p',contract_ref='c')
    assert rejected['decision']=='rejected'
    assert first.result.execution_status=='succeeded'


def test_closure_requires_dispositions_current_input_and_cleanup_truth(repository):
    closure=ClosureService(repository)
    result=NodeResult(execution_status='blocked')
    assert closure.classify_scope(result,processing_ref=None)=='open'
    result=result.model_copy(update={'scope_disposition':'blocked_closed','cleanup_status':'unknown'})
    assert closure.classify_scope(result,processing_ref='decision:q')=='partial'
    with repository.transaction() as tx: tx.put('closure_revisions','r',{'revision':2})
    with pytest.raises(Conflict): closure.seal_archive('r',expected_revision=1,analysis_ref='a',report_analysis_ref='a',excel_analysis_ref='a',scopes={'w':(result,'decision:q')},inflight=(),artifact_refs=())
    archive=closure.seal_archive('r',expected_revision=2,analysis_ref='a',report_analysis_ref='a',excel_analysis_ref='a',scopes={'w':(result,'decision:q')},inflight=(),artifact_refs=())
    assert archive['status']=='partial'
    assert archive['cleanup']['w']=='unknown'


class ProposalModel:
    def __init__(self,proposal): self.proposal=proposal
    async def diagnose(self,context): return self.proposal,None,'synthetic-model:1'


def test_agent_rejects_cross_scope_fabrication_and_budgets(repository):
    good={'facts':({'statement':'failed','evidence_refs':('e',)},),'hypotheses':(), 'suggested_actions':('reconcile',), 'affected_scope':('G_A',),'required_decisions':()}
    gateway=AgentGateway(repository,ProposalModel(good),registered_actions={'reconcile'})
    context=gateway.build_context(scope='G_A',node_id='N05',fields={'problem':'failed','evidence_refs':['e'],'other_group_logs':['secret']},budget=ContextBudget(max_input_bytes=500,max_output_bytes=500,max_calls=1,max_tokens=100,deadline=1100),allowed_fields={'problem','evidence_refs'})
    assert 'other_group_logs' not in context['fields']
    proposal=asyncio.run(gateway.diagnose(context,accessible_evidence={'e'},resources_disposed=True))
    assert proposal.suggested_actions==('reconcile',)
    with pytest.raises(Conflict): asyncio.run(gateway.diagnose(context,accessible_evidence={'e'},resources_disposed=True))
    bad=DiagnosisProposal.model_validate(good|{'affected_scope':('G_B',)})
    with pytest.raises(PermissionError): gateway.validate_proposal(bad,scope='G_A',accessible_evidence={'e'})
    with pytest.raises(ValueError): DiagnosisProposal.model_validate(good|{'measured_metric':1})


def test_change_impact_distinguishes_recompute_and_new_measurement():
    assert diff({'threshold':1},{'threshold':2}).rerun_nodes==('A3','A4','A5','A7','F1','F2','F4')
    assert diff({'spec':'small'},{'spec':'large'}).requires_measurement
    assert diff({'completion_event':'ready'},{'completion_event':'exec'},available_events={'ready'}).requires_measurement
    assert not diff({'wording':'old'},{'wording':'new'}).requires_measurement


def test_actual_new_review_invalidates_run_closure_snapshot(repository,tmp_path):
    from sandbox_graph.domain.models import RunRecord
    from sandbox_graph.services.revisions import RevisionService
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='t',graph_ref='g',contract_ref='c',active_plan_ref='p',auth_ref='auth',lineage_id='r'))
    store=FileArtifactStore(tmp_path);reviews=ReviewService(repository,store)
    first=delivery(store)
    reviews.review_delivery(first,expected_scope='G_A',plan_ref='p',contract_ref='c')
    with repository.transaction() as tx: snapshot=tx.get('closure_revisions','r')['body']['revision']
    reviews.review_delivery(first.model_copy(update={'version':2}),expected_scope='G_A',plan_ref='p',contract_ref='c')
    result=NodeResult(execution_status='succeeded',data_status='valid',scope_disposition='fulfilled')
    with pytest.raises(Conflict,match='closure revision'):
        ClosureService(repository).seal_archive('r',expected_revision=snapshot,analysis_ref='a',report_analysis_ref='a',excel_analysis_ref='a',scopes={'w':(result,'review:r')},inflight=(),artifact_refs=())


def test_identical_delivery_review_replay_does_not_dirty_new_analysis(repository,tmp_path):
    store=FileArtifactStore(tmp_path);reviews=ReviewService(repository,store);item=delivery(store)
    first=reviews.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')
    reviews.freeze_analysis('p',{'G_A':('d',1)},rules_ref='r')
    second=reviews.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')
    assert first==second
    with repository.transaction() as tx: assert tx.get('analysis_dirty','p')['body']['dirty'] is False


def test_success_summary_without_verified_raw_is_not_accepted(repository,tmp_path):
    service=ReviewService(repository,FileArtifactStore(tmp_path))
    item=DeliveryManifest(delivery_id='summary',version=1,scope='G_A',plan_ref='p',contract_ref='c',work_refs=('w',),evidence=(),result=NodeResult(execution_status='succeeded'))
    assert service.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')['decision']=='rejected'


def test_repaired_raw_appends_new_review_after_rejection(repository,tmp_path):
    store=FileArtifactStore(tmp_path);service=ReviewService(repository,store);item=delivery(store)
    first=service.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')
    path=store.path_for(item.evidence[0]);data=path.read_bytes();path.unlink()
    rejected=service.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')
    path.write_bytes(data)
    repaired=service.review_delivery(item,expected_scope='G_A',plan_ref='p',contract_ref='c')
    assert repaired['decision']=='accepted'
    assert repaired['review_id'] not in {first['review_id'],rejected['review_id']}
    with repository.transaction() as tx:
        assert tx.get('delivery_reviews','d:1')['body']['review_id']==repaired['review_id']
        assert len(tx.scan('reviews'))==3


def test_archive_rechecks_fence_and_actual_inflight_inside_seal(repository):
    from sandbox_graph.domain.models import RunRecord
    from sandbox_graph.services.revisions import RevisionService
    from sandbox_graph.runtime.leases import LeaseService
    from sandbox_graph.persistence.repository import StaleOwner
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref='p',contract_ref='c',auth_ref='a',lineage_id='r'))
    token=LeaseService(repository).acquire_thread('report','owner',30)
    result=NodeResult(execution_status='blocked',data_status='insufficient',cleanup_status='unknown',scope_disposition='blocked_closed')
    args=dict(expected_revision=0,expected_plan_ref='p',token=token,analysis_ref='a',report_analysis_ref='a',excel_analysis_ref='a',scopes={'w':(result,'review:r')},inflight=False,artifact_refs=())
    with repository.transaction() as tx:
        tx.put('operations','op',{'run_id':'r','scope':'w','phase':'running'})
    with pytest.raises(Conflict,match='in-flight'): ClosureService(repository).seal_archive('r',**args)
    with repository.transaction() as tx:
        tx.put('operations','op',{'run_id':'r','scope':'w','phase':'uncertain'})
        tx.put('resource_leases','host',{'scope':'w','status':'quarantine'})
    assert ClosureService(repository).seal_archive('r',**args)['status']=='partial'
    repository.clock.advance(31)
    with pytest.raises(StaleOwner): ClosureService(repository).seal_archive('r',**args)
