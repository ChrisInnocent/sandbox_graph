import tempfile
from pathlib import Path
import unittest
from sandbox_bench.extensions import (ExtensionProfile,ExtensionStep,ActionRegistry,
                                      ExtensionRunner,KnownActionFailure)


class ExtensionTests(unittest.TestCase):
    def test_unresponsive_action_has_bounded_wait_and_unknown_effect(self):
        import threading
        import time
        from dataclasses import replace
        release=threading.Event()
        registry=ActionRegistry()
        registry.register('create',lambda p,c: (release.wait(1) or {}),side_effect=True)
        profile=replace(self.profile('E03',[ExtensionStep('create','create')]),max_duration_s=.05)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                start=time.monotonic()
                result=ExtensionRunner(profile,registry,Path(tmp)/'state').run(selected=True)
                self.assertLess(time.monotonic()-start,.5)
                self.assertEqual(result['status'],'isolated')
                self.assertEqual(result['steps']['0']['status'],'unknown')
            finally:release.set()

    def profile(self,kind,steps,**extra):
        return ExtensionProfile(kind=kind,version='v1',profile_id='p1',seed=7,
                                max_steps=20,max_duration_s=60,
                                scope={'environment':'test','resource_ids':['owned']},steps=tuple(steps),**extra)

    def test_unselected_unknown_and_missing_workload_never_invoke_callbacks(self):
        registry=ActionRegistry();called=[]
        registry.register('work',lambda p,c:called.append(p),side_effect=False)
        profile=self.profile('E01',[ExtensionStep('work','workload')])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';runner=ExtensionRunner(profile,registry,path)
            self.assertEqual(runner.run(selected=False)['status'],'not_selected')
            self.assertFalse(path.exists())
            self.assertEqual(runner.run(selected=True)['status'],'blocked')
            self.assertEqual(called,[])
            unknown=self.profile('E03',[ExtensionStep('unregistered','create')])
            with self.assertRaises(ValueError):
                ExtensionRunner(unknown,registry,path).run(selected=True)

    def test_e01_fixed_work_units_seed_and_hashes(self):
        workload={'work_units':3,'input_sha256':'a'*64,'output_sha256':'b'*64}
        registry=ActionRegistry();seeds=[]
        registry.register('work',lambda p,c:(seeds.append(c['seed']) or dict(workload)),side_effect=False)
        profile=self.profile('E01',[ExtensionStep('work','workload')],workload=workload)
        with tempfile.TemporaryDirectory() as tmp:
            runner=ExtensionRunner(profile,registry,Path(tmp)/'state')
            result=runner.run(selected=True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(seeds,[7])
            self.assertEqual(runner.run(selected=True)['status'],'complete')
            self.assertEqual(seeds,[7])
        bad=ActionRegistry();bad.register('work',lambda p,c:dict(workload,work_units=4),side_effect=False)
        with tempfile.TemporaryDirectory() as tmp:
            result=ExtensionRunner(profile,bad,Path(tmp)/'state').run(selected=True)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['reason'],'workload_evidence_mismatch')

    def test_e02_reset_content_and_identity_are_asserted(self):
        registry=ActionRegistry()
        for name in ['baseline','mutate','reset','verify']:
            registry.register(name,lambda p,c:{'content_sha256':'original','sandbox_id':'owned'},side_effect=False)
        steps=[ExtensionStep(name,name) for name in ['baseline','mutate','reset','verify']]
        profile=self.profile('E02',steps,reset_mode='reset')
        with tempfile.TemporaryDirectory() as tmp:
            result=ExtensionRunner(profile,registry,Path(tmp)/'state').run(selected=True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['group'],'reset')
        bad=ActionRegistry()
        for name in ['baseline','mutate','reset']:
            bad.register(name,lambda p,c:{'content_sha256':'original','sandbox_id':'owned'},side_effect=False)
        bad.register('verify',lambda p,c:{'content_sha256':'changed','sandbox_id':'owned'},side_effect=False)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ExtensionRunner(profile,bad,Path(tmp)/'state').run(selected=True)['reason'],'reset_content_mismatch')

    def test_e03_recovery_keeps_completed_prefix_and_segments_unknown_state(self):
        registry=ActionRegistry();calls=[]
        registry.register('create',lambda p,c:(calls.append('create') or {'sandbox_id':'owned'}),side_effect=True)
        def pause(p,c):
            calls.append('pause');raise ConnectionError('response lost')
        registry.register('pause',pause,side_effect=True)
        registry.register('resume',lambda p,c:(calls.append('resume') or {}),side_effect=True)
        registry.register('audit',lambda p,c:(calls.append('audit') or {'healthy':True}),side_effect=False)
        profile=self.profile('E03',[ExtensionStep(a,a) for a in ['create','pause','resume','audit']])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state';runner=ExtensionRunner(profile,registry,path)
            self.assertEqual(runner.run(selected=True)['status'],'isolated')
            reopened=ExtensionRunner(profile,registry,path)
            self.assertEqual(reopened.run(selected=True)['status'],'isolated')
            self.assertEqual(calls,['create','pause'])
            reopened.reconcile_step(1,outcome='applied',evidence={'receipt':'confirmed-paused'},result={})
            result=reopened.run(selected=True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(calls,['create','pause','resume','audit'])
            self.assertGreaterEqual(len(result['segments']),2)
        illegal=self.profile('E03',[ExtensionStep('resume','resume')])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            ExtensionRunner(illegal,registry,Path(tmp)/'state').run(selected=True)

    def test_pressure_interference_fault_compensation_persists_and_unknown_isolated(self):
        for kind,stages in [('E04',['pressure','work','release','recovery']),
                            ('E05',['baseline','interference','measure','remove','recovery']),
                            ('E06',['inject','observe','recover','verify'])]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                registry=ActionRegistry();calls=[]
                first_effect='interference' if kind=='E05' else stages[0]
                def effect(p,c):
                    calls.append(c['action']);raise ConnectionError('unknown applied')
                def undo(p,c):calls.append('undo')
                for stage in stages:
                    registry.register(stage,effect if stage==first_effect else (lambda p,c:{}),
                                      side_effect=stage==first_effect,compensate=undo if stage==first_effect else None)
                profile=self.profile(kind,[ExtensionStep(s,s,parameters={'targets':['owned']}) for s in stages])
                runner=ExtensionRunner(profile,registry,Path(tmp)/'state')
                self.assertEqual(runner.run(selected=True)['status'],'isolated')
                self.assertEqual(runner.recover()['status'],'isolated')
                self.assertNotIn('undo',calls)
                index=stages.index(first_effect)
                runner.reconcile_step(index,outcome='applied',evidence={'receipt':'effect observed'},result={})
                self.assertEqual(runner.recover()['status'],'compensated')
                self.assertEqual(calls,[first_effect,'undo'])
                self.assertEqual(runner.recover()['status'],'compensated')
                self.assertEqual(calls,[first_effect,'undo'])

    def test_scope_budget_and_missing_compensator_are_rejected_preflight(self):
        registry=ActionRegistry();registry.register('inject',lambda p,c:{},side_effect=True)
        profile=self.profile('E06',[ExtensionStep('inject','inject')])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            ExtensionRunner(profile,registry,Path(tmp)/'state').run(selected=True)
        registry=ActionRegistry();registry.register('create',lambda p,c:{},side_effect=True)
        profile=self.profile('E03',[ExtensionStep('create','create',parameters={'targets':['not-owned']})])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            ExtensionRunner(profile,registry,Path(tmp)/'state').run(selected=True)

    def test_each_registered_stage_known_failure_preserves_recoverable_stack(self):
        variants={'E04':['pressure','work','release','recovery'],
                  'E05':['baseline','interference','measure','remove','recovery'],
                  'E06':['inject','observe','recover','verify']}
        for kind,stages in variants.items():
            for fail_at in stages:
                with self.subTest(kind=kind,fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                    registry=ActionRegistry();applied=[];undone=[]
                    for stage in stages:
                        def action(parameters,context,stage=stage):
                            if stage==fail_at:
                                raise KnownActionFailure('controlled_failure',applied=False)
                            applied.append(stage);return {}
                        def compensate(parameters,context,stage=stage):undone.append(stage)
                        registry.register(stage,action,side_effect=True,compensate=compensate)
                    profile=self.profile(kind,[ExtensionStep(stage,stage) for stage in stages])
                    runner=ExtensionRunner(profile,registry,Path(tmp)/'state')
                    self.assertEqual(runner.run(selected=True)['status'],'failed')
                    self.assertEqual(undone,list(reversed(applied)))
                    runner.recover()
                    self.assertEqual(undone,list(reversed(applied)))

    def test_compensation_unknown_requires_evidence_and_does_not_repeat(self):
        registry=ActionRegistry();undos=[]
        def undo(p,c):undos.append('undo');raise ConnectionError('ack lost')
        for stage in ['inject','observe','recover','verify']:
            registry.register(stage,lambda p,c:{},side_effect=stage=='inject',compensate=undo if stage=='inject' else None)
        profile=self.profile('E06',[ExtensionStep(s,s) for s in ['inject','observe','recover','verify']])
        with tempfile.TemporaryDirectory() as tmp:
            runner=ExtensionRunner(profile,registry,Path(tmp)/'state')
            self.assertEqual(runner.run(selected=True)['status'],'isolated')
            self.assertEqual(runner.recover()['status'],'isolated')
            self.assertEqual(undos,['undo'])
            runner.reconcile_compensation(0,applied=True,evidence={'receipt':'fault removed'})
            self.assertEqual(runner.recover()['status'],'compensated')
            self.assertEqual(undos,['undo'])

    def test_deadline_stops_following_stage_and_mutated_scope_is_rejected(self):
        now=[0.0];registry=ActionRegistry();calls=[]
        def first(p,c):
            calls.append('first');now[0]=61;return {}
        registry.register('create',first,side_effect=True)
        registry.register('audit',lambda p,c:(calls.append('audit') or {'healthy':True}),side_effect=False)
        profile=self.profile('E03',[ExtensionStep('create','create'),ExtensionStep('audit','audit')])
        with tempfile.TemporaryDirectory() as tmp:
            runner=ExtensionRunner(profile,registry,Path(tmp)/'state',clock=lambda:now[0])
            self.assertEqual(runner.run(selected=True)['reason'],'deadline_exceeded')
            self.assertEqual(calls,['first'])
        with tempfile.TemporaryDirectory() as tmp:
            runner=ExtensionRunner(profile,registry,Path(tmp)/'state')
            profile.scope['resource_ids'].append('not-originally-owned')
            with self.assertRaises(ValueError):
                runner.run(selected=True)

    def test_periodic_audit_contract_is_enforced_before_actions(self):
        registry=ActionRegistry()
        for name in ['create','pause','audit']:
            registry.register(name,lambda p,c:{},side_effect=False)
        profile=self.profile('E03',[ExtensionStep('create','create'),ExtensionStep('pause','pause'),ExtensionStep('audit','audit')],audit_every_steps=1)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            ExtensionRunner(profile,registry,Path(tmp)/'state').run(selected=True)
