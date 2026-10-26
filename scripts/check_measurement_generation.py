"""Run the local measurement-generation regression gate; not a release gate."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
TESTS = (
    "tests/orchestration/unit/test_measurement_generation.py",
    "tests/orchestration/graphs/test_measurement_generation.py",
    "tests/orchestration/graphs/test_delivery_rework.py",
    "tests/orchestration/graphs/test_work_remediation.py",
    "tests/orchestration/graphs/test_group_delivery.py",
    "tests/orchestration/unit/test_lifecycle_dispatch.py",
    "tests/orchestration/unit/test_runner_protocol.py",
    "tests/orchestration/unit/test_revision_drain.py",
    "tests/orchestration/unit/test_revision_rollback.py",
    "tests/orchestration/unit/test_report_closure.py",
    "tests/orchestration/unit/test_review_closure_agents.py",
    "tests/orchestration/unit/test_lifecycle_closure.py",
    "tests/benchmark/unit/test_work_measurement.py",
    "tests/benchmark/unit/test_sampler_lifecycle.py",
    "tests/benchmark/unit/test_session.py",
    "tests/benchmark/integration/test_sampler_observation_bridge.py",
)


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "artifacts/gates/measurement-generation" / f"{stamp}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    junit = output / "junit.xml"
    command = [sys.executable, "-m", "pytest", *TESTS, "-q", "--tb=short",
               "-o", "addopts=--import-mode=importlib", f"--junitxml={junit}"]
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)  # Do not silently filter the gate's fixed suite.
    print(f"Gate evidence: {output}", flush=True)
    with (output / "pytest.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    print((output / "pytest.log").read_text(), end="")
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
        rejected = sum(any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                       for case in cases)
        passed = result.returncode == 0 and bool(cases) and rejected == 0
    except (OSError, ET.ParseError):
        cases, rejected, passed = [], 0, False
    summary = {"gate": "measurement-generation-local", "passed": passed,
               "pytest_exit_code": result.returncode, "tests": len(cases),
               "failed_error_or_skipped": rejected, "command": command}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Gate {'PASS' if passed else 'FAIL'}: {len(cases)} cases; evidence {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
