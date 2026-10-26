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
    changes=sorted(path for path in (ROOT/'openspec/changes').iterdir() if path.is_dir() and path.name!='archive')
    files = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md"),
             *(path for change in changes for path in change.rglob("*.md"))]
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
    tasks, invalid_task_lines, by_change = [], [], {}
    for change in changes:
        current=[];invalid=[]
        task_file=change/'tasks.md'
        if not task_file.is_file():
            invalid.append({'line':0,'text':'missing tasks.md'})
        else:
            for line_number,line in enumerate(task_file.read_text().splitlines(),1):
                match=re.fullmatch(r"- \[([ x])\] (\d+\.\d+) \S.*",line)
                if match:current.append(match.groups())
                elif re.match(r"\s*[-*+]\s*(?:\[|\d+\.\d+(?:\s|$))",line):
                    invalid.append({'line':line_number,'text':line})
        ids=[task for _,task in current]
        by_change[change.name]={'task_count':len(current),'unique_task_ids':len(set(ids)),
            'completed_task_ids':[task for state,task in current if state=='x'],
            'invalid_task_lines':invalid,'valid':bool(current) and not invalid and len(ids)==len(set(ids))}
        tasks.extend((state,change.name+':'+task if len(changes)>1 else task) for state,task in current)
        invalid_task_lines.extend((dict(row,change=change.name) if len(changes)>1 else row) for row in invalid)
    identifiers=[task for _,task in tasks]
    nodes = standard_registry().nodes
    packages = ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres",
                "psycopg", "pydantic", "pytest", "openpyxl")
    result = {
        "changes":by_change, "markdown_files": len(files), "local_links_checked": links, "broken_links": broken,
        "task_count": len(tasks), "unique_task_ids": len(set(identifiers)),
        "invalid_task_lines": invalid_task_lines,
        "completed_task_ids": [task for state, task in tasks if state == "x"],
        "registered_nodes": len(nodes), "python": sys.version.split()[0],
        "kernel": os.uname().sysname,
        "packages": {package: importlib.metadata.version(package) for package in packages},
    }
    if args.openspec_bin:
        validations={}
        for change in changes:
            validation=subprocess.run([args.openspec_bin,'validate',change.name,'--strict'],
                cwd=ROOT,capture_output=True,text=True,timeout=60)
            validations[change.name]={'exit_code':validation.returncode,'output':validation.stdout+validation.stderr}
        result['openspec']={'exit_code':max((abs(row['exit_code']) for row in validations.values()),default=1),
            'changes':validations,'output':'\n'.join(row['output'] for row in validations.values())}
    else:
        result["openspec"] = {"exit_code": None, "output": "OpenSpec CLI unavailable"}
    result["valid"] = (not broken and bool(tasks) and all(row["valid"] for row in by_change.values()) and not invalid_task_lines
                       and len(tasks) == len(set(identifiers))
                       and len(nodes) == 45 and result["openspec"]["exit_code"] == 0)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
