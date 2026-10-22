#!/usr/bin/env python3
"""Dry run：证明图编排能起(子)agent 干活。

链路：真图/真worker → 节点失败 → `agent_supervision` → `supervision` job →
owner agent 的 `AgentSupervisor` → `AgentGateway`(受控上下文) → 一个 **stub 子 agent**
（模拟 LLM：只读上下文、回结构化提案、不执行/不写入）→ 提案经校验落 `agent_reviews`。

真模型实现只需把 stub 换成真实 `ModelGateway`（HTTP / 本地模型），其余代码不变。

用法：.venv/bin/python scripts/agent_dry_run.py
"""
import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pipeline import run_mock

EVIDENCE = 'dry-run:evidence'


class StubSubAgent:
    """模拟一个 LLM 子 agent：只读入参上下文，回结构化提案，无副作用。"""

    def __init__(self):
        self.calls = []

    async def diagnose(self, context):
        self.calls.append(context)
        return {
            'facts': [{'statement': f"{context['node_id']} 在 {context['scope']} 未成功，需复核",
                       'evidence_refs': [EVIDENCE]}],
            'hypotheses': ['可能是环境/接口未就绪，或节点合同待澄清'],
            'suggested_actions': [],            # 不许请求未注册动作
            'affected_scope': [context['scope']],
            'required_decisions': [],
        }, 37, 'dry-run-subagent:v1'


def fields_for(node_id, record):
    return {'node_id': node_id, 'scope': record['scope'],
            'execution_status': record['execution_status'], 'reason': record.get('reason'),
            'receipt_ref': record['receipt_ref']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('agent-wiring-demo'))
    args = parser.parse_args()
    stub = StubSubAgent()
    summary = asyncio.run(run_mock(groups=2, works=4, requests=1, max_work_concurrency=2,
        fail_last_group=True, out=args.out.resolve(), model_gateway=stub,
        agent_allowed_fields={'node_id', 'scope', 'execution_status', 'reason', 'receipt_ref'},
        agent_fields_by_node=fields_for, agent_accessible_evidence={EVIDENCE}))

    print('=' * 78)
    print('Dry run：图编排 → 节点 agent → 子 agent')
    print('=' * 78)
    jobs = Counter((j['kind'], j['status']) for j in summary.get('jobs', []))
    print('jobs:', dict(sorted(jobs.items())))
    print('描述：每个 work 一个持久 work job（这就是派发给工作单元）。')

    reviews = summary.get('node_reviews', [])
    print(f"\n节点 agent 复审（node_reviews）: {len(reviews)} 条；"
          f"owner 级别分布 {dict(Counter(r['owner_agent_id'].split(':')[1] for r in reviews))}")
    print('  → 每个节点执行都由其 owner agent 复审并落账（确定性规则，无模型）。')

    print(f"\nagent_supervision（待复核）: {summary.get('agent_supervision')} 条")
    print('子 agent 被调用次数:', len(stub.calls))
    for call in stub.calls:
        print('  子 agent 入参上下文 =', {k: call[k] for k in ('scope', 'node_id', 'fields') if k in call})
    print('\nagent_reviews（子 agent 的提案落账）:')
    for decision in summary.get('agent_reviews', []):
        print(f"  owner={decision['owner_agent_id']}  status={decision['status']}")
        proposal = decision.get('proposal') or {}
        if proposal:
            print('    facts:', [f['statement'] for f in proposal['facts']])
            print('    hypotheses:', proposal['hypotheses'])
    print(f"\n产物目录: {args.out.resolve()}")
    print('说明：stub 只收到 allowlist 内的字段，且只回提案；它不能执行、不能改测量。')


if __name__ == '__main__':
    main()
