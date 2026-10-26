#!/usr/bin/env python3
"""测试规格矩阵：按 规格 × 并发 C × 请求总量 Q 跑一格一格，输出对比表。

轴依据：[RT-19](openspec/changes/sandbox-evaluation-workflow/specs/runtime-evaluation/spec.md)
（沙箱规格作为独立实验条件）、RT-44/45（指标/条件矩阵）、`scenario-spec.md`（逐场景规格）。
本脚本用离线 mock 跑（合成样本，真图/真调度/真证据），用于验证矩阵编排与产物，**不是真实性能**。

用法：
  .venv/bin/python scripts/matrix_run.py --quick            # 小切片（默认）
  .venv/bin/python scripts/matrix_run.py --specs 2v2,2v4,4v8 --concurrency 1,2,4 --requests 3,10
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pipeline import run_mock
from sandbox_bench.conditions import parse_spec_label


async def one(cell, *, base, out):
    summary = await run_mock(requests=cell['requests'], concurrency=cell['concurrency'],
                             vcpus=cell['vcpus'], memory_bytes=cell['memory_bytes'], out=out, **base)
    case = json.loads(sorted(Path(out).glob('case-evidence/**/summary.json'))[0].read_text())
    repeat = (case.get('latency', {}).get('repeats', {}) or {}).get('1', {})
    return {'spec': cell['spec'], 'concurrency': cell['concurrency'], 'requests': cell['requests'],
            'work_status': summary['status']['work'].get('N05', '-'),
            'latency_mean_s': (repeat.get('latency_s') or {}).get('mean'),
            'success_samples': repeat.get('n_success'),
            'resources_status': case.get('resources', {}).get('status')}


async def run(args):
    if args.profile is not None:
        from sandbox_bench.conditions import load_profile
        profile = load_profile(args.profile)
        print(f'条件档: {profile.summary()}')
        cells = profile.cells()
        base = {'groups': profile.groups, 'works': profile.total_works,
                'max_work_concurrency': profile.max_work_concurrency,
                'fail_last_group': profile.fail_last_group}
    else:
        specs = [parse_spec_label(s) for s in args.specs.split(',')]
        concs = [int(c) for c in args.concurrency.split(',')]
        reqs = [int(q) for q in args.requests.split(',')]
        cells = [{'spec': args.specs.split(',')[i], 'vcpus': s[0], 'memory_bytes': s[1],
                  'concurrency': c, 'requests': q}
                 for i, s in enumerate(specs) for c in concs for q in reqs]
        base = {'groups': 1, 'works': 1, 'max_work_concurrency': 1}
    print(f'矩阵：{len(cells)} 格（离线 mock）')
    results = []
    for n, cell in enumerate(cells, 1):
        out = Path(args.out) / f"{cell['spec']}_c{cell['concurrency']}_q{cell['requests']}"
        row = await one(cell, base=base, out=out)
        results.append(row)
        print(f"  [{n}/{len(cells)}] {row['spec']:6s} C={row['concurrency']} Q={row['requests']:3d} "
              f"N05={row['work_status']} latency_mean={row['latency_mean_s']} "
              f"success={row['success_samples']} resources={row['resources_status']}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / 'matrix.json').write_text(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\n矩阵结果: {Path(args.out) / 'matrix.json'}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--specs', default='2v2g,2v4g')
    ap.add_argument('--concurrency', default='1,2,4')
    ap.add_argument('--requests', default='3')
    ap.add_argument('--out', default='./matrix-run')
    ap.add_argument('--profile', type=Path, default=None,
                    help='外部条件文件（yaml/json）；给定则用它展开矩阵，忽略 --specs/--concurrency/--requests')
    ap.add_argument('--quick', action='store_true', help='小切片：2 规格 × 并发 1,2 × 请求 3')
    args = ap.parse_args()
    if args.profile is None and args.quick:
        args.specs, args.concurrency, args.requests = '2v2g,2v4g', '1,2', '3'
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
