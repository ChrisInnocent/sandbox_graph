import json
from unittest.mock import patch
import pytest
from sandbox_bench.adapters import Adapter
from sandbox_bench.transport import Response


def config(product):
    return {'product':product,'api_url':'http://control','data_url':'http://data',
            'source':{'kind':'image','id':'repo@sha256:abc'},'create_body':{'timeout':100},
            'build_body':{'cpu':2000,'memory':2048}}


def response(value,status=200):
    return Response(status,{},json.dumps(value).encode(),.01,.02)


def test_cube_image_build_uses_returned_id_and_correct_status():
    a=Adapter(config('cubesandbox'))
    assert hasattr(a,'build_image'), 'build_image missing'
    calls=[]
    def request(method,url,**kw):
        calls.append((method,url,json.loads(kw['body']) if kw.get('body') else None))
        if method=='POST':return response({'jobID':'j','templateID':'actual'},202)
        return response({'status':'ready'})
    with patch('sandbox_bench.adapters.request',request):
        result=a.build_image('work',10,poll_interval=.01)
    assert result['source_id']=='actual'
    assert calls[0][2]['cpu']==2000
    assert calls[1][1].endswith('/templates/actual/builds/j/status')


def test_kuasar_build_distinguishes_registration_and_portable_id():
    a=Adapter(config('kuasar'));assert hasattr(a,'build_image'), 'build_image missing'
    values=iter([response({'templateID':'registration','buildID':'b'},202),response({},202),
                 response({'status':'ready','templateID':'e2b-img-persist','kind':'img'})])
    calls=[]
    def request(method,url,**kw):
        calls.append((method,url,kw.get('body')));return next(values)
    with patch('sandbox_bench.adapters.request',request):r=a.build_image('work',10,.01)
    assert r['source_id']=='e2b-img-persist'
    assert calls[0][1].endswith('/v3/templates')
    assert calls[1][1].endswith('/v2/templates/registration/builds/b')
    assert json.loads(calls[1][2])['fromImage']=='repo@sha256:abc'


def test_pause_export_and_partial_fork_are_not_full_success():
    a=Adapter(config('kuasar')); assert hasattr(a,'export_snapshot'), 'lifecycle mapping missing'
    with patch('sandbox_bench.adapters.request',return_value=response({'result':'e2b-snp-source'})) as req:
        assert a.export_snapshot({'sandbox_id':'source'},5)['source_id']=='e2b-snp-source'
        assert json.loads(req.call_args.kwargs['body'])['keepSource'] is True
    for product in ['agentenv','cubesandbox']:
        a=Adapter(config(product))
        with patch('sandbox_bench.adapters.request',return_value=response([
            {'sandbox':{'sandboxID':'child'}},{'error':{'code':'capacity'}}],201)):
            rows=a.fork({'sandbox_id':'source'},2,5)
        assert rows[0]['sandbox']['sandbox_id']=='child'
        assert rows[1]['error']['code']=='capacity'
    with pytest.raises(NotImplementedError):Adapter(config('kuasar')).fork({'sandbox_id':'s'},2,5)
