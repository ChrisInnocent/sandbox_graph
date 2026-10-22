#!/usr/bin/env python3
"""本地 mock 全链路跑图（CLI 版，跑完落盘）。

实时可视化查看请用 `scripts/graph_server.py`。
用法：.venv/bin/python scripts/local_mock_run.py [--out ./local-mock-run]
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pipeline import run_mock

WORK_NODES = tuple(f'N{i:02}' for i in range(11))


def _print(summary, out):
    print('=' * 78)
    print('本地 mock 全链路跑图结果（非真实环境验证）')
    print('=' * 78)
    print(f"run_id      : {summary['run_id']}")
    print(f"plan_hash   : {summary['plan_hash']}")
    print(f"worker ticks: {summary['ticks']}")
    print(f"artifact store: {summary['artifact_store']}")
    print('\n-- 工作图 N00-N10 --')
    for node in WORK_NODES:
        st = summary['status'].get('work', {}).get(node, '-')
        print(f"  {node}: {st}")
    print('\n-- 分析产物 --')
    for a in summary.get('analyses', []):
        print(f"  analysis_ref={a['analysis_ref'][:12]}… files={a['files']}")
    print('\n-- 归档（F4） --')
    for a in summary.get('archives', []):
        print(f"  status={a['status']} artifact_refs={len(a['artifact_refs'])} 个")
    print('\n-- 落盘文件 --')
    for path in summary.get('written', {}).get('case_evidence', []):
        print(f"  [证据] {path}")
    for path in summary.get('written', {}).get('exports', []):
        print(f"  [分析] {path}")
    print(f'\n产物根目录: {out}')
    print('实时查看: .venv/bin/python scripts/graph_server.py  → http://127.0.0.1:8770')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('local-mock-run'))
    args = parser.parse_args()
    out = args.out.resolve()
    summary = asyncio.run(run_mock(out=out))
    _print(summary, out)


if __name__ == '__main__':
    main()
