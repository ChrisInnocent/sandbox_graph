import json
from pathlib import Path
import pytest
from sandbox_graph.testing.critical_mutations import MUTATIONS,caught,patch


def test_each_fixed_mutation_matches_current_source_and_preserves_baseline(tmp_path):
    root=Path(__file__).resolve().parents[3]
    for row in MUTATIONS:
        target=tmp_path/row[1];target.parent.mkdir(parents=True,exist_ok=True);target.write_text((root/row[1]).read_text())
        patch(tmp_path,row)
        compile(target.read_text(),str(target),'exec')
        assert row[3] in target.read_text()
        assert target.read_text()!=(root/row[1]).read_text()


@pytest.mark.parametrize('fault',['skip','xfail','setup','teardown','zero','collection','exit','duplicate'])
def test_invalid_mutation_execution_never_counts_as_caught(fault):
    node='test.py::test_assertion'
    value={'collected':[node],'phases':[{'nodeid':node,'when':p,'outcome':'failed' if p=='call' else 'passed'} for p in ('setup','call','teardown')]}
    assert caught(value,node,1)
    code=1
    if fault=='skip':value['phases'][1]['outcome']='skipped'
    elif fault=='xfail':value['phases'][1]['wasxfail']=True
    elif fault in {'setup','teardown'}:next(r for r in value['phases'] if r['when']==fault)['outcome']='failed'
    elif fault=='zero':value['collected']=[]
    elif fault=='collection':value['collection_errors']=['import failure']
    elif fault=='exit':code=2
    else:value['phases'].append(value['phases'][1])
    assert not caught(value,node,code)
