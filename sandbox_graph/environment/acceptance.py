"""Publish immutable preparation evidence and revalidate it at T0."""
from datetime import datetime,timezone
import json
from .bindings import verify_acceptance,scope_fingerprint
from .contracts import EnvironmentAcceptance,EnvironmentDeclaration,EnvironmentReport
from .verification import ProductVerification
from ..domain.models import EvidenceRef,canonical_hash

PREFIX='environment-acceptance:'


def observed_fingerprint(report,node_ids):
    report=EnvironmentReport.model_validate(report)
    selected=[]
    for row in report.observations:
        if row.node_id not in node_ids:continue
        if row.check_id in {'functional_readiness','registration'}:continue
        value=row.observed
        if row.check_id=='access':
            value={'machine_id':value.get('observation',{}).get('machine_id'),'identity_status':value.get('identity_status')}
        selected.append({'node_id':row.node_id,'component':row.component,'check_id':row.check_id,
            'status':row.status,'boot_id':row.boot_id,'observed':value})
    return canonical_hash(sorted(selected,key=lambda x:(x['node_id'],x['component'],x['check_id'])))


def credentials(declaration):
    return {item.plane+':'+','.join(sorted(item.server_targets)):item.credential_version or item.credential_ref.rsplit('/',1)[-1]
        for item in declaration.auth}


class AcceptanceService:
    def __init__(self,repository,*,store):self.repository=repository;self.store=store

    def _raw(self,name):
        with self.repository.transaction() as tx:row=tx.get('environment_evidence',name)
        if not row:raise ValueError('environment evidence is missing')
        ref=EvidenceRef.model_validate(row['body']);self.store.verify(ref)
        if name!='artifact://'+ref.uri or ref.size>1024*1024:raise ValueError('environment evidence binding or size differs')
        return ref,self.store.path_for(ref).read_bytes()

    def issue(self,declaration,report,*,scope,node_ids,token):
        declaration=EnvironmentDeclaration.model_validate(declaration);report=EnvironmentReport.model_validate(report)
        nodes=tuple(sorted(set(node_ids)));now=datetime.now(timezone.utc)
        if (report.declaration_hash!=declaration.declaration_hash or report.source_set!=declaration.source_set
                or report.profile!=declaration.profile or any(report.readiness.get(node)!='ready' for node in nodes)
                or not report.valid_until):raise ValueError('ready scoped source-bound environment report required')
        functional=[];cleanup=[]
        verifier=ProductVerification(probe=None,repository=self.repository,scope=scope,store=self.store)
        declared={node.node_id:node for node in declaration.nodes}
        for node_id in nodes:
            boots={item.boot_id for item in report.observations if item.node_id==node_id}
            if len(boots)!=1 or None in boots:raise ValueError('current node boot identity missing or ambiguous')
            status,refs,_=verifier._proofs(declaration,declared[node_id],next(iter(boots)),now)
            if status!='satisfied':raise ValueError('current functional and cleanup proof required')
            for name in refs:
                _,raw=self._raw(name);kind=json.loads(raw)['kind']
                (functional if kind=='functional' else cleanup).append(name)
        report_ref=self.store.commit([report.model_dump_json().encode()],scope=scope,
            media_type='application/json',producer='environment-acceptance-report.v1')
        acceptance=EnvironmentAcceptance(report_manifest_ref='artifact://'+report_ref.uri,
            declaration_hash=declaration.declaration_hash,product=declaration.product,product_version=declaration.product_version,
            profile=declaration.profile,scope=nodes,credential_versions=credentials(declaration),valid_until=report.valid_until,
            invalidation_fingerprint=observed_fingerprint(report,nodes),
            functional_verification_refs=tuple(sorted(set(functional))),cleanup_refs=tuple(sorted(set(cleanup))))
        key=PREFIX+canonical_hash(acceptance)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            tx.put('environment_evidence','artifact://'+report_ref.uri,report_ref.model_dump(mode='json'),immutable=True)
            tx.put('environment_acceptances',key,{'acceptance':acceptance.model_dump(mode='json'),
                'declaration':declaration.model_dump(mode='json'),'evidence_scope':scope},immutable=True)
        return key

    def read(self,reference):
        if not reference.startswith(PREFIX):raise ValueError('versioned environment acceptance reference required')
        with self.repository.transaction() as tx:row=tx.get('environment_acceptances',reference)
        if not row or reference!=PREFIX+canonical_hash(row['body']['acceptance']):raise ValueError('environment acceptance identity missing or changed')
        return row['body']

    def verify(self,reference,*,current_declaration,current_report,node_ids):
        body=self.read(reference);current=EnvironmentDeclaration.model_validate(current_declaration)
        report=EnvironmentReport.model_validate(current_report)
        if (not report.valid_until or datetime.fromisoformat(report.valid_until.replace('Z','+00:00'))<=datetime.now(timezone.utc)
                or not set(node_ids)<=set(row.node_id for row in report.observations)
                or any(row.status not in {'satisfied','not_applicable'} for row in report.observations if row.node_id in node_ids)):
            raise ValueError('current environment report expired or incomplete')
        if (report.declaration_hash!=current.declaration_hash or report.source_set!=current.source_set
                or any(report.readiness.get(node)!='ready' for node in node_ids)):
            raise ValueError('current environment report failed')
        return verify_acceptance(body['acceptance'],frozen_declaration=body['declaration'],current_declaration=current,
            scope=node_ids,current_fingerprint=observed_fingerprint(report,node_ids),credential_versions=credentials(current),
            resolve_report=lambda name:json.loads(self._raw(name)[1]),resolve_evidence=self._raw,
            evidence_scope=body['evidence_scope'],trusted_producers=('environment-canary.v1',))


class EnvironmentAcceptanceGate:
    def __init__(self,service,*,probe_factory,declaration_reader=None):
        self.service=service;self.probe_factory=probe_factory;self.declaration_reader=declaration_reader

    async def check(self,context,cases):
        for reference in sorted({case.environment_ref for case in cases if case.environment_ref.startswith(PREFIX)}):
            body=self.service.read(reference);acceptance=EnvironmentAcceptance.model_validate(body['acceptance'])
            current=EnvironmentDeclaration.model_validate(self.declaration_reader(reference) if self.declaration_reader else body['declaration'])
            probe=self.probe_factory(body['evidence_scope'])
            report=await probe.check(current,acceptance.scope,fresh=True)
            self.service.verify(reference,current_declaration=current,current_report=report,node_ids=acceptance.scope)
