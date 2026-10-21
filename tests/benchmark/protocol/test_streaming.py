import base64,hashlib,json,struct
import sandbox_bench.transport as transport
import pytest


def frame(value,flag=0):
    raw=json.dumps(value).encode();return bytes([flag])+struct.pack('>I',len(raw))+raw


def test_streaming_split_frames_and_raw_output_hash():
    assert hasattr(transport,'ConnectDecoder'),'incremental decoder missing'
    events=[];d=transport.ConnectDecoder(on_event=events.append)
    raw=b'\xff\xe4\xbd\xa0'
    message=frame({'event':{'start':{'pid':12}}})+frame({'event':{'data':{'stdout':base64.b64encode(raw).decode()}}})+frame({'event':{'end':{'exited':True}}})+frame({},2)
    for byte in message:d.feed(bytes([byte]))
    result=d.finish()
    assert result['stdout_bytes']==4
    assert result['stdout_sha256']==hashlib.sha256(raw).hexdigest()
    assert result['exit_code']==0
    assert events==['start','stdout','end','stream_end']


def test_stream_does_not_succeed_without_both_end_events():
    assert hasattr(transport,'ConnectDecoder'),'incremental decoder missing'
    for message in [frame({'event':{'start':{'pid':12}}})+frame({},2),
                    frame({'event':{'end':{'exited':True}}}),
                    frame({'event':{'end':{'exited':True}}})+frame({'error':{'code':'internal'}},2)]:
        with pytest.raises(transport.TransportError):
            d=transport.ConnectDecoder();d.feed(message);d.finish()
