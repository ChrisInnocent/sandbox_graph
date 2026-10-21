import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from sandbox_bench.analysis import export_case
from sandbox_bench.comparison import compare_cases, export_comparison


class ComparisonTests(unittest.TestCase):
    def seal(self, root, name, *, product='kuasar', values=(1,2,3,4), change=None, valid=True):
        descriptor={'product':product,'product_version':'1.0','method_id':'B01.TEMPLATE',
                    'method_version':'v1','completion':{'endpoint':'STATE_READY'},
                    'spec':{'vcpus':2,'memory_bytes':1024},'cache_profile':'cold',
                    'condition':{'hardware_profile':'h1','load_mode':'WINDOW','concurrency':2},
                    'window':{'kind':'natural_drain','admission_budget_s':10}}
        descriptor.update(change or {})
        records={'case_id':name,'comparison':descriptor,'validation_policy':'none',
                 'operations':[{'operation_id':str(i),'status':'success','elapsed_s':v,
                                'product':product,'repeat':1,'method':'B01.TEMPLATE'} for i,v in enumerate(values)],
                 'assertions':[{'kind':'effective_spec','status':'valid' if valid else 'unverified'}]}
        path=Path(root)/name;export_case(records,path)
        return path

    def test_repeat_p95_are_separate_facts_with_exact_manifest_citations(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'run-1');b=self.seal(root,'run-2',values=(10,20,30,40))
            result=compare_cases([b,a])
            facts=[f for f in result['facts'] if f['metric_path']=='/latency/repeats/1/latency_s/p95']
            self.assertAlmostEqual(facts[0]['value'],3.85)
            self.assertAlmostEqual(facts[1]['value'],38.5)
            self.assertEqual([f['case_id'] for f in facts],['run-1','run-2'])
            self.assertEqual(facts[0]['manifest_sha256'],hashlib.sha256((a/'case_manifest.json').read_bytes()).hexdigest())
            self.assertEqual(result['pairs'][0]['status'],'comparable')
            self.assertEqual(result['conclusions'],[])
            self.assertEqual(compare_cases([a,b]),result)

    def test_every_contract_difference_is_reported_without_dropping_facts(self):
        changes={'method_id':'B01.SNAPSHOT','method_version':'v2','completion':{'endpoint':'API_COMPLETE'},
                 'spec':{'vcpus':4},'cache_profile':'warm','condition':{'hardware_profile':'h2'},
                 'window':{'kind':'fixed_deadline'},'product_version':'2.0'}
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a');b=self.seal(root,'b',change=changes)
            result=compare_cases([a,b]);pair=result['pairs'][0]
            self.assertEqual(pair['status'],'incomparable')
            self.assertEqual(set(pair['reasons']),{f'mismatch:{key}' for key in changes})
            self.assertTrue(result['facts'])

    def test_cross_product_versions_are_explicit_and_missing_conditions_block_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a');b=self.seal(root,'b',product='agentenv',change={'product_version':'8'})
            c=self.seal(root,'c',change={'condition':None})
            result=compare_cases([a,b,c])
            self.assertEqual(result['pairs'][0]['status'],'comparable')
            self.assertIn('missing:condition',result['pairs'][1]['reasons'])

    def test_invalid_or_empty_measurements_never_become_comparable(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a');b=self.seal(root,'b',valid=False);c=self.seal(root,'c',values=())
            result=compare_cases([a,b,c])
            self.assertIn('unqualified:b',result['pairs'][0]['reasons'])
            self.assertIn('no_success_samples:c',result['pairs'][1]['reasons'])
            self.assertEqual(result['conclusions'],[])
        self.assertEqual(compare_cases([])['status'],'no_data')

    def test_tampered_seal_and_duplicate_case_id_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a')
            with self.assertRaises(ValueError):compare_cases([a,a])
            (a/'operations.json').write_text('[]')
            with self.assertRaises(ValueError):compare_cases([a])

    def test_json_markdown_export_and_separate_interpretation_require_known_fact_references(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a');result=compare_cases([a]);fid=result['facts'][0]['fact_id']
            out=Path(root)/'comparison'
            export_comparison([a],out,interpretation=[{'text':'Review this measurement.','fact_refs':[fid]}])
            saved=json.loads((out/'facts.json').read_text())
            self.assertEqual(saved,result)
            report=(out/'report.md').read_text()
            self.assertIn(result['cases'][0]['manifest_sha256'],report)
            self.assertIn('/latency/repeats/1/latency_s/p95',report)
            self.assertNotIn('Review this measurement.',report)
            self.assertEqual(json.loads((out/'interpretation.json').read_text())['status'],'unverified_model_interpretation')
            with self.assertRaises(ValueError):
                export_comparison([a],Path(root)/'bad',interpretation=[{'text':'invented','fact_refs':['missing']}])
            self.assertFalse((Path(root)/'bad').exists())
            with self.assertRaises(FileExistsError):export_comparison([a],out)

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'),'optional openpyxl not installed')
    def test_excel_has_numeric_facts_and_literal_untrusted_strings(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'=unsafe')
            out=Path(root)/'comparison';export_comparison([a],out,xlsx=True)
            workbook=openpyxl.load_workbook(out/'facts.xlsx',data_only=False)
            self.assertEqual(workbook.properties.modified.year,2000)
            rows=list(workbook['facts'].iter_rows())
            self.assertTrue(any(cell.value==3.85 for row in rows for cell in row))
            cells=[cell for row in rows for cell in row if cell.value=='=unsafe']
            self.assertTrue(cells)
            self.assertTrue(all(cell.data_type=='s' for cell in cells))
            second=Path(root)/'second';export_comparison([a],second,xlsx=True)
            self.assertEqual((out/'facts.xlsx').read_bytes(),(second/'facts.xlsx').read_bytes())

    def test_frozen_analysis_input_and_expected_manifest_digests_bind_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'a')
            checksum=hashlib.sha256((a/'case_manifest.json').read_bytes()).hexdigest()
            facts=compare_cases([a],analysis_input_ref='analysis:7',expected_manifests={'a':checksum})
            self.assertEqual(facts['analysis_input_ref'],'analysis:7')
            out=Path(root)/'output'
            manifest=export_comparison([a],out,analysis_input_ref='analysis:7',expected_manifests={'a':checksum})
            self.assertEqual(json.loads((out/'facts.json').read_text()),facts)
            self.assertEqual(manifest['analysis_input_ref'],'analysis:7')
            with self.assertRaises(ValueError):
                compare_cases([a],expected_manifests={'a':'0'*64})
            with self.assertRaises(ValueError):
                compare_cases([a],expected_manifests={})

    def test_reuse_aliases_preserve_coverage_without_multiplying_samples(self):
        with tempfile.TemporaryDirectory() as root:
            a=self.seal(root,'original')
            aliases={name:{'case_id':'original','source_delivery_ref':'original:1',
                           'projection_ref':'projection:'+name} for name in ('new-a','new-b')}
            result=compare_cases([a],case_aliases=aliases)
            self.assertEqual(len(result['cases']),1)
            self.assertEqual(result['cases'][0]['success_samples'],4)
            self.assertEqual(result['case_aliases'],aliases)
            out=Path(root)/'export'
            export_comparison([a],out,case_aliases=aliases,xlsx=True)
            self.assertEqual(json.loads((out/'facts.json').read_bytes()),result)
            self.assertIn('new-b',(out/'report.md').read_text())
            with self.assertRaises(ValueError):
                compare_cases([a],case_aliases={'bad':dict(aliases['new-a'],case_id='absent')})
