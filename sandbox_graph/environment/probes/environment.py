"""Read-only remote environment inspection, with content-addressed raw evidence."""
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from ..contracts import EnvironmentReport,CheckObservation
from ..transport import SSHRequest
from ..declarations import _unique_json_object
from ..evidence import SecretRedactor,assert_public_payload_safe
from .access import AccessProbe
from ...domain.models import EvidenceRef


class RemoteEnvironmentProbe:
    def __init__(self,*,transport,resolver,store,scope,access=None,verification=None,timeout_seconds=20):
        self.transport=transport;self.resolver=resolver;self.store=store;self.scope=scope
        self.access=access or AccessProbe(transport=transport,resolver=resolver)
        self.verification=verification;self.timeout_seconds=timeout_seconds

    def commit(self,value):
        value=SecretRedactor().redact(value);assert_public_payload_safe(value)
        ref=self.store.commit([json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()],
            scope=self.scope,media_type='application/json',producer='environment-probe.v1')
        return 'artifact://'+ref.uri

    def resolve(self,reference):
        if not isinstance(reference,str) or not reference.startswith('artifact://'):return None
        suffix=reference[len('artifact://'):]
        if len(suffix.split('/'))!=2:return None
        scope,digest=suffix.split('/')
        if scope!=self.scope or len(digest)!=64 or set(digest)-set('0123456789abcdef'):return None
        manifest=self.store.root/scope/(digest+'.json')
        if not manifest.is_file() or manifest.resolve()!=manifest:return None
        ref=EvidenceRef.model_validate_json(manifest.read_bytes());self.store.verify(ref)
        return ref if ref.uri==suffix and ref.scope==self.scope else None

    async def request(self,node,action,data):
        result=await self.transport.request(SSHRequest(address=node.ssh_address,port=node.ssh_port,user=node.ssh_user,
            known_hosts=self.resolver.resolve(node.known_hosts_ref),identity_file=self.resolver.resolve(node.automation_credential_ref),
            payload={'action':action,'input':data},timeout_seconds=self.timeout_seconds))
        if result.returncode:raise OSError('remote probe failed')
        value=json.loads(result.stdout,object_pairs_hook=_unique_json_object)
        if not isinstance(value,dict):raise ValueError('remote probe returned no structured object')
        return value

    async def management_dependency(self,declaration,node,boot_id):
        """Observe the manager itself; group canary proves the associated API path.

        This does not assert that a particular manager process handled the
        request. Its identity, running service and startup config are observed
        independently, without inventing a compute registration or sandbox API.
        """
        proof={'node_id':node.node_id,'boot_id':boot_id,'roles':list(node.roles),
            'evidence_level':'group_dependency','status':'unavailable',
            'observed_at':datetime.now(timezone.utc).isoformat()}
        try:
            access=await self.access.check(node)
            if (access.identity_status!='confirmed' or access.execution_status!='allowed'
                    or (access.observation or {}).get('boot_id')!=boot_id):return proof
            product=await self.request(node,'probe.product.v1',{'product':declaration.product,
                'roles':list(node.roles),'paths':node.paths,'expected_hashes':{asset.name:asset.digest for asset in declaration.artifacts}})
            configs=await self.request(node,'probe.configuration.v1',{'product':declaration.product,
                'roles':list(node.roles),'paths':node.paths})
            proof.update(service_observations=product,configuration_observations=configs)
            components=configs.get('components',{})
            if (product.get('installation')=='installed' and components
                    and all(row.get('status')=='satisfied' for row in components.values())):
                proof['status']='verified'
        except (OSError,ValueError,KeyError,TypeError) as exc:proof['reason']=type(exc).__name__
        return proof

    async def check(self,declaration,node_ids,*,fresh):
        nodes={node.node_id:node for node in declaration.nodes}
        if not node_ids or not set(node_ids)<=nodes.keys():raise ValueError('probe requires declared node selection')
        now=datetime.now(timezone.utc);observed_at=now.isoformat().replace('+00:00','Z')
        observations=[];references=[];readiness={};limitations=[]
        def observe(node,component,check,status,observed,evidence,repair_code=None,boot_id=None):
            observations.append(CheckObservation(check_id=check,rule_ref='environment-probe.v1:'+check,
                node_id=node,component=component,status=status,observed=observed,observed_at=observed_at,
                evidence_refs=(evidence,),repair_code=repair_code,boot_id=boot_id))
        for node_id in node_ids:
            node=nodes[node_id];readiness[node_id]='blocked'
            access=await self.access.check(node);ref=self.commit({'node_id':node_id,'kind':'access','observed_at':observed_at,'result':asdict(access)})
            references.append(ref);boot=(access.observation or {}).get('boot_id')
            allowed=access.identity_status=='confirmed' and access.execution_status=='allowed'
            observe(node_id,'ssh','access','satisfied' if allowed else 'unknown',asdict(access),ref,boot_id=boot)
            if not allowed:continue
            host_ok=True;architecture=None
            for role in node.roles:
                try:
                    data=await self.request(node,'probe.host.v1',{'role':role})
                    architecture=data.get('facts',{}).get('architecture')
                    checks=data['role']['checks']
                    if not checks:raise ValueError('host probe returned no assertions')
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    data={'failure':type(exc).__name__};checks={'host':{'status':'unknown'}}
                ref=self.commit({'node_id':node_id,'kind':'host','role':role,'observed_at':observed_at,'result':data});references.append(ref)
                for key,item in checks.items():
                    status=item.get('status','unknown')
                    if status not in {'satisfied','not_applicable'}:host_ok=False
                    observe(node_id,role,'host:'+role+':'+key,status,item,ref,boot_id=boot)
            from ..catalog import ProfileCatalog
            try:
                profile=ProfileCatalog.standard().verify_declaration(declaration.model_copy(update={'nodes':(node,)}),
                    {node_id:architecture},mutation=False)
                source={'status':'satisfied','profile':profile.profile,'source_commits':dict(profile.source_commits)}
            except ValueError:
                source={'status':'unknown','reason':'architecture_or_audited_source_set_mismatch'};host_ok=False
            ref=self.commit({'node_id':node_id,'kind':'source_binding','observed_at':observed_at,'result':source});references.append(ref)
            observe(node_id,'product','source_binding',source['status'],source,ref,boot_id=boot)
            try:
                data=await self.request(node,'probe.product.v1',{'product':declaration.product,'roles':list(node.roles),
                    'paths':node.paths,'expected_hashes':{asset.name:asset.digest for asset in declaration.artifacts}})
                state=data['installation']
                if state not in {'installed','absent','partial','unknown'}:raise ValueError('invalid installation state')
            except (OSError,ValueError,KeyError,TypeError) as exc:data={'failure':type(exc).__name__};state='unknown'
            ref=self.commit({'node_id':node_id,'kind':'product','observed_at':observed_at,'result':data});references.append(ref)
            observe(node_id,'product','installation',{'installed':'satisfied','absent':'missing','partial':'mismatch','unknown':'unknown'}[state],
                data,ref,'PRODUCT_ABSENT' if state=='absent' else 'PRODUCT_PARTIAL' if state=='partial' else None,boot_id=boot)
            if state=='installed':
                if self.verification is None:
                    limitations.append(node_id+':effective_configuration_registration_authentication_functional_proof_required')
                    observe(node_id,'product','functional_readiness','unknown',{'reason':'independent_product_verification_required'},ref,boot_id=boot)
                else:
                    # The verifier performs product-specific read-only checks or
                    # consumes a separately authorized canary's raw receipts.
                    verified=await self.verification.check(declaration,node,fresh=fresh,boot_id=boot)
                    if not verified:raise ValueError('product verification returned no checks')
                    for item in verified:
                        item=CheckObservation.model_validate(item)
                        if item.node_id!=node_id or item.boot_id!=boot or not item.evidence_refs:
                            raise ValueError('verification proof node/boot/evidence binding mismatch')
                        observations.append(item);references.extend(item.evidence_refs)
                    if host_ok and all(row.status in {'satisfied','not_applicable'} for row in verified):readiness[node_id]='ready'
        return EnvironmentReport(declaration_hash=declaration.declaration_hash,source_set=declaration.source_set,
            profile=declaration.profile,observations=tuple(observations),readiness=readiness,
            limitations=tuple(limitations),artifact_manifest=tuple(sorted(set(references))),
            valid_until=(now+timedelta(minutes=5)).isoformat().replace('+00:00','Z'))
