#!/usr/bin/env python3
"""按笔记本规格定测试密度，并用 mock 压图。

密度来自真实资源：并发 = min(实例数, 核数/2, (总内存-预留)/每实例)。
本机内存已被占满时只对“图工程测试”有效；真沙箱需要先释放内存。

用法：.venv/bin/python scripts/laptop_density_run.py --instances 12
"""
import argparse
import asyncio
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pipeline import run_mock, WORK_NODES


def machine():
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:
        cores = os.cpu_count() or 1
    mem = {}
    try:
        for line in open('/proc/meminfo'):
            key = line.split(':')[0]
            if key in ('MemTotal', 'MemAvailable'):
                mem[key] = int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        load = float(open('/proc/loadavg').read().split()[0])
    except OSError:
        load = 0.0
    return {'total_bytes': mem.get('MemTotal', 0), 'free_bytes': mem.get('MemAvailable', 0),
            'cores': cores, 'load1': load}


def plan_density(m, *, instances, memory_gib, reserve_gib):
    per = memory_gib * 1024 ** 3
    mem_slots = max(1, int((m['total_bytes'] - reserve_gib * 1024 ** 3) // per))
    cpu_slots = max(1, m['cores'] // 2)
    concurrency = max(1, min(instances, mem_slots, cpu_slots))
    waves = math.ceil(instances / concurrency)
    return {'memory_slots': mem_slots, 'cpu_slots': cpu_slots, 'concurrency': concurrency, 'waves': waves}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--instances', type=int, default=12)
    ap.add_argument('--groups', type=int, default=3)
    ap.add_argument('--requests', type=int, default=3, help='每个工作单元的创建请求数')
    ap.add_argument('--vcpus', type=float, default=2)
    ap.add_argument('--memory-gib', type=float, default=2)
    ap.add_argument('--reserve-gib', type=float, default=8, help='系统+工具预留内存')
    ap.add_argument("--out", type=Path, default=Path("laptop-density-run"))
    ap.add_argument("--concurrency", type=int, default=0, help="覆盖自动并发上限（0=按机算）")
    args = ap.parse_args()

    m = machine()
    d = plan_density(m, instances=args.instances, memory_gib=args.memory_gib, reserve_gib=args.reserve_gib)
    if args.concurrency:
        d['concurrency'] = args.concurrency
        d['waves'] = math.ceil(args.instances / args.concurrency)
    print('=' * 74)
    print('笔记本规格与测试密度')
    print('=' * 74)
    print(f"CPU 核数        : {m['cores']}   load1={m['load1']:.2f}")
    print(f"总内存          : {m['total_bytes'] / 1024**3:.1f} GiB   当前可用 {m['free_bytes'] / 1024**3:.2f} GiB")
    print(f"单实例规格      : {args.vcpus:g} vCPU / {args.memory_gib:g} GiB")
    print(f"预留内存        : {args.reserve_gib:g} GiB")
    print(f"可并发槽位      : 内存 {d['memory_slots']} × CPU {d['cpu_slots']}  →  取 {d['concurrency']}")
    print(f"计划            : {args.instances} 个工作单元，{args.groups} 个环境组，"
          f"每组 {args.instances // args.groups} 个，{d['waves']} 波完成，每单元 {args.requests} 请求")
    print(f"并发上限        : max_work_concurrency = {d['concurrency']}")
    if m['free_bytes'] < args.memory_gib * 1024 ** 3:
        print('⚠️  当前可用内存不足以跑真实沙箱；下面是【图工程 mock 测试】，不是真实性能。')

    summary = asyncio.run(run_mock(out=args.out.resolve(), groups=args.groups, works=args.instances,
        requests=args.requests, max_work_concurrency=d['concurrency'],
        vcpus=args.vcpus, memory_bytes=int(args.memory_gib * 1024 ** 3)))

    delivered = len(summary.get('deliveries', []))
    print('\n' + '=' * 74)
    print('图工程执行结果（mock）')
    print('=' * 74)
    print(f"run_id      : {summary['run_id']}")
    print(f"worker ticks: {summary['ticks']}")
    kinds = {}
    for j in summary.get('jobs', []):
        kinds[(j['kind'], j['status'])] = kinds.get((j['kind'], j['status']), 0) + 1
    for (kind, state), n in sorted(kinds.items()):
        print(f"  job {kind:10s} {state:9s} x{n}")
    print(f"组交付 work : {delivered}")
    print('\n工作图 N00–N10 汇总状态（跨所有工作单元）:')
    for node in WORK_NODES:
        print(f"  {node}: {summary['status'].get('work', {}).get(node, '-')}")
    print('\n分析产物:')
    for a in summary.get('analyses', []):
        print(f"  files={a['files']}")
    print('归档:')
    for a in summary.get('archives', []):
        print(f"  status={a['status']} artifact_refs={len(a['artifact_refs'])}")
    print(f"\n落盘目录: {args.out.resolve()}")
    print('实时看图: .venv/bin/python scripts/graph_server.py  → http://127.0.0.1:8770')


if __name__ == '__main__':
    main()
