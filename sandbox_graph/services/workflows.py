"""Concrete control-plane handlers; business scripts must be explicitly injected.

No missing environment probe, statistics formula, workbook exporter or renderer
is replaced by a synthetic successful result. Work measurement handlers come from
the registered benchmark bridge.
"""
from uuid import uuid4
from langgraph.graph import END
from ..domain.models import NodeResult,ErrorRecord,DeliveryManifest,EvidenceRef,canonical_hash
from ..planning.compiler import Plan
from ..persistence.repository import LeaseToken,Conflict
from ..runtime.tasks import TaskBackend,JobSpec
from ..runtime.waits import WaitService,WaitRecord
from ..runtime.events import EventService,EventEnvelope
from ..graphs.factory import GraphDefinition
from .scheduling import SchedulingService


def blocked(code,message,scope=()):
    return NodeResult(execution_status='blocked',coverage_status='blocked',affected_scope=tuple(scope),error=ErrorRecord(code=code,observed_at=0,message=message))

def token_for(context): return LeaseToken(context.thread_id,context.owner_epoch,context.lease_id,0)

class WorkflowHandlers:
    def __init__(self,repository,*,deadline,node_services=None,review_service=None,max_work_concurrency=1):
        self.repository=repository;self.deadline=deadline;self.node_services=dict(node_services or {});self.review_service=review_service;self.max_work_concurrency=max_work_concurrency
        self.scheduler=SchedulingService(repository,review_service=review_service)
    def mapping(self):
        handlers={'R0':self.restore_run,'R1':self.validate_catalog,'RP':self.restore_plan,'R2':self.dispatch_groups,'R3':self.ingest_deliveries,'R6':self.compile_rework,'R7':self.park_run,
                  'G0':self.restore_group,'T0':self.bind_methods,'T1':self.expand_work,'T4':self.schedule_work,'T5':self.dispatch_work,'T6':self.review_work,'T8':self.summarize_group,'TC':self.record_unsupported,'TB':self.record_gap,'GB':self.record_block,'GW':self.park_group,'GE':self.publish_group,
                  'A1':self.restore_analysis_input,'A2':self.validate_analysis_input,'A5':self.compile_rework,'F2':self.verify_report_facts}
        # T2, E/C, N00-N10, A3/A4/A7, F1/F4 require actual registered service implementations.
        # Their absence is explicit in NodeExecutor and GraphService validation.
        for node,service in self.node_services.items():
            if node in handlers: raise ValueError(f'deterministic control handler cannot be overridden: {node}')
            handlers[node]=service
        return handlers
    def _plan(self,context):
        with self.repository.transaction() as tx:
            row=tx.get('plans',context.plan_ref)
            if not row: raise Conflict('plan not found')
            return Plan.model_validate(row['body'])
    async def restore_run(self,context):
        with self.repository.transaction() as tx:
            row=tx.get('runs',context.run_ref)
            if not row or row['body']['graph_ref']!=context.graph_ref: return blocked('VERSION_CONFLICT','original run definition required')
        return NodeResult(execution_status='succeeded',output={'run_ref':context.run_ref})
    async def validate_catalog(self,context):
        from ..planning.identity import missing_required_identity
        plan=self._plan(context)
        missing=[c.case_id for c in plan.cases if not c.method_id or not c.scenario_id]
        if missing: return blocked('INPUT_MISSING',str(missing))
        identity_missing={c.case_id:list(missing_required_identity(c)) for c in plan.cases if missing_required_identity(c)}
        if identity_missing: return blocked('SOURCE_IDENTITY_MISSING',str(identity_missing))
        return NodeResult(execution_status='succeeded',output={'catalog_ref':plan.catalog_hash})
    async def restore_plan(self,context):
        plan=self._plan(context)
        if plan.status!='ready': return blocked('INPUT_MISSING',f'plan status={plan.status}')
        return NodeResult(execution_status='succeeded',output={'plan_ref':plan.hash})
    async def dispatch_groups(self,context):
        if context.input_refs.get('rework_ref'): return NodeResult(execution_status='succeeded',output={'job_refs':[]})
        jobs=self.scheduler.dispatch_groups(context.run_ref,token=token_for(context),deadline=self.deadline)
        return NodeResult(execution_status='succeeded',output={'job_refs':jobs})
    async def ingest_deliveries(self,context):
        with self.repository.transaction() as tx:
            sources={r['key']:r['body'] for r in tx.scan('group_delivery_sources') if r['body']['run_id']==context.run_ref}
            deliveries=[r['body'] for r in tx.scan('group_deliveries') if r['key'] in sources and r['body']['plan_ref']==context.plan_ref]
            observed_cursor=max((r['body']['aggregate_seq'] for r in tx.scan('events') if r['body']['aggregate_id']==f'run:{context.run_ref}'),default=0)
        if context.input_refs.get('rework_ref'): return NodeResult(execution_status='succeeded',continuation='R6',output={'rework_ref':context.input_refs['rework_ref'],'event_cursor':observed_cursor})
        if not deliveries: return NodeResult(execution_status='succeeded',continuation='R7',output={'new_inputs':False,'event_cursor':observed_cursor})
        if self.review_service is None: return blocked('ADAPTER_MISSING','review service required before incremental analysis')
        newest={}
        for delivery in deliveries:
            old=newest.get(delivery['scope'])
            if old is None or delivery['version']>old['version']: newest[delivery['scope']]=delivery
        plan=self._plan(context)
        for scope,delivery in newest.items():
            key=f'{delivery["delivery_id"]}:{delivery["version"]}'
            allowed={scope}|set(sources[key]['work_refs'])
            self.review_service.review_delivery(DeliveryManifest.model_validate(delivery),expected_scope=scope,plan_ref=context.plan_ref,contract_ref=plan.contract_ref,allowed_evidence_scopes=allowed)
        frozen=self.review_service.freeze_analysis(context.plan_ref,{scope:(d['delivery_id'],d['version']) for scope,d in newest.items()},rules_ref=context.input_refs.get('rules_ref','manifest:1'),missing_scope=sorted({c.group_id for c in plan.cases}-set(newest)),source_kind='group_deliveries')
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            dedupe=f'analysis:{context.run_ref}:{frozen}'
            if not tx.get('job_dedupe',dedupe):
                TaskBackend.enqueue_in(tx,JobSpec(job_id=str(uuid4()),job_kind='analysis',scope_ref=context.run_ref,thread_id=f'{context.run_ref}:analysis:{frozen}',activation_id=str(uuid4()),dedupe_key=dedupe,due_at=tx.now,deadline=self.deadline,run_id=context.run_ref,payload={'analysis_input_ref':frozen,'plan_ref':context.plan_ref,'graph_ref':context.graph_ref}))
        return NodeResult(execution_status='succeeded',continuation='R7',output={'analysis_input_ref':frozen,'event_cursor':observed_cursor})
    async def compile_rework(self,context):
        ref=context.input_refs.get('rework_ref')
        if not ref: return NodeResult(execution_status='succeeded',output={'rework_required':False})
        from .rework import ReworkService
        return ReworkService(self.repository).dispatch(ref,token=token_for(context),run_id=context.run_ref,plan_ref=context.plan_ref,graph_ref=context.graph_ref)
    async def park_run(self,context):
        cursor=int(context.input_refs.get('event_cursor','0'))
        with self.repository.transaction() as tx:
            receipt=tx.get('receipts',context.result_refs.get('R3',''))
            if receipt: cursor=receipt['body']['result']['output'].get('event_cursor',cursor)
        wait=WaitRecord(wait_id=f'{context.activation_id}:groups',activation_id=context.activation_id,thread_id=context.thread_id,scope=context.scope,plan_ref=context.plan_ref,graph_ref=context.graph_ref,version=1,aggregate_id=f'run:{context.run_ref}',cursor=cursor,deadline=self.deadline)
        status=WaitService(self.repository).park(wait)
        return NodeResult(execution_status='succeeded',output={'wait_ref':wait.wait_id,'wait_status':status})
    async def restore_group(self,context):
        plan=self._plan(context)
        if context.scope not in {c.group_id for c in plan.cases}: return blocked('CONTRACT_CONFLICT','group is not in frozen plan')
        self.scheduler.materialize_reuse(context.run_ref,context.scope,token=token_for(context))
        with self.repository.transaction() as tx:
            work=[r['body'] for r in tx.scan('work_instances') if r['body']['run_id']==context.run_ref and r['body']['group_id']==context.scope]
        return NodeResult(execution_status='succeeded',continuation='T6' if work and all(w['status'] in {'delivered','blocked','failed','cancelled','superseded'} for w in work) else 'E1')
    async def bind_methods(self,context):
        cases=[c for c in self._plan(context).cases if c.group_id==context.scope]
        if all(c.capability=='unsupported' for c in cases): return NodeResult(execution_status='succeeded',continuation='TC',coverage_status='unsupported')
        if not any(c.capability=='supported' and c.adapter_verified for c in cases): return blocked('ADAPTER_UNVERIFIED','method adapter not verified')
        return NodeResult(execution_status='succeeded',continuation='T1',output={'methods':[c.method_id for c in cases]})
    async def expand_work(self,context):
        refs=self.scheduler.expand_work(context.run_ref,context.scope,plan_ref=context.plan_ref)
        return NodeResult(execution_status='succeeded',output={'work_refs':refs})
    async def schedule_work(self,context):
        if self.max_work_concurrency<1: return blocked('INPUT_MISSING','finite concurrency required')
        return NodeResult(execution_status='succeeded',output={'max_work_concurrency':self.max_work_concurrency})
    async def dispatch_work(self,context):
        refs=self.scheduler.dispatch_work(context.run_ref,context.scope,token=token_for(context),max_concurrency=self.max_work_concurrency,deadline=self.deadline)
        with self.repository.transaction() as tx:
            work=[r['body'] for r in tx.scan('work_instances') if r['body']['run_id']==context.run_ref and r['body']['group_id']==context.scope and r['body']['plan_ref']==context.plan_ref]
        terminal=work and all(w['status'] in {'delivered','blocked','failed','cancelled','superseded'} for w in work)
        return NodeResult(execution_status='succeeded',continuation='T6' if terminal else None,output={'job_refs':refs})
    async def review_work(self,context):
        with self.repository.transaction() as tx:
            rows=[row['body'] for row in self._selected_work_deliveries(tx,context)]
            work=[r['body'] for r in tx.scan('work_instances') if r['body']['run_id']==context.run_ref and r['body']['group_id']==context.scope and r['body']['plan_ref']==context.plan_ref]
            covered={ref for row in rows for ref in row['work_refs']}
            gaps=[{'work_id':w['work_id'],'status':w['status'],'result_ref':w.get('result_ref'),'reason':'missing_delivery'} for w in work if w['work_id'] not in covered]
            if work and any(w['status'] not in {'delivered','failed','blocked','cancelled','superseded'} for w in work):
                return blocked('INPUT_MISSING','work still running; wait for terminal facts')
            if gaps:
                tx.put('group_review_gaps',f'{context.run_ref}:{context.plan_ref}:{context.scope}',{'scope':context.scope,'gaps':gaps})
        if not rows:
            if gaps:
                self._record_dispositions(context,work,[],[])
                return NodeResult(execution_status='succeeded',review_status='rejected',data_status='insufficient',coverage_status='blocked',output={'review_refs':[],'gaps':gaps})
            return blocked('INPUT_MISSING','no work delivery or terminal gap evidence')
        if self.review_service is None: return blocked('ADAPTER_MISSING','artifact review service missing')
        from ..domain.models import DeliveryManifest
        reviews=[self.review_service.review_delivery(DeliveryManifest.model_validate(row),expected_scope=row['scope'],plan_ref=context.plan_ref,contract_ref=row['contract_ref']) for row in rows]
        self._record_dispositions(context,work,rows,reviews)
        return NodeResult(execution_status='succeeded',review_status='rejected' if gaps or any(r['decision']=='rejected' for r in reviews) else 'accepted',output={'review_refs':[r['review_id'] for r in reviews],'gaps':gaps})
    def _record_dispositions(self,context,work,deliveries,reviews):
        """Deterministic terminal-review:1 closes this delivery with explicit gaps.

        This is a report disposition, not permission to release resources or to
        turn unverified data into valid data. A later rework is a new version.
        """
        indexed={w:(d,r) for d,r in zip(deliveries,reviews) for w in d['work_refs']}
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            changed=False
            for item in work:
                if item['status'] not in {'delivered','failed','blocked','cancelled','superseded'}: continue
                source=indexed.get(item['work_id'])
                if source:
                    delivery,review=source
                    result=NodeResult.model_validate(delivery['result']).model_copy(update={'review_status':review['decision']})
                    processing={'delivery_ref':f'{delivery["delivery_id"]}:{delivery["version"]}','review_ref':review['review_id']}
                else:
                    status={'delivered':'succeeded','superseded':'cancelled'}.get(item['status'],item['status'])
                    result=NodeResult(execution_status=status,data_status='insufficient',review_status='rejected',cleanup_status='unknown',coverage_status='blocked')
                    processing={'work_id':item['work_id'],'result_ref':item.get('result_ref'),'gap':'missing_delivery'}
                disposition='cancelled' if item['status'] in {'cancelled','superseded'} else 'failed' if result.execution_status=='failed' else 'fulfilled' if result.execution_status=='succeeded' and result.data_status=='valid' and result.review_status=='accepted' else 'blocked_closed'
                result=result.model_copy(update={'scope_disposition':disposition})
                processing=processing|{'rule_ref':'terminal-review:1','run_id':context.run_ref,'plan_ref':context.plan_ref}
                ref=canonical_hash(processing);tx.put('scope_processing',ref,processing,immutable=True)
                body={'run_id':context.run_ref,'plan_ref':context.plan_ref,'result':result.model_dump(mode='json'),'processing_ref':ref,'rule_ref':'terminal-review:1'}
                prior=tx.get('scope_dispositions',item['work_id'])
                if not prior or prior['body']!=body:
                    tx.put('scope_dispositions',item['work_id'],body);changed=True
            if changed:
                revision=tx.get('closure_revisions',context.run_ref)
                tx.put('closure_revisions',context.run_ref,{'plan_ref':context.plan_ref,'revision':revision['body']['revision']+1 if revision else 1})
    def _selected_work_deliveries(self,tx,context):
        owned={r['key']:r['body']['work_id'] for r in tx.scan('work_delivery_owners') if r['body']['group_id']==context.scope and r['body']['run_id']==context.run_ref}
        selected={}
        for row in tx.scan('work_deliveries'):
            if row['key'] not in owned or row['body']['plan_ref']!=context.plan_ref: continue
            work_id=owned[row['key']]
            old=selected.get(work_id)
            if old and old['body']['delivery_id']!=row['body']['delivery_id']:
                raise Conflict('ambiguous delivery lineage for one work')
            if old is None or row['body']['version']>old['body']['version']: selected[work_id]=row
        return list({r['key']:r for r in selected.values()}.values())
    async def summarize_group(self,context):
        with self.repository.transaction() as tx:
            records=[r['body'] for r in tx.scan('coverage') if r['body'].get('scope')==context.scope and r['body'].get('run_id')==context.run_ref and r['body'].get('plan_ref')==context.plan_ref]
            work=[r['body'] for r in tx.scan('work_instances') if r['body']['run_id']==context.run_ref and r['body']['group_id']==context.scope and r['body']['plan_ref']==context.plan_ref]
            gaps=tx.get('group_review_gaps',f'{context.run_ref}:{context.plan_ref}:{context.scope}')
            children=self._selected_work_deliveries(tx,context)
            review_refs={}
            for child in children:
                review=tx.get('delivery_reviews',child['key'])
                if not review: return blocked('INPUT_MISSING','child delivery has not passed through T6')
                review_refs[child['key']]=review['body']['review_id']
            summary={'scope':context.scope,'run_id':context.run_ref,'plan_ref':context.plan_ref,'work_refs':sorted(w['work_id'] for w in work),'coverage':records,'gaps':gaps['body']['gaps'] if gaps else [],'deliveries':review_refs}
            ref=canonical_hash(summary);tx.put('group_summaries',ref,summary,immutable=True)
        return NodeResult(execution_status='succeeded',output={'summary_ref':ref})
    async def record_unsupported(self,context):
        self.scheduler.expand_work(context.run_ref,context.scope,plan_ref=context.plan_ref)
        return NodeResult(execution_status='succeeded',coverage_status='unsupported',performance_status='not_applicable')
    async def record_gap(self,context):
        with self.repository.transaction() as tx: tx.put('scope_gaps',f'{context.run_ref}:{context.scope}:{context.visit_id}',{'scope':context.scope,'plan_ref':context.plan_ref,'reason':'adapter_or_contract_missing','result_refs':context.result_refs},immutable=True)
        return blocked('ADAPTER_MISSING','scope gap recorded')
    async def record_block(self,context):
        with self.repository.transaction() as tx: tx.put('scope_blocks',f'{context.run_ref}:{context.scope}:{context.visit_id}',{'scope':context.scope,'plan_ref':context.plan_ref,'result_refs':context.result_refs},immutable=True)
        return NodeResult(execution_status='succeeded',coverage_status='blocked')
    async def park_group(self,context):
        wait=WaitRecord(wait_id=f'{context.activation_id}:work',activation_id=context.activation_id,thread_id=context.thread_id,scope=context.scope,plan_ref=context.plan_ref,graph_ref=context.graph_ref,version=1,aggregate_id=f'group:{context.run_ref}:{context.scope}',cursor=int(context.input_refs.get('event_cursor','0')),deadline=self.deadline)
        status=WaitService(self.repository).park(wait)
        return NodeResult(execution_status='succeeded',output={'wait_ref':wait.wait_id,'wait_status':status})
    async def publish_group(self,context):
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            receipt=tx.get('receipts',context.result_refs.get('T8',''))
            summary_ref=receipt['body']['result']['output'].get('summary_ref') if receipt else None
            record=tx.get('group_summaries',summary_ref) if summary_ref else None
            if not record: return blocked('INPUT_MISSING','exact T8 summary receipt required')
            summary=record['body']
            if (summary['scope'],summary['plan_ref'],summary['run_id'])!=(context.scope,context.plan_ref,context.run_ref):
                raise Conflict('group summary scope/version mismatch')
            published=tx.get('group_summary_publications',summary_ref)
            if published: return NodeResult(execution_status='succeeded',output=published['body'])
            children=[DeliveryManifest.model_validate(tx.get('work_deliveries',ref)['body']) for ref in summary['deliveries']]
            evidence={ref.artifact_id:ref for child in children for ref in child.evidence}
            gaps=[f'{gap["work_id"]}:{gap["status"]}:{gap["reason"]}' for gap in summary['gaps']]
            gaps.extend(f'{child.delivery_id}:{gap}' for child in children for gap in child.gaps)
            gaps.extend(f'{c["case_id"]}:{c["status"]}' for c in summary['coverage'] if c['status']!='completed')
            child_states=[child.result for child in children]
            data='insufficient' if summary['gaps'] or any(s.data_status in {'invalid','insufficient'} for s in child_states) else 'valid' if child_states and all(s.data_status=='valid' for s in child_states) else 'unverified'
            cleanup='unknown' if any(s.cleanup_status=='unknown' for s in child_states) else 'incomplete' if any(s.cleanup_status in {'pending','incomplete'} for s in child_states) else 'complete' if child_states else 'not_needed'
            unsupported=bool(summary['coverage']) and not children and all(c['status']=='unsupported' for c in summary['coverage'])
            performance='not_applicable' if unsupported else 'fail' if any(s.performance_status=='fail' for s in child_states) else 'pass' if child_states and all(s.performance_status=='pass' for s in child_states) else 'not_evaluated'
            group_identity=f'group:{context.run_ref}:{context.scope}'
            prior=tx.get('group_versions',group_identity)
            version=prior['body']['version']+1 if prior else 1
            plan=Plan.model_validate(tx.get('plans',context.plan_ref)['body'])
            manifest=DeliveryManifest(delivery_id=group_identity,version=version,scope=context.scope,plan_ref=context.plan_ref,contract_ref=plan.contract_ref,work_refs=tuple(summary['work_refs']),evidence=tuple(evidence.values()),result=NodeResult(execution_status='succeeded',data_status=data,cleanup_status=cleanup,performance_status=performance,coverage_status='unsupported' if unsupported else 'completed' if not gaps else 'blocked',output={'child_delivery_refs':list(summary['deliveries']),'coverage':summary['coverage']}),gaps=tuple(gaps))
            key=f'{group_identity}:{version}'
            tx.put('group_deliveries',key,manifest.model_dump(mode='json'),immutable=True)
            disposition='skipped' if unsupported else 'fulfilled' if not gaps and data=='valid' else 'blocked_closed'
            tx.put('group_dispositions',key,{'run_id':context.run_ref,'plan_ref':context.plan_ref,'scope':context.scope,'result':manifest.result.model_copy(update={'scope_disposition':disposition}).model_dump(mode='json'),'processing_ref':summary_ref,'gaps':gaps,'rule_ref':'terminal-review:1'},immutable=True)
            tx.put('group_delivery_sources',key,{'run_id':context.run_ref,'summary_ref':summary_ref,'child_delivery_refs':list(summary['deliveries']),'review_refs':summary['deliveries'],'work_refs':summary['work_refs']},immutable=True)
            tx.put('group_versions',group_identity,{'version':version,'delivery_ref':key})
            aggregate=f'run:{context.run_ref}'
            events=[e for e in tx.scan('events') if e['body']['aggregate_id']==aggregate]
            seq=max((e['body']['aggregate_seq'] for e in events),default=0)+1
            event=EventEnvelope(event_id=f'group-delivery:{key}',producer_scope=context.scope,producer_epoch=context.owner_epoch,aggregate_id=aggregate,aggregate_seq=seq,run_ref=context.run_ref,plan_ref=context.plan_ref,graph_ref=context.graph_ref,kind='group_delivery',payload={'summary_ref':summary_ref,'delivery_ref':key})
            EventService.append_in(tx,event,token_for(context))
            publication={'event_ref':event.event_id,'delivery_ref':key}
            tx.put('group_summary_publications',summary_ref,publication,immutable=True)
            closure=tx.get('closure_revisions',context.run_ref)
            tx.put('closure_revisions',context.run_ref,{'plan_ref':context.plan_ref,'revision':closure['body']['revision']+1 if closure else 1})
        return NodeResult(execution_status='succeeded',output=publication)
    async def restore_analysis_input(self,context):
        ref=context.input_refs.get('analysis_input_ref')
        with self.repository.transaction() as tx:
            row=tx.get('analysis_inputs',ref) if ref else None
            if not row or row['body']['plan_hash']!=context.plan_ref: return blocked('INPUT_MISSING','exact analysis input manifest required')
        return NodeResult(execution_status='succeeded',output={'analysis_input_ref':ref})
    async def validate_analysis_input(self,context):
        if self.review_service is None: return blocked('ADAPTER_MISSING','raw artifact validation service missing')
        try: self.review_service.resolve_analysis(context.input_refs['analysis_input_ref'])
        except (OSError,ValueError,PermissionError,KeyError): return blocked('EVIDENCE_INSUFFICIENT','frozen source or reuse projection unavailable')
        manifest=self.review_service.get_analysis(context.input_refs['analysis_input_ref'])
        with self.repository.transaction() as tx:
            for scope,(identity,version) in manifest['deliveries'].items():
                row=tx.get(manifest['delivery_kind'],f'{identity}:{version}')
                if not row: return blocked('EVIDENCE_INSUFFICIENT','frozen delivery missing')
                from ..domain.models import EvidenceRef
                for evidence in row['body']['evidence']:
                    try: self.review_service.artifacts.verify(EvidenceRef.model_validate(evidence))
                    except (OSError,ValueError): return blocked('EVIDENCE_INSUFFICIENT','frozen raw artifact unavailable')
        return NodeResult(execution_status='succeeded',output={'analysis_input_ref':context.input_refs['analysis_input_ref']})
    async def verify_report_facts(self,context):
        ref=context.input_refs.get('report_facts_ref')
        analysis=context.input_refs.get('analysis_ref')
        with self.repository.transaction() as tx:
            receipt=tx.get('receipts',context.result_refs.get('F1',''))
            if receipt:
                output=receipt['body']['result']['output']
                ref=output.get('facts_ref',ref)
                analysis=output.get('analysis_ref',analysis)
            row=tx.get('report_facts',ref) if ref else None
            if not row: return blocked('INPUT_MISSING','versioned report facts required')
            if row['body'].get('analysis_ref')!=analysis or (context.input_refs.get('analysis_ref') and analysis!=context.input_refs['analysis_ref']): return blocked('VERSION_CONFLICT','report facts bind a different analysis')
        return NodeResult(execution_status='succeeded',output={'facts_ref':ref})


