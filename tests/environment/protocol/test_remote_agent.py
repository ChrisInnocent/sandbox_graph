import hashlib
import json
import os
from pathlib import Path

import pytest

from sandbox_graph.environment.remote_agent import RemoteAgent, RemoteAgentError, load_execution_grant_verifier
from sandbox_graph.environment.permits import ExecutionGrantIssuer, GrantLedger, PermitVerifier
from sandbox_graph.environment.scripts.authorized_key import validate_public_key

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA bench"


def test_remote_agent_accepts_only_registered_read_action_and_emits_structured_result(tmp_path):
    (tmp_path/"machine-id").write_text("host-1\n")
    (tmp_path/"boot-id").write_text("boot-1\n")
    agent=RemoteAgent(home=tmp_path, proc_root=tmp_path, machine_id_file=tmp_path/"machine-id")
    result=agent.dispatch({"action":"probe.access.v1","input":{"read_only":True}})
    assert result["machine_id"]=="host-1"
    assert result["boot_id"]=="boot-1"
    with pytest.raises(RemoteAgentError,match="unregistered"):
        agent.dispatch({"action":"shell.run","input":{"command":"id"}})


def test_remote_agent_key_action_uses_cas_and_preserves_existing_file(tmp_path):
    (tmp_path/"machine-id").write_text("host-1\n")
    (tmp_path/".ssh").mkdir(mode=0o700)
    old=b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB old\n"
    (tmp_path/".ssh/authorized_keys").write_bytes(old)
    agent=RemoteAgent(home=tmp_path, proc_root=tmp_path, machine_id_file=tmp_path/"machine-id", authorize=lambda *_: True)
    target=tmp_path/".ssh/authorized_keys"
    expected=hashlib.sha256(old).hexdigest()
    payload={"action":"ssh.authorized_key.ensure.v1","grant":{"grant_id":"g1"},"input":{
        "node_id":"n1","machine_id":"host-1","public_key":validate_public_key(KEY)[0],
        "expected_fingerprint":validate_public_key(KEY)[1],"expected_sha256":expected,
        "preserve_existing":True,"compare_and_swap":True}}
    result=agent.dispatch(payload)
    assert result["changed"] is True
    assert target.read_bytes().startswith(old)
    again=agent.dispatch({**payload,"input":{**payload["input"],"expected_sha256":hashlib.sha256(target.read_bytes()).hexdigest()}})
    assert again["changed"] is False


def test_remote_agent_mutation_fails_closed_without_execution_grant(tmp_path):
    (tmp_path/"machine-id").write_text("host-1\n")
    agent=RemoteAgent(home=tmp_path, proc_root=tmp_path, machine_id_file=tmp_path/"machine-id")
    with pytest.raises(RemoteAgentError,match="grant"):
        agent.dispatch({"action":"ssh.authorized_key.ensure.v1","input":{}})


def test_execution_grant_binds_machine_action_input_expiry_and_resource_generation(tmp_path):
    key=b"local-test-grant-key-material-123456789"
    issuer=ExecutionGrantIssuer(key,clock=lambda:100.0)
    ledger=GrantLedger(tmp_path/"grant-state.json",clock=lambda:100.0)
    verifier=PermitVerifier(issuer,ledger,machine_id="host-1")
    action="ssh.authorized_key.ensure.v1"
    data={"node_id":"n1","machine_id":"host-1","public_key":KEY,
        "expected_fingerprint":"SHA256:test","expected_sha256":None,
        "preserve_existing":True,"compare_and_swap":True}
    grant=issuer.issue(operation_id="op1",step_id="step1",run_id="run1",thread_id="t1",
        owner_epoch=1,machine_id="host-1",resource_generations={"host:host-1":1},
        action=action,input_data=data,ttl_seconds=20)
    assert verifier.authorize(action,grant.model_dump(mode="json"),data)
    assert not verifier.authorize(action,grant.model_dump(mode="json"),data)
    other=issuer.issue(operation_id="op2",step_id="step2",run_id="run1",thread_id="t1",
        owner_epoch=2,machine_id="host-1",resource_generations={"host:host-1":1},
        action=action,input_data=data,ttl_seconds=20)
    assert not verifier.authorize(action,other.model_dump(mode="json"),data)


