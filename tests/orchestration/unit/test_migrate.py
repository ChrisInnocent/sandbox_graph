"""issue #4：migrate() 必须同时建原生账本表与 LangGraph checkpointer 表。"""
import contextlib

import sandbox_graph.persistence.repository as R


def test_migrate_provisions_native_ledger_and_checkpointer_tables(monkeypatch):
    calls = {}

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def transaction(self): return contextlib.nullcontext()
        def execute(self, sql, *a, **k): calls['migration_sql'] = sql

    monkeypatch.setattr(R.psycopg, 'connect', lambda *a, **k: FakeConn())

    import langgraph.checkpoint.postgres as LG

    class FakeSaver:
        def __init__(self, conn): calls['saver_conn'] = conn
        def setup(self): calls['saver_setup'] = True

    monkeypatch.setattr(LG, 'PostgresSaver', FakeSaver)
    R.PostgresRepository('postgresql://ignored/example').migrate()
    assert 'migration_sql' in calls
    assert calls.get('saver_setup') is True