def run_definition():
    nodes=('R0','R1','RP','R2','R3','R6','R7')
    routes={'R0':('R1',END),'R1':('RP',END),'RP':('R2',END),'R2':('R3',END),'R3':('R6','R7',END),'R6':('R7',END),'R7':(END,)}
    return GraphDefinition(graph_id='run',version='1',nodes=nodes,entry='R0',edges={},dynamic_routes=routes)

def run_routers():
    nexts={'R0':'R1','R1':'RP','RP':'R2','R2':'R3','R3':'R7','R6':'R7','R7':END}
    def router(node):
        def choose(state):
            summary=state['results'][node]
            return summary.get('continuation') or nexts[node] if summary['execution_status']=='succeeded' else END
        return choose
    return {node:router(node) for node in nexts}

def analysis_definition():
    nodes=('A1','A2','A3','A4','A5','A7')
    return GraphDefinition(graph_id='analysis',version='1',nodes=nodes,entry='A1',edges={},dynamic_routes={node:(nodes[i+1],END) if i+1<len(nodes) else (END,) for i,node in enumerate(nodes)})

def report_definition():
    nodes=('F1','F2','F4')
    return GraphDefinition(graph_id='report',version='1',nodes=nodes,entry='F1',edges={},dynamic_routes={'F1':('F2',END),'F2':('F4',END),'F4':(END,)})

