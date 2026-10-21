"""Resource reservations outlive driver expiry until explicit reconciliation."""
import math
from uuid import uuid4
from ..persistence.repository import Conflict,positive_duration

class LeaseService:
    def __init__(self, repository): self.repository=repository
    def acquire_thread(self, thread_id, owner, ttl):
        positive_duration(ttl)
        with self.repository.transaction() as tx: return tx.acquire_thread(thread_id,owner,ttl)
    def renew(self, token, ttl):
        positive_duration(ttl)
        with self.repository.transaction() as tx: return tx.renew_thread(token,ttl)
    def acquire_all(self, domains, *, scope, token,owner_run_id=None):
        domains=tuple(sorted(set(domains)))
        if not domains: raise ValueError('canonical conflict domains required')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            for domain in domains:
                old=tx.get('resource_leases',domain)
                if old and (old['body']['scope']!=scope or old['body']['status']=='quarantine' or (owner_run_id is not None and old['body'].get('run_id')!=owner_run_id)):
                    raise Conflict(f'conflict domain unavailable: {domain}')
            for domain in domains:
                tx.put('resource_leases',domain,{'scope':scope,'thread_id':token.thread_id,'owner_epoch':token.owner_epoch,'status':'reserved'}|({'run_id':owner_run_id} if owner_run_id is not None else {}))
        return domains
    def quarantine(self, domains, *, scope, reason, token):
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            for domain in sorted(set(domains)):
                old=tx.get('resource_leases',domain)
                if not old or old['body']['scope']!=scope: raise Conflict('cannot quarantine another scope')
                tx.put('resource_leases',domain,old['body']|{'status':'quarantine','reason':reason})
    @staticmethod
    def announce_release(tx,domains,scope,owner_epoch):
        tx.put("outbox",f"resource-release:{uuid4()}",{"kind":"resource_domains_available","domains":sorted(domains),"scope":scope,"owner_epoch":owner_epoch},immutable=True)
    def release(self, domains, *, scope, token, closed,operation_refs=(),owner_run_id=None):
        if not closed: raise Conflict('resource closure must be verified before release')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            for identity in operation_refs:
                operation=tx.get('operations',identity)
                if not operation or operation['body']['scope']!=scope or operation['body']['phase']!='terminal' or operation['body']['status']['state']!='completed' or (owner_run_id is not None and operation['body']['run_id']!=owner_run_id):
                    raise Conflict('configuration operation not conclusively completed')
            for domain in sorted(set(domains)):
                old=tx.get('resource_leases',domain)
                if not old or old['body']['scope']!=scope or (owner_run_id is not None and old['body'].get('run_id')!=owner_run_id): raise Conflict('cannot release another scope')
                tx.delete('resource_leases',domain)
            self.announce_release(tx,domains,scope,token.owner_epoch)
