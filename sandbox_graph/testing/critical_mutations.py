"""ATG-06 developer mutation campaign; never a production GateManifest.

Each mutation runs in a disposable copy. Baseline tests are unchanged. Only a
collected call-phase assertion failure with successful setup/teardown counts as
caught; import errors, skips, timeouts and crashes do not count.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import hashlib

PREFIX='tests/orchestration/unit/'
# (id, source, exact old, replacement, expected occurrences, independent oracle)
MUTATIONS=(
 ('removed_fence','sandbox_graph/services/agent_lifecycle.py',
  'def _assert_scope_token(tx, identity, token):\n        tx.assert_fence(token)',
  'def _assert_scope_token(tx, identity, token):\n        pass',1,
  PREFIX+'test_lifecycle_service.py::test_scope_and_expired_fence_cannot_create_lifecycle'),
 ('unknown_usage_zero','sandbox_graph/services/lifecycle_budgets.py',
  'elif actual is None:\n                unknown = True',
  'elif actual is None:\n                unknown = True\n                charged[counter] = 0',1,
  PREFIX+'test_lifecycle_budgets.py::test_budget_survives_context_and_revision_changes'),
 ('fixture_jump','sandbox_graph/services/recovery.py','entry_node=route[0]',"entry_node='N05'",1,
  PREFIX+'test_recovery_plan.py::test_destroyed_fixture_invalidates_resume'),
 ('infinite_retry','sandbox_graph/domain/agent_lifecycle.py',
  'guards.same_failure_count < guards.max_same_failure or guards.new_evidence','True',2,
  PREFIX+'test_lifecycle_state.py::test_same_failure_without_new_evidence_cannot_loop'),
 ('self_signed_gate','sandbox_graph/services/gates.py',
  'def evaluate_in(cls,tx,manifest_ref,candidate):',
  "def evaluate_in(cls,tx,manifest_ref,candidate):\n        return {'status':'pass'}",1,
  PREFIX+'test_trusted_gates.py::test_only_stored_authenticated_current_manifest_can_pass'),
 ('activation_without_worker','sandbox_graph/services/promotion.py','def promote(self,request,*,token):',
  "def promote(self,request,*,token):\n        return {'status':'activated'}",1,
  PREFIX+'test_promotion.py::test_no_ready_worker_does_not_close_old_run'),
 ('candidate_failure_terminal','sandbox_graph/runtime/candidate_worker.py',
  "'fail':'tests_failed','blocked':'gate_blocked'","'fail':'gate_blocked','blocked':'gate_blocked'",1,
  PREFIX+'test_candidate_worker.py::test_real_structured_edits_failed_gate_then_second_revision_pass'),
 ('repair_failure_terminal','sandbox_graph/domain/agent_lifecycle.py',
  "('remediating', 'postcheck_fail_with_budget'): '@retry'","('remediating', 'postcheck_fail_with_budget'): 'blocked'",1,
  PREFIX+'test_lifecycle_state.py::test_failure_with_new_strategy_loops'),
 ('performance_failure_resample','sandbox_graph/services/recovery.py',
  'route, revalidate = tails[record.key.node_id], ()',"route, revalidate = ('N05',)+tails[record.key.node_id], ()",1,
  PREFIX+'test_recovery_plan.py::test_measurement_continuity_and_generation[N06]'),
 ('zero_collection_pass','sandbox_graph/testing/local_gate.py',
  'errors=[]\n    if not validate_collection',"errors=[]\n    if not required:return {'status':'pass','errors':[],'required_count':0}\n    if not validate_collection",1,
  PREFIX+'test_lifecycle_stop_gate.py::test_rejects_incomplete_or_changed_evidence[zero]'),
)


def patch(root,mutation):
    _,name,old,new,count,_=mutation;path=Path(root)/name;source=path.read_text()
    if source.count(old)!=count:raise ValueError('mutation source drift: '+mutation[0])
    path.write_text(source.replace(old,new))


def caught(report,node,returncode):
    if returncode!=1 or report.get('collection_errors') or report.get('collected')!=[node]:return False
    rows=report.get('phases',[])
    if len(rows)!=3 or {r.get('when') for r in rows}!={'setup','call','teardown'}:return False
    return all(r.get('nodeid')==node and not r.get('wasxfail') and
        r.get('outcome')==('failed' if r['when']=='call' else 'passed') for r in rows)


def campaign(root,output):
    root=Path(root).resolve();output=Path(output).absolute();output.mkdir(parents=True,exist_ok=False)
    results=[]
    for mutation in MUTATIONS:
        identity,name,old,new,count,node=mutation
        with tempfile.TemporaryDirectory(prefix='sandbox-mutant-') as directory:
            tree=Path(directory)
            for folder in ('sandbox_graph','sandbox_bench','tests','config'):
                shutil.copytree(root/folder,tree/folder,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
            shutil.copy2(root/'pyproject.toml',tree/'pyproject.toml')
            test=tree/node.split('::')[0];before=hashlib.sha256(test.read_bytes()).hexdigest()
            patch(tree,mutation)
            report=output/(identity+'.json');log=output/(identity+'.log')
            env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTEST_ADDOPTS','SANDBOX_GATE_REPORT'}}
            env.update(PYTHONPATH=str(tree),PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',SANDBOX_GATE_REPORT=str(report))
            argv=[sys.executable,'-m','pytest','-q','-p','pytest_asyncio.plugin',
                  '-p','sandbox_graph.testing.pytest_receipts',node]
            try:
                with log.open('wb') as stream:process=subprocess.run(argv,cwd=tree,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=60)
                value=json.loads(report.read_text());killed=caught(value,node,process.returncode)
                if hashlib.sha256(test.read_bytes()).hexdigest()!=before:killed=False
                results.append({'mutation':identity,'nodeid':node,'caught':killed,'returncode':process.returncode,
                    'baseline_test_sha256':before,'mutated_source_sha256':hashlib.sha256((tree/name).read_bytes()).hexdigest()})
            except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                results.append({'mutation':identity,'nodeid':node,'caught':False,'error':type(exc).__name__})
    value={'schema_version':1,'kind':'developer_critical_mutations','status':'pass' if all(r['caught'] for r in results) else 'fail','results':results}
    (output/'summary.json').write_text(json.dumps(value,indent=2)+'\n');return value