def test_expired_delayed_or_input_mismatched_grant_is_rejected(tmp_path):
    key=b"local-test-grant-key-material-123456789"
    clock=[100.0]
    issuer=ExecutionGrantIssuer(key,clock=lambda:clock[0])
    ledger=GrantLedger(tmp_path/"state.json",clock=lambda:clock[0])
    verifier=PermitVerifier(issuer,ledger,machine_id="host-1")
    data={"target":"owned"}
    grant=issuer.issue(operation_id="op",step_id="s",run_id="r",thread_id="t",owner_epoch=1,
        machine_id="host-1",resource_generations={"host:host-1":1},action="config.write.v1",
        input_data=data,ttl_seconds=5)
    clock[0]=106.0
    assert not verifier.authorize("config.write.v1",grant.model_dump(mode="json"),data)
    assert not verifier.authorize("config.write.v1",grant.model_dump(mode="json"),{"target":"other"})


def test_remote_agent_protocol_rejects_extra_and_malformed_fields(tmp_path):
    agent=RemoteAgent(home=tmp_path, proc_root=tmp_path, machine_id_file=tmp_path/"machine-id")
    with pytest.raises(RemoteAgentError,match="shape"):
        agent.dispatch({"action":"probe.access.v1","input":{},"command":"id"})


def test_remote_grant_key_must_be_private_and_machine_bound(tmp_path):
    machine=tmp_path/"machine-id";machine.write_text("host-1\n")
    key_file=tmp_path/"grant.key";key_file.write_bytes(b"g"*32);key_file.chmod(0o600)
    verifier=load_execution_grant_verifier(home=tmp_path,machine_id_file=machine,key_file=key_file)
    assert verifier is not None and verifier.machine_id=="host-1"
    key_file.chmod(0o644)
    with pytest.raises(RemoteAgentError,match="permissions"):
        load_execution_grant_verifier(home=tmp_path,machine_id_file=machine,key_file=key_file)


def test_operation_journal_actions_require_authorization_and_reconcile_input(tmp_path):
    from sandbox_graph.environment.journal import OperationJournal
    from sandbox_graph.environment.remote_agent import RemoteAgent, RemoteAgentError
    accepted=[]
    def authorize(action, grant, data):
        accepted.append((action, data))
        return grant == {"valid": True}
    journal=OperationJournal(tmp_path/'operations.json',machine_id='machine-1',boot_id='boot-1')
    agent=RemoteAgent(home=tmp_path,machine_id_file=tmp_path/'machine-id',authorize=authorize,journal=journal)
    action_input={"operation_id":"op-9","action":"cube.service.start.v1","input":{"unit":"cubelet.service"}}
    with pytest.raises(RemoteAgentError,match='grant'):
        agent.dispatch({"action":"operation.journal.accept.v1","input":action_input})
    accepted_record=agent.dispatch({"action":"operation.journal.accept.v1","input":action_input,"grant":{"valid":True}})
    assert accepted_record['state']=='accepted'
    assert accepted[-1][0]=='operation.journal.accept.v1'


def test_operation_journal_transition_requires_registered_grant_each_time(tmp_path):
    from sandbox_graph.environment.journal import OperationJournal
    from sandbox_graph.environment.remote_agent import RemoteAgent
    (tmp_path/'machine-id').write_text('machine-1')
    journal=OperationJournal(tmp_path/'operations.json',machine_id='machine-1',boot_id='boot-1')
    agent=RemoteAgent(home=tmp_path,machine_id_file=tmp_path/'machine-id',
        authorize=lambda action,grant,data: grant=={'valid':True},journal=journal)
    agent.dispatch({'action':'operation.journal.accept.v1','input':{'operation_id':'op','action':'a.v1','input':{}},'grant':{'valid':True}})
    started=agent.dispatch({'action':'operation.journal.transition.v1','input':{'operation_id':'op','state':'started','process_identity':{'pid':2,'start_ticks':8}},'grant':{'valid':True}})
    assert started['state']=='started' and started['boot_id']=='boot-1'
