"""Analysis/report graph handlers over exact frozen, verified deliveries.

Only immutable references enter the graph. Files are materialized privately from
the artifact store; a caller-supplied path or a `latest` alias is never consulted.
"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import tempfile
from uuid import uuid4

from sandbox_graph.domain.models import DeliveryManifest, EvidenceRef, ErrorRecord, NodeResult, canonical_hash
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.tasks import JobSpec, TaskBackend
from sandbox_graph.services.closure import ClosureService
from sandbox_graph.services.workflows import blocked, token_for
from .analysis import verify_case, recompute_case
from .comparison import compare_cases, export_comparison


class AnalysisReportServices:
    def __init__(self, repository, reviews, artifact_store, *, deadline):
        self.repository = repository
        self.reviews = reviews
        self.artifacts = artifact_store
        self.deadline = deadline

    def mapping(self):
        return {'A3': self.calculate, 'A4': self.export, 'A7': self.publish,
                'F1': self.report, 'F4': self.archive}

    def _read(self, ref):
        self.artifacts.verify(ref)
        return self.artifacts.path_for(ref).read_bytes()

    def _commit(self, data, scope):
        # Content-addressed metadata must be identical for identical bytes.
        return self.artifacts.commit((data,), scope=scope, media_type='application/octet-stream',
                                     producer='benchmark.session.v1')

    def _materialize(self, input_ref, plan_ref, destination, *, run_id=None, alias_sink=None):
        resolved = self.reviews.resolve_analysis(input_ref)
        if resolved['manifest']['plan_hash'] != plan_ref:
            raise Conflict('frozen analysis belongs to another plan')
        paths, expected, gaps = [], {}, list(resolved['manifest']['missing_scope'])
        samples = {}
        for group, record in sorted(resolved['groups'].items()):
            if run_id is not None and record['source'] and record['source']['run_id'] != run_id:
                raise Conflict('frozen group belongs to another run; accepted reuse delivery required')
            if record['review']['decision'] != 'accepted':
                gaps.append(group + ':review_rejected')
            for review in record['work_reviews']:
                if review['decision'] != 'accepted':
                    gaps.append(review['delivery_id'] + ':review_rejected')
            gaps.extend(record['delivery']['gaps'])
            for measurement in record['measurement_sources']:
                logical = DeliveryManifest.model_validate(measurement['logical_delivery'])
                delivery = DeliveryManifest.model_validate(measurement['source_delivery'])
                projection = measurement['projection']
                if logical.plan_ref != plan_ref:
                    raise Conflict('logical delivery belongs to another plan')
                if projection and (projection['target_plan_ref'] != plan_ref
                        or (run_id is not None and projection['target_run_id'] != run_id)
                        or projection['target_scope'] != logical.scope
                        or tuple(logical.work_refs) != (projection['target_work_id'],)
                        or projection['source_plan_ref'] != delivery.plan_ref
                        or projection['source_contract_ref'] != delivery.contract_ref):
                    raise Conflict('reuse projection does not bind selected work and original measurement')
                source_ref = f'{delivery.delivery_id}:{delivery.version}'
                refs = {ref.artifact_id: ref for ref in delivery.evidence}
                identity = delivery.result.output.get('case_index_ref')
                if not identity:
                    gaps.append(logical.delivery_id + ':case_index_missing')
                    continue
                if identity not in refs:
                    raise ValueError('case index absent from frozen delivery evidence')
                index = json.loads(self._read(refs[identity]))
                case_id = index['case_id']
                if case_id not in delivery.work_refs:
                    raise ValueError('case identity mismatch')
                sample_key = (source_ref, index['case_hash'])
                if case_id in expected and samples.get(sample_key) != case_id:
                    raise ValueError('duplicate case identity from different measurement deliveries')
                if alias_sink is not None:
                    for work in logical.work_refs:
                        alias = {'case_id': case_id, 'source_delivery_ref': source_ref,
                                 'projection_ref': projection['projection_ref'] if projection else None}
                        if work in alias_sink and alias_sink[work] != alias:
                            raise Conflict('selected work maps to conflicting measured evidence')
                        alias_sink[work] = alias
                if sample_key in samples:
                    # resolve_analysis revalidates every projection and source artifact.
                    # Multiple logical references preserve coverage but count one sample set.
                    continue
                folder = destination / canonical_hash([source_ref, case_id])
                folder.mkdir()
                for name, raw_ref in index['paths'].items():
                    path = PurePosixPath(name)
                    if path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name:
                        raise ValueError('unsafe evidence path')
                    ref = EvidenceRef.model_validate(raw_ref)
                    if refs.get(ref.artifact_id) != ref or ref.scope != delivery.scope:
                        raise ValueError('index references evidence outside frozen delivery')
                    target = folder / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self._read(ref))
                manifest = verify_case(folder)
                if (manifest['case_id'] != case_id or manifest['metadata'].get('plan_ref') != delivery.plan_ref
                        or manifest['metadata'].get('case_hash') != index['case_hash']
                        or index['case_hash'] != delivery.result.output.get('case_hash')
                        or manifest['metadata'].get('contract_ref') != delivery.contract_ref):
                    raise ValueError('case seal does not bind frozen delivery')
                recompute_case(folder)
                expected[case_id] = index['paths']['case_manifest.json']['digest']
                samples[sample_key] = case_id
                paths.append(folder)
        return paths, expected, sorted(set(gaps))

    def _receipt_output(self, context, node):
        with self.repository.transaction() as tx:
            row = tx.get('receipts', context.result_refs.get(node, ''))
            if not row:
                raise Conflict(f'exact {node} receipt required')
            body = row['body']
            identity = body['identity']
            if any(identity[key] != getattr(context, key) for key in
                   ('run_ref', 'plan_ref', 'activation_id', 'thread_id', 'scope', 'graph_ref')) or identity['node_id'] != node:
                raise Conflict('receipt belongs to another activation')
            return body['result']['output']

    async def calculate(self, context):
        input_ref = context.input_refs['analysis_input_ref']
        aliases = {}
        with tempfile.TemporaryDirectory(prefix='sandbox-analysis-') as directory:
            paths, expected, gaps = self._materialize(input_ref, context.plan_ref, Path(directory), run_id=context.run_ref, alias_sink=aliases)
            facts = compare_cases(paths, analysis_input_ref=input_ref, expected_manifests=expected, case_aliases=aliases)
        # 结果/报告合理性审视（归属 agent 职责）：只出 findings，不改事实。
        from sandbox_graph.services.quality_review import review_report_facts
        review_findings = review_report_facts(facts, expected_case_ids=set(expected))
        ref = self._commit(json.dumps(facts, sort_keys=True, allow_nan=False).encode(), context.scope)
        record = {'analysis_input_ref': input_ref, 'plan_ref': context.plan_ref,
                  'run_id': context.run_ref, 'facts_ref': ref.model_dump(mode='json'),
                  'expected_manifests': expected, 'gaps': gaps, 'case_aliases': aliases,
                  'review_findings': review_findings}
        identity = canonical_hash(record)
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            tx.put('analysis_results', identity, record, immutable=True)
        return NodeResult(execution_status='succeeded', artifact_refs=(ref.artifact_id,),
                          data_status='insufficient' if gaps or not expected else 'unverified',
                          output={'analysis_ref': identity, 'analysis_input_ref': input_ref,
                                  'review_findings': review_findings})

    async def export(self, context):
        identity = self._receipt_output(context, 'A3')['analysis_ref']
        with self.repository.transaction() as tx:
            record = tx.get('analysis_results', identity)['body']
        with tempfile.TemporaryDirectory(prefix='sandbox-export-') as directory:
            root = Path(directory)
            aliases = {}
            paths, expected, gaps = self._materialize(record['analysis_input_ref'], context.plan_ref, root, run_id=context.run_ref, alias_sink=aliases)
            if expected != record['expected_manifests'] or gaps != record['gaps'] or aliases != record['case_aliases']:
                raise Conflict('analysis inputs changed during export')
            export_comparison(paths, root / 'export', xlsx=True,
                              analysis_input_ref=record['analysis_input_ref'], expected_manifests=expected, case_aliases=aliases)
            facts = json.loads((root / 'export/facts.json').read_bytes())
            if facts != json.loads(self._read(EvidenceRef.model_validate(record['facts_ref']))):
                raise Conflict('export and calculated facts disagree')
            refs = {p.name: self._commit(p.read_bytes(), context.scope).model_dump(mode='json')
                    for p in (root / 'export').iterdir()}
        body = {'analysis_ref': identity, 'analysis_input_ref': record['analysis_input_ref'],
                'files': refs}
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            tx.put('analysis_exports', identity, body, immutable=True)
        return NodeResult(execution_status='succeeded',
                          artifact_refs=tuple(r['artifact_id'] for r in refs.values()),
                          output={'analysis_ref': identity})

    async def publish(self, context):
        identity = self._receipt_output(context, 'A4')['analysis_ref']
        facts_ref = canonical_hash({'analysis_ref': identity, 'report_version': 1})
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            export = tx.get('analysis_exports', identity)
            if not export or export['body']['analysis_input_ref'] != context.input_refs['analysis_input_ref']:
                raise Conflict('exact exported analysis required')
            dedupe = f'report:{context.run_ref}:{identity}'
            old = tx.get('job_dedupe', dedupe)
            if old:
                job_id = old['body']['job_id']
            else:
                run = tx.get('runs', context.run_ref)['body']
                job_id = TaskBackend.enqueue_in(tx, JobSpec(
                    job_id=str(uuid4()), job_kind='report', scope_ref=context.scope,
                    thread_id=f'{context.run_ref}:report:{identity}', activation_id=str(uuid4()),
                    dedupe_key=dedupe, due_at=tx.now, deadline=self.deadline,
                    run_id=context.run_ref, dispatch_generation=run['dispatch_generation'],
                    payload={'analysis_ref': identity, 'report_facts_ref': facts_ref,
                             'analysis_input_ref': context.input_refs['analysis_input_ref'],
                             'plan_ref': context.plan_ref, 'graph_ref': context.graph_ref}))
        return NodeResult(execution_status='succeeded',
                          output={'analysis_ref': identity, 'report_job_ref': job_id})

    async def report(self, context):
        identity = context.input_refs['analysis_ref']
        facts_ref = context.input_refs['report_facts_ref']
        if facts_ref != canonical_hash({'analysis_ref': identity, 'report_version': 1}):
            raise Conflict('report facts identity mismatch')
        with self.repository.transaction() as tx:
            record = tx.get('analysis_results', identity)['body']
            export = tx.get('analysis_exports', identity)['body']
        if (record['run_id'] != context.run_ref or record['plan_ref'] != context.plan_ref
                or record['analysis_input_ref'] != context.input_refs['analysis_input_ref']):
            raise Conflict('report scope/version mismatch')
        for ref in export['files'].values():
            self.artifacts.verify(EvidenceRef.model_validate(ref))
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            tx.put('report_facts', facts_ref, {'analysis_ref': identity,
                   'analysis_input_ref': record['analysis_input_ref'], 'files': export['files'],
                   'gaps': record['gaps'], 'interpretation_status': 'not_requested'}, immutable=True)
        return NodeResult(execution_status='succeeded',
                          artifact_refs=tuple(ref['artifact_id'] for _,ref in sorted(export['files'].items())),
                          output={'report_facts_ref': facts_ref, 'analysis_ref': identity,
                                  'files': export['files'],
                                  'excel_ref': export['files'].get('facts.xlsx',{}).get('artifact_id')})

    async def archive(self, context):
        identity = context.input_refs['analysis_ref']
        frozen = self.reviews.resolve_analysis(context.input_refs['analysis_input_ref'])
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            record = tx.get('analysis_results', identity)['body']
            export = tx.get('analysis_exports', identity)['body']
            report = tx.get('report_facts', context.input_refs['report_facts_ref'])['body']
            if record['analysis_input_ref'] != context.input_refs['analysis_input_ref']:
                raise Conflict('archive frozen input differs from calculated analysis')
            revision = tx.get('closure_revisions', context.run_ref)
            revision = revision['body']['revision'] if revision else 0
            run = tx.get('runs', context.run_ref)['body']
            if run['active_plan_ref'] != context.plan_ref or record['run_id'] != context.run_ref:
                return blocked('VERSION_CONFLICT', 'archive plan is no longer active')
            # A newer delivery/review must produce a new analysis before closure.
            current = tx.get('analysis_dirty', context.plan_ref)
            frozen_ref = current['body'].get('input_ref') if current else None
            if not current or current['body'].get('dirty') or frozen_ref != record['analysis_input_ref']:
                # issue #2 P1：若有更新且已完成的分析，本报告被取代（不终态阻塞）；
                # 否则给出**有界可重试**的显式缺口（reconcile），让新分析完成后重跑 F4。
                newer = frozen_ref is not None and frozen_ref != record['analysis_input_ref'] and any(
                    r['key'] != identity and r['body'].get('plan_ref') == context.plan_ref
                    and r['body'].get('analysis_input_ref') == frozen_ref
                    for r in tx.scan('analysis_results'))
                if newer:
                    return NodeResult(execution_status='cancelled',
                        output={'superseded_by_input_ref': frozen_ref,
                                'analysis_input_ref': record['analysis_input_ref']})
                return NodeResult(execution_status='blocked', coverage_status='blocked',
                    error=ErrorRecord(code='ANALYSIS_STALE', retry_class='reconcile', observed_at=0,
                        message='newer frozen analysis input requires re-analysis before closure'),
                    output={'analysis_input_ref': record['analysis_input_ref'],
                            'frozen_input_ref': frozen_ref, 'reanalysis_required': True,
                            'analysis_dirty': bool(current and current['body'].get('dirty'))})
            work = [r['body'] for r in tx.scan('work_instances')
                    if r['body']['run_id'] == context.run_ref and r['body']['plan_ref'] == context.plan_ref]
            scopes = {}
            for item in work:
                disposition = tx.get('scope_dispositions', item['work_id'])
                if (not disposition or disposition['body'].get('plan_ref') != context.plan_ref
                        or disposition['body'].get('run_id') != context.run_ref):
                    return blocked('DISPOSITION_MISSING', 'every selected work requires an explicit final disposition')
                body = disposition['body']
                scopes[item['work_id']] = (NodeResult.model_validate(body['result']), body['processing_ref'])
            if frozen['manifest']['missing_scope']:
                return blocked('COVERAGE_INCOMPLETE', 'selected groups have not delivered')
            covered_gaps = set()
            for group, item in frozen['groups'].items():
                delivery = item['delivery']
                key = f'{delivery["delivery_id"]}:{delivery["version"]}'
                disposition = tx.get('group_dispositions', key)
                if item['source']:
                    if not disposition:
                        return blocked('DISPOSITION_MISSING', 'frozen group delivery requires final disposition')
                    body = disposition['body']
                    if body['plan_ref'] != context.plan_ref or body['run_id'] != context.run_ref:
                        raise Conflict('group disposition scope/version mismatch')
                    result = NodeResult.model_validate(body['result'])
                    if group in scopes:
                        raise Conflict('group/work scope collision')
                    scopes[group] = (result, body['processing_ref'])
                    if ClosureService.classify_scope(result, body['processing_ref']) == 'partial':
                        covered_gaps.update(body['gaps'])
                        covered_gaps.add(group + ':review_rejected')
                for child in item['work_deliveries'] or [delivery]:
                    processed = [scopes.get(work) for work in child['work_refs']]
                    if processed and all(entry and ClosureService.classify_scope(*entry) == 'partial'
                                         for entry in processed):
                        covered_gaps.add(child['delivery_id'] + ':case_index_missing')
                        covered_gaps.add(child['delivery_id'] + ':review_rejected')
                        covered_gaps.update(child['gaps'])
            if not scopes or set(record['gaps']) - covered_gaps:
                return blocked('COVERAGE_INCOMPLETE', 'unprocessed gaps remain in the factual report')
            inflight = any(w['status'] not in {'delivered','failed','blocked','cancelled','superseded'} for w in work)
        for ref in export['files'].values():
            self.artifacts.verify(EvidenceRef.model_validate(ref))
        # Recheck the exact frozen raw artifacts at publication, including a
        # recomputation; a successful A2/F1 receipt alone is not proof they remain.
        with tempfile.TemporaryDirectory(prefix='sandbox-archive-verify-') as directory:
            aliases = {}
            _, expected, gaps = self._materialize(record['analysis_input_ref'], context.plan_ref, Path(directory), run_id=context.run_ref, alias_sink=aliases)
            if expected != record['expected_manifests'] or gaps != record['gaps'] or aliases != record['case_aliases']:
                raise Conflict('archive evidence differs from calculated inputs')
        archive = ClosureService(self.repository).seal_archive(
            context.run_ref, expected_revision=revision, analysis_ref=identity,
            report_analysis_ref=report['analysis_ref'], excel_analysis_ref=export['analysis_ref'],
            scopes=scopes, inflight=inflight,
            artifact_refs=[ref['artifact_id'] for ref in export['files'].values()],
            token=token_for(context), expected_plan_ref=context.plan_ref)
        return NodeResult(execution_status='succeeded', output={
            'analysis_ref': identity, 'archive_status': archive['status']})
