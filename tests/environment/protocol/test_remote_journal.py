import pytest
from sandbox_graph.environment.journal import OperationJournal, JournalConflict


def test_journal_reuses_same_identity_and_rejects_id_with_changed_input(tmp_path):
    journal=OperationJournal(tmp_path/'journal.json',machine_id='machine-1',boot_id='boot-1')
    created=journal.accept(operation_id='op-1',action='service.start.v1',input_data={'unit':'cubelet.service'})
    again=journal.accept(operation_id='op-1',action='service.start.v1',input_data={'unit':'cubelet.service'})
    assert created==again and created['state']=='accepted'
    with pytest.raises(JournalConflict,match='input identity'):
        journal.accept(operation_id='op-1',action='service.start.v1',input_data={'unit':'other.service'})


def test_journal_transitions_record_boot_and_process_and_reject_illegal_transition(tmp_path):
    journal=OperationJournal(tmp_path/'journal.json',machine_id='machine-1',boot_id='boot-1')
    journal.accept(operation_id='op-2',action='service.start.v1',input_data={})
    started=journal.transition('op-2',state='started',process_identity={'pid':123,'start_ticks':55})
    assert started['boot_id']=='boot-1' and started['process_identity']=={'pid':123,'start_ticks':55}
    done=journal.transition('op-2',state='completed',result={'exit_code':0})
    assert done['state']=='completed'
    with pytest.raises(JournalConflict,match='terminal'):
        journal.transition('op-2',state='started',process_identity={'pid':123,'start_ticks':55})


def test_corrupt_or_symlink_journal_fails_closed(tmp_path):
    path=tmp_path/'journal.json'
    path.write_text('{bad'); path.chmod(0o600)
    journal=OperationJournal(path,machine_id='m',boot_id='b')
    with pytest.raises(JournalConflict,match='corrupt'):
        journal.accept(operation_id='op',action='a.v1',input_data={})


def test_journal_state_survives_agent_restart_at_each_boundary(tmp_path):
    path=tmp_path/'operations.json'
    accepted=OperationJournal(path,machine_id='m-1',boot_id='boot-1').accept(
        operation_id='restart-op',action='service.restart.v1',input_data={'unit':'worker.service'})
    restarted=OperationJournal(path,machine_id='m-1',boot_id='boot-1')
    assert restarted.accept(operation_id='restart-op',action='service.restart.v1',
        input_data={'unit':'worker.service'})==accepted
    started=restarted.transition('restart-op',state='started',process_identity={'pid':44,'start_ticks':777})
    restarted_again=OperationJournal(path,machine_id='m-1',boot_id='boot-1')
    assert restarted_again.accept(operation_id='restart-op',action='service.restart.v1',
        input_data={'unit':'worker.service'})==started
    terminal=restarted_again.transition('restart-op',state='completed',result={'verified':True})
    assert OperationJournal(path,machine_id='m-1',boot_id='boot-1').accept(
        operation_id='restart-op',action='service.restart.v1',
        input_data={'unit':'worker.service'})==terminal
