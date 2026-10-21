"""Single-owner read-only sampler with a bounded, durable SQLite evidence spool.

The persistent DB handle is held for the sampler lifetime. Process files are
reopened each sample so a persistent descriptor cannot silently follow a dead
process. A remote sink must be idempotent by sample_id (acknowledgement can be
lost). safe_to_submit is an explicit guard for the load runner after sink/storage
failure; this module itself never creates or changes sandbox resources.
"""
import fcntl
from functools import wraps
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from .resources import deduplicate_inventory, sample_process


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _serialized(function):
    @wraps(function)
    def call(self,*args,**kwargs):
        with self._mutex:
            return function(self,*args,**kwargs)
    return call


class ResourceSampler:
    def __init__(self,inventory,spool_path,*,proc_root='/proc',max_pending=256,
                 clock_ticks=None,clock_domain,run_id,clock=time.monotonic):
        if type(max_pending) is not int or max_pending<=0 or not run_id or not clock_domain:
            raise ValueError('finite capacity, run ID, and clock domain required')
        self.inventory=deduplicate_inventory(inventory)
        if not self.inventory or any(not r.get('roles') or not r.get('components') for r in self.inventory):
            raise ValueError('explicit process inventory with roles/components required')
        domains={(r['identity']['environment'],r['identity']['node'],r['identity']['boot_id']) for r in self.inventory}
        if len(domains)!=1:
            raise ValueError('one sampler must use a single host/boot inventory')
        self._inventory_domain=next(iter(domains))
        self.proc_root=Path(proc_root);self.clock_ticks=clock_ticks
        self.clock_domain=clock_domain;self.run_id=run_id;self.max_pending=max_pending
        self.clock=clock;self._safe=True;self._closed=False
        self._mutex=threading.RLock()
        path=Path(spool_path);path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=path.with_suffix(path.suffix+'.lock').open('a+')
        try:
            fcntl.flock(self._lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.db=sqlite3.connect(path,check_same_thread=False)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS samples (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            identity={'inventory':self.inventory,'clock_domain':clock_domain,'run_id':run_id,
                      'clock_ticks':clock_ticks,'max_pending':max_pending,'proc_root':str(self.proc_root.resolve())}
            fingerprint=hashlib.sha256(_json(identity).encode()).hexdigest()
            with self.db:
                old=self._get('identity')
                if old is None:
                    self._set('identity',fingerprint);self._set('next_seq',1);self._set('connected',True)
                elif old!=fingerprint:
                    raise ValueError('sampler identity/configuration differs from persisted spool')
            self._safe=self._get('connected')
            current=self._get('current_inventory')
            if current is None:
                current={'processes':self.inventory,'version':1,'source_ref':'initial_inventory:'+fingerprint,
                         'cohort_ids':sorted({p['sandbox_id'] for p in self.inventory if p.get('sandbox_id')})}
                with self.db:self._set('current_inventory',current);self._set('inventory_valid',True)
            self.inventory=current['processes'];self._inventory_record=current
            self._inventory_valid=self._get('inventory_valid') is not False
        except BaseException:
            if hasattr(self,'db'):
                self.db.close()
            self._lock.close()
            raise

    def _get(self,key):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def _set(self,key,value):
        self.db.execute('INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,_json(value)))

    @property
    def safe_to_submit(self):
        return self._safe and self._inventory_valid and not self._closed

    @_serialized
    def update_inventory(self,snapshot,source_ref,*,cohort_ids=None):
        """Bind an explicit collector snapshot; no process discovery is performed."""
        try:
            if not isinstance(snapshot,list) or not isinstance(source_ref,str) or not source_ref:
                raise ValueError('explicit inventory snapshot and source reference required')
            processes=deduplicate_inventory(snapshot)
            for process in processes:
                identity=process['identity']
                if tuple(identity[key] for key in ('environment','node','boot_id'))!=self._inventory_domain:
                    raise ValueError('inventory environment/node/boot changed')
                if (type(identity['pid']) is not int or identity['pid']<=0 or
                        type(identity['start_ticks']) is not int or identity['start_ticks']<0 or
                        not process['roles'] or not process['components']):
                    raise ValueError('invalid process identity or attribution')
                if process.get('sandbox_id') and not process.get('generation'):
                    raise ValueError('sandbox inventory needs generation evidence')
            mapped={p['sandbox_id'] for p in processes if p.get('sandbox_id')}
            cohort=sorted(mapped) if cohort_ids is None else sorted(cohort_ids)
            if (any(not isinstance(sid,str) or not sid for sid in cohort)
                    or len(set(cohort))!=len(cohort) or not mapped<=set(cohort)):
                raise ValueError('declared cohort must contain mapped sandbox identities')
            current={'processes':processes,'source_ref':source_ref,'cohort_ids':cohort}
            old={key:value for key,value in self._inventory_record.items() if key!='version'}
            current['version']=self._inventory_record['version']+(current!=old)
            with self.db:self._set('current_inventory',current);self._set('inventory_valid',True)
            self.inventory=processes;self._inventory_record=current;self._inventory_valid=True
            return current['version']
        except Exception:
            self._inventory_valid=False
            with self.db:self._set('inventory_valid',False)
            raise

    def _record_gap(self,reason):
        with self.db:
            seq=self._get('next_seq');self._set('next_seq',seq+1)
            gap={'kind':'sample_gap','seq':seq,'through_seq':seq,'sample_id':f'{self.run_id}:{seq}',
                 'reason':reason,'dropped_count':1,'started_s':self.clock(),
                 'clock_domain':self.clock_domain,'inventory_version':self._inventory_record['version'],
                 'inventory_source_ref':self._inventory_record['source_ref']}
            overflow=self._get('overflow')
            if overflow is not None or self.db.execute('SELECT count(*) FROM samples').fetchone()[0]>=self.max_pending:
                if overflow is not None:
                    overflow.update(through_seq=seq,dropped_count=overflow['dropped_count']+1,
                                    reasons=sorted(set(overflow.get('reasons',[overflow['reason']])+[reason])))
                    gap=overflow
                self._set('overflow',gap)
            else:self.db.execute('INSERT INTO samples VALUES (?,?)',(seq,_json(gap)))
        return gap

    @_serialized
    def invalidate_observation(self,reason):
        """Persist one missing observation and stop admission until a fresh proof."""
        if not isinstance(reason,str) or not reason or len(reason)>256:raise ValueError('bounded gap reason required')
        self._safe=False;self._inventory_valid=False
        with self.db:self._set('connected',False);self._set('inventory_valid',False)
        return self._record_gap(reason)

    @_serialized
    def sample_once(self):
        if not self._inventory_valid:return self._record_gap('inventory_not_verified')
        started=self.clock();cpu_start=time.thread_time()
        try:
            with self.db:
                seq=self._get('next_seq');self._set('next_seq',seq+1)
                count=self.db.execute('SELECT count(*) FROM samples').fetchone()[0]
                overflow=self._get('overflow')
                if count>=self.max_pending or overflow is not None:
                    gap=overflow or {'kind':'sample_gap','seq':seq,'sample_id':f'{self.run_id}:{seq}',
                                     'started_s':started,'reason':'spool_capacity','dropped_count':0,
                                     'clock_domain':self.clock_domain}
                    gap.update(through_seq=seq,ended_s=self.clock(),dropped_count=gap['dropped_count']+1)
                    self._set('overflow',gap);self._safe=False;self._set('connected',False)
                    return gap
                placeholder={'kind':'sample_gap','seq':seq,'sample_id':f'{self.run_id}:{seq}',
                             'through_seq':seq,'reason':'interrupted_sampling','dropped_count':1,
                             'started_s':started,'clock_domain':self.clock_domain}
                self.db.execute('INSERT INTO samples VALUES (?,?)',(seq,_json(placeholder)))
            processes=[]
            for item in self.inventory:
                identity=item['identity']
                sample=sample_process(self.proc_root,identity['pid'],environment=identity['environment'],
                                      node=identity['node'],clock=self.clock,clock_ticks=self.clock_ticks)
                if sample['identity']!=identity:
                    sample.update(status='unavailable',reason='inventory_identity_mismatch',
                                  pss_bytes=None,cpu_seconds=None)
                processes.append({**sample,'expected_identity':identity,'roles':item['roles'],
                                  'components':item['components'],'sandbox_id':item.get('sandbox_id'),
                                  'generation':item.get('generation')})
            ended=self.clock()
            row={'schema_version':'resource_sample.v1','kind':'resource_sample','seq':seq,
                 'sample_id':f'{self.run_id}:{seq}','clock_domain':self.clock_domain,
                 'started_s':started,'ended_s':ended,'t_s':ended,'processes':processes,
                 'sampler_wall_seconds':ended-started,'sampler_cpu_seconds':time.thread_time()-cpu_start,
                 'poll_requests':0,'renewal_requests':0}
            row.update(inventory_version=self._inventory_record['version'],
                       inventory_source_ref=self._inventory_record['source_ref'],
                       snapshot_cohort_ids=self._inventory_record['cohort_ids'],
                       inventory_complete={p['sandbox_id'] for p in processes if p.get('sandbox_id')}==set(self._inventory_record['cohort_ids']))
            with self.db:
                self.db.execute('UPDATE samples SET payload=? WHERE seq=?',(_json(row),seq))
            return row
        except (sqlite3.Error,OSError):
            self._safe=False
            raise

    @_serialized
    def pending(self):
        rows=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM samples ORDER BY seq')]
        overflow=self._get('overflow')
        if overflow is not None:
            rows.append(overflow)
        return sorted(rows,key=lambda row:row['seq'])

    @_serialized
    def reconcile(self,sink):
        """Replay durable IDs in order; acknowledgement removes only that record."""
        count=0
        for row in self.pending():
            try:
                if sink(row) is False:
                    raise ConnectionError('sink did not acknowledge sample')
            except Exception as exc:
                self._safe=False
                with self.db:
                    self._set('connected',False)
                return {'status':'disconnected','acknowledged':count,'reason':type(exc).__name__,
                        'pending':len(self.pending())}
            with self.db:
                overflow=self._get('overflow')
                if overflow is not None and row['seq']==overflow['seq']:
                    self._set('overflow',None)
                else:
                    self.db.execute('DELETE FROM samples WHERE seq=?',(row['seq'],))
            count+=1
        with self.db:
            self._set('connected',True)
        self._safe=True
        return {'status':'reconciled','acknowledged':count,'pending':0}

    @_serialized
    def close(self):
        if not self._closed:
            self.db.close();self._lock.close();self._closed=True

    def __enter__(self):
        return self

    def __exit__(self,*exc):
        self.close()
