import json
from pathlib import Path
import tempfile
import unittest
from sandbox_bench.analysis import analyze_case, export_case, verify_case, recompute_case


class AnalysisTests(unittest.TestCase):
    def records(self):
        return {'case_id':'case-1',
                'operations':[{'operation_id':str(i),'status':'success','elapsed_s':v,'repeat':1,'method':'WINDOW'} for i,v in enumerate([1,2,3,4])],
                'events':[{'operation_id':str(i),'completed_s':v,'clock_domain':'n:b'} for i,v in enumerate([.2,.7,1.2,2.2])],
                'window':{'start_s':0,'end_s':2.5,'window_kind':'fixed_deadline','clock_domain':'n:b'},
                'resource_series':[],'cohorts':[],'assertions':[],'cleanup_ledger':[],'raw_refs':[]}

    def test_analysis_uses_same_golden_metrics_and_repeat_strata(self):
        result=analyze_case(self.records())
        self.assertAlmostEqual(result['latency']['repeats']['1']['latency_s']['p95'],3.85)
        self.assertEqual(result['throughput']['overall_per_s'],1.6)
        self.assertEqual(result['quality']['completed_success_count'],4)

    def test_missing_assertions_leave_both_qualifications_unverified(self):
        result=analyze_case(self.records())
        self.assertEqual(result['qualifications']['status'],'unverified')
        self.assertEqual(result['qualifications']['post_validation']['status'],'unverified')
        self.assertEqual(result['qualifications']['effective_spec']['status'],'unverified')

    def test_failed_post_validation_preserves_measured_success_and_latency(self):
        records=self.records()
        records['assertions']=[
            {'kind':'post_validation','status':'invalid','sandbox_id':'s1','reason':'sentinel_mismatch'},
            {'kind':'effective_spec','status':'valid','sandbox_id':'s1',
             'source_spec_proof':{'cpu':2},'actual_spec':{'cpu':2}}]
        result=analyze_case(records)
        self.assertEqual(result['qualifications']['status'],'invalid')
        self.assertEqual(result['qualifications']['post_validation']['status'],'invalid')
        self.assertEqual(result['qualifications']['effective_spec']['status'],'valid')
        evidence=result['qualifications']['effective_spec']['assertions'][0]
        self.assertEqual(evidence['source_spec_proof'],{'cpu':2})
        self.assertEqual(evidence['actual_spec'],{'cpu':2})
        self.assertEqual(result['quality']['completed_success_count'],4)
        self.assertAlmostEqual(result['latency']['repeats']['1']['latency_s']['p95'],3.85)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            export_case(records,path)
            self.assertEqual(recompute_case(path),result)

    def test_qualification_precedence_requires_all_claims_valid(self):
        for second_status,expected in [('valid','valid'),('unverified','unverified'),
                                       ('invalid','invalid'),('completed','unverified')]:
            with self.subTest(second_status=second_status):
                records=self.records()
                records['assertions']=[
                    {'kind':'post_validation','status':'valid','sandbox_id':'s1'},
                    {'kind':'post_validation','status':second_status,'sandbox_id':'s2'},
                    {'kind':'effective_spec','status':'valid','sandbox_id':'s1'}]
                self.assertEqual(analyze_case(records)['qualifications']['status'],expected)

    def test_no_command_validation_is_not_applicable_but_semantic_assertions_still_apply(self):
        records=self.records();records['validation_policy']='none'
        records['assertions']=[{'kind':'effective_spec','status':'valid','sandbox_id':'s1'}]
        result=analyze_case(records)['qualifications']
        self.assertEqual(result['post_validation'],{'status':'not_applicable','assertions':[]})
        self.assertEqual(result['status'],'valid')
        records['assertions'].append({'kind':'post_validation','status':'invalid',
                                      'sandbox_id':'s1','reason':'clone_isolation_failure'})
        self.assertEqual(analyze_case(records)['qualifications']['status'],'invalid')

    def test_immutable_export_roundtrip_recomputes_same_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            manifest=export_case(self.records(),path)
            self.assertEqual(verify_case(path),manifest)
            before=(path/'summary.json').read_bytes()
            self.assertEqual(json.loads(before),recompute_case(path))
            self.assertEqual((path/'summary.json').read_bytes(),before)
            self.assertTrue((path/'summary.csv').is_file())
            with self.assertRaises(FileExistsError):
                export_case(self.records(),path)

    def test_tamper_missing_sequence_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            export_case(self.records(),path)
            (path/'events.json').write_text('[]')
            with self.assertRaises(ValueError):
                verify_case(path)
        records=self.records()
        records['events'][0]['seq']=2
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            export_case(records,Path(tmp)/'case')

    def test_resource_summary_is_not_invented_for_empty_or_cross_domain_samples(self):
        result=analyze_case(self.records())
        self.assertEqual(result['resources']['status'],'unavailable')
        records=self.records();records['resource_max_gap_s']=2
        records['resource_series']=[{'kind':'pss','t_s':0,'value':10,'clock_domain':'n:b','cohort':['s']},
                                    {'kind':'pss','t_s':2.5,'value':10,'clock_domain':'n:b','cohort':['s']}]
        result=analyze_case(records)
        self.assertEqual(result['resources']['pss']['coverage'],0)
        self.assertIsNone(result['resources']['pss']['mean'])

    def test_declared_missing_sample_range_roundtrips_without_fabricating_samples(self):
        records=self.records()
        records['resource_series']=[{'kind':'sample_gap','seq':1,'through_seq':3,'reason':'spool_capacity','dropped_count':3},
                                    {'kind':'pss','seq':4,'t_s':1,'value':20,'clock_domain':'n:b','cohort':['s']}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            export_case(records,path)
            saved=json.loads((path/'resource_series.json').read_text())
            self.assertEqual([r['seq'] for r in saved],[1,4])
            self.assertEqual(recompute_case(path)['resources']['pss']['reason'],'missing_max_gap_s')

    def test_manifest_window_change_cannot_pass_internal_summary_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case';export_case(self.records(),path)
            manifest=json.loads((path/'case_manifest.json').read_text())
            manifest['metadata']['window']['end_s']=10
            (path/'case_manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                verify_case(path)

    def test_unaligned_resource_domains_are_not_silently_flattened(self):
        records=self.records();records['resource_max_gap_s']=3
        records['resource_series']=[{'kind':'pss','t_s':0,'value':10,'clock_domain':'n:b','cohort':['s']},
                                    {'kind':'pss','t_s':2.5,'value':10,'clock_domain':'n:c','cohort':['s']}]
        self.assertEqual(analyze_case(records)['resources']['status'],'unavailable')

    def test_explicit_sample_gap_breaks_resource_integral_even_with_short_dt(self):
        records=self.records();records['resource_max_gap_s']=3
        records['window']['end_s']=1
        records['events']=[]
        records['resource_series']=[{'kind':'pss','seq':1,'t_s':0,'value':10,'clock_domain':'n:b','cohort':['s']},
                                   {'kind':'sample_gap','seq':2,'through_seq':2,'dropped_count':1,'reason':'spool_capacity'},
                                   {'kind':'pss','seq':3,'t_s':1,'value':10,'clock_domain':'n:b','cohort':['s']}]
        result=analyze_case(records)
        self.assertEqual(result['resources']['pss']['coverage'],0)
        self.assertIsNone(result['resources']['pss']['mean'])

    def test_resource_window_from_another_clock_domain_is_not_used_for_integrals(self):
        records=self.records();records['resource_max_gap_s']=3
        records['resource_series']=[{'kind':'pss','t_s':0,'value':10,'clock_domain':'other:boot','cohort':['s']},
                                    {'kind':'pss','t_s':2.5,'value':10,'clock_domain':'other:boot','cohort':['s']}]
        result=analyze_case(records)
        self.assertEqual(result['resources']['status'],'unavailable')
        self.assertEqual(result['resources']['reason'],'resource_window_clock_mismatch')

    def test_raw_payloads_are_content_addressed_and_logical_names_never_paths(self):
        import hashlib
        payload=b'{"source":"original"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            manifest=export_case(self.records(),path,raw_payloads={'load.json':payload,'../unsafe-logical-name':payload})
            expected='raw/'+hashlib.sha256(payload).hexdigest()
            self.assertEqual((path/expected).read_bytes(),payload)
            self.assertIn(expected,manifest['files'])
            refs=json.loads((path/'raw_refs.json').read_text())
            self.assertEqual({r['path'] for r in refs},{expected})
            self.assertEqual({r['logical_name'] for r in refs},{'load.json','../unsafe-logical-name'})
            self.assertEqual(verify_case(path),manifest)
            self.assertEqual(recompute_case(path),analyze_case(self.records()))

    def test_missing_or_tampered_raw_is_rejected(self):
        for mode in ['missing','tamper']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'case'
                export_case(self.records(),path,raw_payloads={'load.json':b'original'})
                raw=next((path/'raw').iterdir())
                if mode=='missing':raw.unlink()
                else:raw.write_bytes(b'altered!')
                with self.assertRaises(ValueError):verify_case(path)

    def test_raw_reference_without_payload_and_symlink_directory_are_rejected(self):
        records=self.records();records['raw_refs']=[{'path':'load.json','sha256':'a'*64}]
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ValueError):
            export_case(records,Path(tmp)/'case')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            export_case(self.records(),path,raw_payloads={'load.json':b'original'})
            (path/'raw').rename(path/'raw_saved')
            (path/'raw').symlink_to(path/'raw_saved',target_is_directory=True)
            with self.assertRaises(ValueError):verify_case(path)

    def test_raw_reference_must_resolve_even_if_ref_index_hash_was_updated(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case'
            export_case(self.records(),path,raw_payloads={'load.json':b'original'})
            refs=json.loads((path/'raw_refs.json').read_text())
            refs[0]['path']='raw/'+'0'*64
            raw=json.dumps(refs).encode();(path/'raw_refs.json').write_bytes(raw)
            manifest=json.loads((path/'case_manifest.json').read_text())
            manifest['files']['raw_refs.json']={'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
            (path/'case_manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):verify_case(path)

    def test_density_curve_points_reference_the_actual_raw_trace(self):
        import hashlib
        records=self.records();records['density']={'trace':[{'observed_at':1,'listed_count':2},
                                                          {'observed_at':2,'listed_count':4}]}
        raw=json.dumps(records['density']).encode();checksum=hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'case';export_case(records,path,raw_payloads={'density.json':raw})
            curve=recompute_case(path)['density_curve']
            self.assertEqual(curve['points'][1]['observation']['listed_count'],4)
            self.assertEqual(curve['points'][1]['evidence_ref'],{
                'logical_name':'density.json','sha256':checksum,'path':'raw/'+checksum,'json_pointer':'/trace/1'})
            records['density']['trace'][1]['listed_count']=99
            with self.assertRaises(ValueError):
                export_case(records,Path(tmp)/'mismatched',raw_payloads={'density.json':raw})

    def test_resources_expose_roles_components_cohort_and_independent_steady_window(self):
        records=self.records();records['window']['end_s']=2;records['events']=[];records['resource_max_gap_s']=1
        records['final_cohort']=['s1','s2'];records['steady']={'status':'steady','start_s':1,'end_s':2,'clock_domain':'n:b','cohort':['s1','s2']}
        def process(pid,pss,cpu,roles,components,sid=None):
            return {'identity':{'environment':'e','node':'n','boot_id':'b','pid':pid,'start_ticks':1},
                    'pss_bytes':pss,'cpu_seconds':cpu,'roles':roles,'components':components,
                    'sandbox_id':sid,'generation':'g1' if sid else None}
        records['resource_series']=[{'kind':'resource_sample','t_s':t,'clock_domain':'n:b','inventory_complete':True,
             'snapshot_cohort_ids':['s1','s2'],'processes':[
                 process(1,10,2*t,['management','shared'],['agent']),
                 process(2,20+10*t,t,['work'],['vmm'],'s1'),
                 process(3,30+10*t,2*t,['work'],['vmm'],'s2')]} for t in (0,1,2)]
        result=analyze_case(records)['resources']
        self.assertEqual(result['pss']['mean'],70)
        self.assertEqual(result['cpu']['sampled_peak_cores'],5)
        self.assertEqual(result['by_role']['management']['pss']['mean'],10)
        self.assertEqual(result['by_component']['vmm']['cpu']['cpu_seconds'],6)
        self.assertEqual(len(result['by_identity']),3)
        self.assertEqual(result['steady']['pss']['mean'],80)
        self.assertEqual(result['final_cohort']['direct_mean_bytes'],45)
        self.assertEqual(result['final_cohort']['with_shared_mean_bytes'],50)
        self.assertEqual(result['guest_net_pss']['status'],'unavailable')
        self.assertEqual(result['guest_net_pss']['raw_pss_bytes'],90)
        records['resource_series'][-1]['processes'].pop()
        records['resource_series'][-1]['inventory_complete']=False
        partial=analyze_case(records)['resources']
        self.assertIsNone(partial['pss']['mean'])
        self.assertEqual(partial['final_cohort']['coverage'],.5)
        self.assertIsNone(partial['final_cohort']['direct_mean_bytes'])
        self.assertEqual(partial['final_cohort']['observed_mean_bytes'],40)

    def test_generation_change_breaks_cpu_and_partial_inventory_never_full_coverage(self):
        records=self.records();records['window']['end_s']=1;records['events']=[];records['resource_max_gap_s']=1
        records['resource_series']=[{'kind':'resource_sample','t_s':t,'clock_domain':'n:b',
            'inventory_complete':False,'snapshot_cohort_ids':['s1','missing'],
            'processes':[{'identity':{'pid':1,'start_ticks':1},'sandbox_id':'s1','generation':g,
                          'roles':['work'],'components':['vmm'],'cpu_seconds':t,'pss_bytes':10}]}
            for t,g in [(0,'g1'),(1,'g2')]]
        result=analyze_case(records)['resources']
        self.assertIsNone(result['pss']['mean'])
        self.assertIsNone(result['cpu']['mean_cores'])
        self.assertEqual(result['cpu']['coverage'],0)

    def test_steady_cpu_can_recover_after_pid_restart_without_using_old_cohort(self):
        records=self.records();records['window']['end_s']=3;records['events']=[];records['resource_max_gap_s']=1
        records['steady']={'status':'steady','start_s':2,'end_s':3,'clock_domain':'n:b'}
        records['resource_series']=[{'kind':'resource_sample','t_s':t,'clock_domain':'n:b','inventory_complete':True,
            'snapshot_cohort_ids':['s1'],'processes':[{'identity':{'pid':pid,'start_ticks':1},
            'sandbox_id':'s1','generation':'g'+str(pid),'roles':['work'],'components':['vmm'],
            'cpu_seconds':t,'pss_bytes':10}]} for t,pid in [(0,1),(1,1),(2,2),(3,2)]]
        result=analyze_case(records)['resources']
        self.assertIsNone(result['cpu']['mean_cores'])
        self.assertEqual(result['steady']['cpu']['mean_cores'],1)
        self.assertEqual(result['steady']['cpu']['coverage'],1)

    def test_node_views_keep_local_windows_without_cross_host_aggregation(self):
        records=self.records();records['events']=[];records['resource_max_gap_s']=2
        records['resource_series']=[{'kind':'resource_sample','t_s':t,'clock_domain':node+':boot',
            'inventory_complete':True,'snapshot_cohort_ids':['s1'],'processes':[
                {'identity':{'node':node,'pid':1,'start_ticks':1},'sandbox_id':'s1','generation':'g1',
                 'roles':['work'],'components':['vmm'],'cpu_seconds':t,'pss_bytes':10}]}
            for node,t in [('n',0),('n',1),('other',100),('other',101)]]
        result=analyze_case(records)['resources']
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['by_node']['other']['pss']['mean'],10)
        self.assertEqual(result['by_node']['other']['window']['window_kind'],'observed_span')
        self.assertEqual(result['by_node_role_component']['n']['work']['vmm']['cpu']['mean_cores'],1)
        records['resource_series'][-1]['inventory_complete']=False
        last=analyze_case(records)['resources']['final_cohort']
        self.assertFalse(last['inventory_complete'])
        self.assertIsNone(last['direct_mean_bytes'])
