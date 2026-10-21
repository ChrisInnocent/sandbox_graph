"""Read-only planning checks; emit a reproducible JSON verification record."""
from pathlib import Path
import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sandbox_graph.catalog.registry import standard_registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--openspec-bin", default=shutil.which("openspec"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    files = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md"),
             *(ROOT / "openspec/changes/sandbox-evaluation-workflow").rglob("*.md")]
    broken, links = [], 0
    for path in sorted(files):
        for raw in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            raw = raw.strip().strip("<>")
            if raw.startswith(("http:", "https:", "mailto:", "#", "app:")):
                continue
            target = unquote(raw.split("#")[0])
            if not target:
                continue
            links += 1
            if not (path.parent / target).exists():
                broken.append({"document": str(path.relative_to(ROOT)), "target": raw})
    task_text = (ROOT / "openspec/changes/sandbox-evaluation-workflow/tasks.md").read_text()
    tasks = re.findall(r"^- \[([ x])\] (\d+\.\d+) ", task_text, re.M)
    identifiers = [task for _, task in tasks]
    nodes = standard_registry().nodes
    packages = ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres",
                "psycopg", "pydantic", "pytest", "openpyxl")
    result = {
        "markdown_files": len(files), "local_links_checked": links, "broken_links": broken,
        "task_count": len(tasks), "unique_task_ids": len(set(identifiers)),
        "completed_task_ids": [task for state, task in tasks if state == "x"],
        "registered_nodes": len(nodes), "python": sys.version.split()[0],
        "kernel": os.uname().sysname,
        "packages": {package: importlib.metadata.version(package) for package in packages},
    }
    if args.openspec_bin:
        validation = subprocess.run(
            [args.openspec_bin, "validate", "sandbox-evaluation-workflow", "--strict"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        result["openspec"] = {"exit_code": validation.returncode,
                              "output": validation.stdout + validation.stderr}
    else:
        result["openspec"] = {"exit_code": None, "output": "OpenSpec CLI unavailable"}
    result["valid"] = (not broken and len(tasks) == len(set(identifiers)) == 96
                       and len(nodes) == 45 and result["openspec"]["exit_code"] == 0)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
