"""Pytest phase receipts for local qualification, never a trusted attestation."""
import json
import os
from pathlib import Path

_state={'collected':[],'collection_errors':[],'phases':[]}


def pytest_collection_finish(session):
    _state['collected']=[item.nodeid for item in session.items]


def pytest_collectreport(report):
    if report.failed:
        _state['collection_errors'].append(report.nodeid)


def pytest_runtest_logreport(report):
    _state['phases'].append({'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,
        'wasxfail':bool(getattr(report,'wasxfail',False))})


def pytest_sessionfinish(session,exitstatus):
    _state['exitstatus']=int(exitstatus)
    target=Path(os.environ['SANDBOX_GATE_REPORT'])
    target.write_text(json.dumps(_state,sort_keys=True,indent=2)+'\n')
