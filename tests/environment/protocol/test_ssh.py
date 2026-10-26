import asyncio

import pytest

from sandbox_graph.environment.transport import SSHRequest, SSHTransport, build_ssh_argv


def test_ssh_argv_requires_strict_host_checking_and_fixed_remote_command(tmp_path):
    known = tmp_path / "known_hosts"
    identity = tmp_path / "id_ed25519"
    known.write_text("host-key")
    identity.write_text("private-key")
    identity.chmod(0o600)
    argv = build_ssh_argv(address="192.0.2.3", port=2222, user="bench", known_hosts=known, identity_file=identity)
    assert "StrictHostKeyChecking=yes" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "UserKnownHostsFile=" + str(known) in argv
    assert argv[-2:] == ["bench@192.0.2.3", "sandbox-graph-remote-agent --stdio-json"]
    assert "private-key" not in " ".join(argv)


def test_ssh_argv_rejects_host_or_user_shell_content(tmp_path):
    known = tmp_path / "known_hosts"
    identity = tmp_path / "id"
    known.touch(); identity.touch()
    with pytest.raises(ValueError, match="host"):
        build_ssh_argv(address="host;id", port=22, user="bench", known_hosts=known, identity_file=identity)
    with pytest.raises(ValueError, match="user"):
        build_ssh_argv(address="host.example", port=22, user="x -oProxyCommand=id", known_hosts=known, identity_file=identity)


def test_bootstrapped_helper_path_is_used_for_both_fixed_protocols(tmp_path):
    known=tmp_path/'known';known.touch()
    identity=tmp_path/'identity';identity.touch();identity.chmod(0o600)
    for command in ('sandbox-graph-remote-agent --stdio-json','sandbox-graph-remote-agent --stdio-grant-v1'):
        argv=build_ssh_argv(address='host.example',port=22,user='bench',known_hosts=known,
            identity_file=identity,remote_command=command,helper_path='/opt/helper/bin/sandbox-graph-remote-agent')
        assert argv[-1]=='/opt/helper/bin/sandbox-graph-remote-agent '+command.split()[1]
    for path in ('relative','/opt/../bad','/opt/agent;id','/opt//agent'):
        with pytest.raises(ValueError):
            build_ssh_argv(address='host.example',port=22,user='bench',known_hosts=known,
                identity_file=identity,helper_path=path)


def test_transport_sends_structured_input_and_bounds_output(tmp_path):
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text("#!/bin/sh\ncat\n")
    fake_ssh.chmod(0o700)
    known = tmp_path / "known_hosts"; known.write_text("test")
    identity = tmp_path / "identity"; identity.write_text("private")
    identity.chmod(0o600)
    transport = SSHTransport(executable=str(fake_ssh), max_output_bytes=256)
    request = SSHRequest(
        address="host.example", port=22, user="bench", known_hosts=known,
        identity_file=identity, payload={"action":"probe.host.v1", "input":{"arg":"$(touch /tmp/pwned)"}},
    )
    result = asyncio.run(transport.request(request))
    assert result.stdout == b'{"action":"probe.host.v1","input":{"arg":"$(touch /tmp/pwned)"}}'
    assert result.returncode == 0


def test_transport_rejects_oversized_output(tmp_path):
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text("#!/bin/sh\nprintf '1234567890'\n")
    fake_ssh.chmod(0o700)
    known = tmp_path / "known_hosts"; known.write_text("test")
    identity = tmp_path / "identity"; identity.write_text("private")
    identity.chmod(0o600)
    transport = SSHTransport(executable=str(fake_ssh), max_output_bytes=4)
    request = SSHRequest(address="host.example", port=22, user="bench", known_hosts=known, identity_file=identity, payload={})
    with pytest.raises(ValueError, match="output limit"):
        asyncio.run(transport.request(request))


@pytest.mark.asyncio
async def test_transport_routes_probe_and_grant_to_same_bootstrapped_entry(tmp_path):
    import sys,json
    captured=tmp_path/'argv'
    fake=tmp_path/'ssh'
    fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\n'
        'Path('+repr(str(captured))+').write_text(sys.argv[-1])\n'
        'sys.stdin.buffer.readline()\n'
        'print(json.dumps({"observation":{"state":"completed"}} if sys.argv[-1].endswith("--stdio-grant-v1") else {"machine_id":"synthetic"}))\n')
    fake.chmod(0o700)
    known=tmp_path/'known';known.touch()
    identity=tmp_path/'id';identity.touch();identity.chmod(0o600)
    path='/opt/bootstrap/bin/sandbox-graph-remote-agent'
    transport=SSHTransport(executable=str(fake),helper_paths={'example.invalid':path})
    request=SSHRequest(address='example.invalid',port=22,user='bench',known_hosts=known,identity_file=identity,payload={})
    assert json.loads((await transport.request(request)).stdout)=={'machine_id':'synthetic'}
    assert captured.read_text()==path+' --stdio-json'
    def forbidden(*args):raise AssertionError('completed journal needs no new grant')
    assert await transport.request_granted(request,forbidden)=={'state':'completed'}
    assert captured.read_text()==path+' --stdio-grant-v1'
