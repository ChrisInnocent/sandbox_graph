"""Scope-qualified safety for group/root nodes without synthetic WorkInstances."""
from dataclasses import asdict
from ..domain.models import canonical_hash
from ..domain.agent_lifecycle import ResourceDispositionReceipt
from ..persistence.repository import Conflict,StaleOwner


class ScopeSafety:
    PREFIX='lifecycle-scope:'

    @classmethod
    def key(cls,record):
        return cls.PREFIX+canonical_hash({'run_ref':record.key.run_ref,'kind':record.key.scope_kind,
            'scope_ref':record.key.scope_ref,'plan_ref':record.plan_ref,'graph_ref':record.graph_ref})

    @classmethod
    def register_in(cls,tx,record):
        key=cls.key(record)
        body={'scope':key,'scope_ref':record.key.scope_ref,'scope_kind':record.key.scope_kind,
            'run_ref':record.key.run_ref,'plan_ref':record.plan_ref,'graph_ref':record.graph_ref,
            'contract_ref':record.contract_ref,'work_revision':0}
        tx.put('lifecycle_scope_identities',key,body,immutable=True)
        return key

    @classmethod
    def resolve_in(cls,tx,scope,token):
        from .agent_lifecycle import LifecycleService
        owned=[]
        for owner in tx.scan('lifecycle_owners'):
            if owner['body']!=LifecycleService._owner_body(token):continue
            life=LifecycleService.load_in(tx,owner['key'])
            if life.key.scope_kind!='work' and (life.key.scope_ref==scope or cls.key(life)==scope):owned.append(cls.register_in(tx,life))
        if not owned and tx.get('work_instances',scope):return scope
        if len(set(owned))!=1:raise Conflict('exact lifecycle owner and typed non-work scope required')
        return owned[0]

    @classmethod
    def snapshot_in(cls,tx,key):
        from .lifecycle_inventory import LifecycleInventory
        from ..runtime.tasks import business_job_kind
        identity=tx.get('lifecycle_scope_identities',key)['body']
        sources=[];closed=True;lives=set()
        for row in tx.scan('agent_lifecycles'):
            body=row['body'];scope=body['key']
            if (scope['run_ref'],scope['scope_ref'],scope['scope_kind'],body['plan_ref'],body['graph_ref'])!=(
                    identity['run_ref'],identity['scope_ref'],identity['scope_kind'],identity['plan_ref'],identity['graph_ref']):continue
            lives.add(row['key'])
            receipt=tx.get('receipts',body['source_receipt'])
            activation=tx.get('activations',receipt['body']['identity']['activation_id']) if receipt else None
            source=activation['body'] if activation else {}
            exact=receipt['body']['identity'] if receipt else {}
            valid=(source.get('status')=='complete' and all(source.get('record',{}).get(k)==exact.get(v)
                for k,v in (('activation_id','activation_id'),('run_id','run_ref'),('scope','scope'),('plan_ref','plan_ref'),('graph_ref','graph_ref')))
                and source.get('result',{}).get('result_refs',{}).get(scope['node_id'])==body['source_receipt'])
            closed &= valid
            sources.append({'receipt':receipt,'activation':activation})
        excluded=[row['key'] for row in tx.scan('jobs') if row['body'].get('payload',{}).get('lifecycle_id') in lives
            and business_job_kind(row['body']['job_kind']) in {'lifecycle','candidate','agent_team','supervision'}]
        run=tx.get('runs',identity['run_ref'])
        extra=(identity['run_ref'],) if run and tx.get('environment_plans',run['body']['active_plan_ref']) else ()
        inventory=LifecycleInventory.collect_in(tx,run_ref=identity['run_ref'],scope_kind=identity['scope_kind'],
            scope_ref=identity['scope_ref'],extra_scopes=extra,exclude_jobs=excluded,include_publication=True)
        items=[]
        for item in inventory.items:
            if item.kind=='work_execution_leases' and item.key==key:continue
            # A budgeted read-only investigator is control work, not site activity.
            if item.kind=='agent_provider_requests':
                envelope=tx.get(item.kind,item.key)['body'].get('envelope',{})
                if envelope.get('lifecycle_id') in lives and envelope.get('scope',{}).get('kind')!='candidate':continue
            items.append(asdict(item))
        return {'identity':identity,'sources':sources,'attempt_closed':bool(sources) and closed,'inventory':items}

    @classmethod
    def produce_in(cls,tx,key,token,ttl):
        snapshot=cls.snapshot_in(tx,key);items=snapshot['inventory']
        blockers=[x for x in items if not x['closed'] or x['ownership']!='owned']
        receipt=ResourceDispositionReceipt(schema_version=1,work_ref=key,
            operation_refs=tuple(x['key'] for x in items if x['kind']=='operations'),
            stopped=snapshot['attempt_closed'] and not blockers,
            admission_status='closed' if snapshot['attempt_closed'] else 'open',sampler_status='unknown' if any(x['kind']=='observations' for x in blockers) else 'not_started',
            owned_resources=tuple(x['key'] for x in items if x['kind']=='resources'),
            residual_resources=tuple(x['key'] for x in blockers if x['kind']=='resources'),
            isolation_domains=tuple(x['key'] for x in blockers if x['kind'] in {'resource_leases','repair_isolations','candidate_isolations'}),
            observed_at=float(tx.now),expires_at=float(tx.now+ttl),clock='wall',producer='scope_safety',producer_version='1')
        body={'schema_version':2,'identity':snapshot['identity'],'producer':{'thread_id':token.thread_id,
            'owner_epoch':token.owner_epoch,'lease_id':token.lease_id},'snapshot_hash':canonical_hash(snapshot),
            'receipt':receipt.model_dump(mode='json')}
        ref='disposition:'+canonical_hash(body);tx.put('resource_disposition_receipts',ref,body,immutable=True);return ref

    @classmethod
    def verify_in(cls,tx,ref,key,token,*,require_closed):
        from .agent_lifecycle import decode
        row=tx.get('resource_disposition_receipts',ref)
        if not row or row['body'].get('schema_version')!=2 or ref!='disposition:'+canonical_hash(row['body']):raise Conflict('scope proof identity invalid')
        body=row['body'];snapshot=cls.snapshot_in(tx,key)
        if (body['identity']!=snapshot['identity'] or body['snapshot_hash']!=canonical_hash(snapshot)
                or body['producer']!={'thread_id':token.thread_id,'owner_epoch':token.owner_epoch,'lease_id':token.lease_id}):
            raise Conflict('scope proof ownership or underlying facts changed')
        proof=decode(ResourceDispositionReceipt,body['receipt'])
        if not proof.observed_at<=tx.now<proof.expires_at or proof.work_ref!=key:raise Conflict('scope proof expired or scope differs')
        if require_closed and (not proof.stopped or proof.admission_status!='closed' or proof.residual_resources or proof.isolation_domains):
            raise Conflict('scope effects are not conclusively closed; reconcile original operations')
        return proof