def linear_routers(nodes):
    def router(node,target):
        return lambda state:target if state['results'][node]['execution_status']=='succeeded' else END
    return {node:router(node,nodes[i+1] if i+1<len(nodes) else END) for i,node in enumerate(nodes)}


def group_definition():
    nodes=('G0','E1','E2','C1','C2','C3','T0','T1','T2','T4','T5','T6','T8','TC','TB','GB','GW','GE')
    routes={'G0':('E1','T6','GB'),'E1':('E2','GB'),'E2':('T0','C1','T8','GB'),'C1':('C2','GB'),'C2':('C3','GB'),'C3':('T0','GB'),'T0':('T1','TC','TB'),'T1':('T2','TB'),'T2':('T4','T6','TC','TB'),'T4':('T5','GW'),'T5':('GW','T6'),'T6':('T8','TB'),'T8':('GE',),'TC':('T8',),'TB':('GB',),'GB':('GW',),'GW':(END,),'GE':(END,)}
    return GraphDefinition(graph_id='group',version='1',nodes=nodes,entry='G0',edges={},dynamic_routes=routes)

def group_routers():
    defaults={'G0':'E1','E1':'E2','E2':'T0','C1':'C2','C2':'C3','C3':'T0','T0':'T1','T1':'T2','T2':'T4','T4':'T5','T5':'GW','T6':'T8','T8':'GE','TC':'T8','TB':'GB','GB':'GW','GW':END,'GE':END}
    failures={'G0':'GB','E1':'GB','E2':'GB','C1':'GB','C2':'GB','C3':'GB','T0':'TB','T1':'TB','T2':'TB','T4':'GW','T5':'GW','T6':'TB','T8':'GE','TC':'T8','TB':'GB','GB':'GW','GW':END,'GE':END}
    def router(node):
        def choose(state):
            result=state['results'][node]
            return result.get('continuation') or (defaults[node] if result['execution_status']=='succeeded' else failures[node])
        return choose
    return {node:router(node) for node in defaults}
