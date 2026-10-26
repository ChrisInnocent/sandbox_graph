import base64
import hashlib
import json
from pathlib import Path
import pytest
from sandbox_graph.environment.journal import OperationJournal


def setup(tmp_path):
    journal=OperationJournal(tmp_path/'state/operations.json',machine_id='host',boot_id='boot')
    row=journal.accept(operation_id='original',action='product.recipe.step.v1',input_data={'recipe':'one'})
    journal.transition('original',state='started',process_identity={'pid':1,'start_ticks':2})
    logs=tmp_path/'state/private-logs';logs.mkdir(mode=0o700)
    path=logs/'owned.stdout';raw=b'private-value\n'*100;path.write_bytes(raw);path.chmod(0o600)
    digest=hashlib.sha256(raw).hexdigest()
    journal.transition('original',state='completed',result={'status':'completed','logs':[{
        'private_path':str(path),'sha256':digest,'bytes':len(raw),'truncated':False}]})
    data={'operation_id':'original','input_hash':row['input_hash'],'stream':0,'sha256':digest,'offset':0,'limit':64}
    return journal,path,raw,data


def test_export_is_original_terminal_log_and_byte_bounded(tmp_path):
    from sandbox_graph.environment.log_export import read_chunk
    journal,path,raw,data=setup(tmp_path)
    value=read_chunk(journal,data)
    assert base64.b64decode(value['data'])==raw[:64]
    assert value['next_offset']==64 and value['size_bytes']==len(raw) and not value['eof']
    assert str(path) not in json.dumps(value)
    data['offset']=len(raw)
    assert read_chunk(journal,data)['eof']


@pytest.mark.parametrize('fault',['foreign','digest','offset','limit','symlink','mode','drift'])
def test_log_export_rejects_foreign_or_changed_private_material(tmp_path,fault):
    from sandbox_graph.environment.log_export import read_chunk
    journal,path,raw,data=setup(tmp_path)
    if fault=='foreign':data['input_hash']='a'*64
    elif fault=='digest':data['sha256']='b'*64
    elif fault=='offset':data['offset']=-1
    elif fault=='limit':data['limit']=2**20
    elif fault=='mode':path.chmod(0o644)
    elif fault=='drift':path.write_bytes(raw+b'changed')
    else:
        other=tmp_path/'secret';other.write_bytes(raw);path.unlink();path.symlink_to(other)
    with pytest.raises((ValueError,PermissionError)):read_chunk(journal,data)


@pytest.mark.asyncio
async def test_export_client_verifies_chunks_and_publishes_private_file(tmp_path):
    from sandbox_graph.environment.log_export import download,read_chunk
    journal,path,raw,data=setup(tmp_path)
    seen=[]
    async def fetch(request):seen.append(request);return read_chunk(journal,request)
    target=tmp_path/'download'
    receipt=await download(fetch,data,output=target,machine_id='host')
    assert target.read_bytes()==raw and target.stat().st_mode&0o777==0o600
    assert receipt['sha256']==hashlib.sha256(raw).hexdigest() and len(seen)>1
    with pytest.raises(FileExistsError):await download(fetch,data,output=target,machine_id='host')
    async def bad(request):return read_chunk(journal,request)|{'machine_id':'another-host'}
    with pytest.raises(ValueError):await download(bad,data,output=tmp_path/'bad',machine_id='host')
    assert not (tmp_path/'bad').exists()
