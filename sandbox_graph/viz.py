"""图可视化：从真实图定义提取节点/边，渲染内联 SVG 与 Mermaid。

数据来源就是代码本身：
- run / group / analysis / report：`services/workflows.py` 的 `*_definition()`
- work：`NODE_GROUPS['work']` + `graphs/factory.py::build_work` 的跳转规则

供 `scripts/render_graphs.py`（静态文件）与 `scripts/graph_server.py`（实时）共用。
"""
from collections import defaultdict, deque
from html import escape

from .catalog.registry import NODE_GROUPS
from .services.workflows import (run_definition, group_definition,
                                 analysis_definition, report_definition)

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 178, 58, 66, 22, 30

# 节点中文职能（来源：openspec/changes/sandbox-evaluation-workflow/node-automation.md §3–§7）
NODE_LABELS = {
    'R0': '恢复运行/版本', 'R1': '校验目录与方法', 'RP': '选测与依赖规划', 'R2': '派发所选环境组',
    'R3': '接收组事件', 'R6': '生成有范围返工单', 'R7': '持久等待与恢复',
    'G0': '恢复本组状态', 'E1': '环境声明与连接', 'E2': '按角色核验组件',
    'C1': '形成配置计划', 'C2': '脚本化配置', 'C3': '独立复检',
    'T0': '绑定方法合同', 'T1': '展开有限用例矩阵', 'T2': '适配器预检',
    'T4': '冲突域与并发调度', 'T5': '派发工作节点子图', 'T6': '组二次验收',
    'T8': '组内分析与发布', 'TC': '登记能力缺口', 'TB': '登记待适配/待澄清',
    'GB': '记录组阻塞', 'GW': '等待修复或输入', 'GE': '发布组交付',
    'N00': '校验工作合同', 'N01': '对账与恢复', 'N02': '准备 fixture/来源',
    'N03': '功能预检与复位', 'N04': '启动观测与采样', 'N05': '执行测量（发压）',
    'N06': '后验与稳态窗口', 'N07': '封存原始证据', 'N08': '局部指标测算',
    'N09': '按归属清理资源', 'N10': '交付工作单元',
    'A1': '冻结分析输入清单', 'A2': '数据质量与可比性', 'A3': '统一指标测算',
    'A4': '同场景横向对比', 'A5': '差异复核', 'A7': '生成并校验 Excel',
    'F1': '分场景事实报告', 'F2': '校验事实/引用/覆盖', 'F4': '归档报告与证据',
}

STAGE_GROUPS = {
    'group': [('环境检查', ('E1', 'E2')), ('环境配置', ('C1', 'C2', 'C3')),
              ('指标测试', ('T0', 'T1', 'T2', 'T4', 'T5', 'T6', 'T8')),
              ('路由/收口', ('G0', 'TC', 'TB', 'GB', 'GW', 'GE'))],
    'work': [('前置', tuple(f'N{i:02}' for i in range(4))),
             ('测量', ('N04', 'N05', 'N06')),
             ('分析/清理/封存', ('N07', 'N08', 'N09', 'N10'))],
    'run': [('选测与调度', ('R0', 'R1', 'RP', 'R2')), ('事件与等待', ('R3', 'R6', 'R7'))],
}
STAGE_COLORS = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#db2777']

# 浏览器实时着色用
STATUS_COLORS = {
    'succeeded': '#bbf7d0', 'blocked': '#fecaca', 'failed': '#fecaca',
    'cancelled': '#e5e7eb', 'running': '#fde68a', 'pending': '#f8fafc',
}


def _from_definition(definition, title, key):
    routes = dict(definition.edges) | dict(definition.dynamic_routes)
    edges = [(src, (dst if dst != '__end__' else None)) for src, targets in routes.items() for dst in targets]
    return {'key': key, 'title': title, 'entry': definition.entry,
            'nodes': list(definition.nodes), 'edges': edges}


def work_graph():
    nodes = list(NODE_GROUPS['work'])
    edges = []
    for i, node in enumerate(nodes):
        if node == 'N10':
            continue
        nxt = nodes[i + 1] if i + 1 < len(nodes) else None
        if node in ('N07', 'N08'):
            edges.append((node, nxt if node == 'N07' else 'N09'))
            edges.append((node, 'N09'))
        else:
            edges.append((node, nxt))
            if node != 'N09':
                edges.append((node, 'N07'))
    return {'key': 'work', 'title': '工作节点子图 N00–N10（每个测试工作单元）',
            'entry': 'N00', 'nodes': nodes, 'edges': edges}


def collect():
    return [
        _from_definition(run_definition(), '主图：调度环境组与统一分析（R0–R7）', 'run'),
        _from_definition(group_definition(), '环境组子图：检查→配置→测试→收口（G/E/C/T）', 'group'),
        work_graph(),
        pipeline_graph(),
        _from_definition(analysis_definition(), '数据分析子图（A1–A7）', 'analysis'),
        _from_definition(report_definition(), '结论归档子图（F1–F4）', 'report'),
    ]


def pipeline_graph():
    """测量数据流：发压 → 完成观察 → 资源采集 → 指标。来自 load/measure/session/metrics。"""
    nodes = ['CASE', 'SCHED', 'ENGINE', 'OBS', 'SMP', 'MET', 'SEAL']
    edges = [('CASE', 'SCHED'), ('SCHED', 'ENGINE'), ('ENGINE', 'OBS'), ('OBS', 'MET'),
             ('SMP', 'MET'), ('ENGINE', 'SEAL'), ('SMP', 'SEAL'), ('MET', 'SEAL')]
    return {'key': 'pipeline', 'title': '测量数据流：怎么发压、怎么观察、怎么采集（N02–N08）',
            'entry': 'CASE', 'nodes': nodes, 'edges': edges}


# 每个节点的内部步骤（来源：session.py 的 _Nxx / load.py / measure.py / sampler.py / metrics/）
NODE_INTERNALS = {
    'N02': ['校验来源 kind/启动路径/规格/归属/TTL/缓存',
            '准备 fixture（来源产物、节点归属、资源登记）',
            '产出 fixture manifest；不支持目标则 blocked'],
    'N03': ['协议与真实功能样例核验',
            '复位与缓存影响核验（clean dirty 状态）',
            '断言与采集就绪预检；要改合同则 blocked'],
    'N04': ['写 observation_pending；审计 observation_started',
            '采基线样本 sampler.sample_once() → resource_samples/*.json',
            '启动 _sample_loop：每 resources.interval_s 采一次',
            '采样落 spool（SQLite）再 reconcile 到 sink；缺口记 resource_gap'],
    'N05': ['读用例 load 合同：mode/requests/concurrency/batch/gap/arrival_rate/run_timeout',
            '选调度模式：WINDOW（即时补槽）/ BATCH（批内限流+批间 gap）/ OPEN_LOOP（有限到达序列 drop|queue）',
            '构造 LoadSchedule + child_weights（B05 fork 按 child 占并发）',
            'LoadEngine.run：reserve（原子消费预算）→ submission_started（fencing+提交意图）→ 观察终点 → record_terminal',
            '完成终点由 CompletionObserver 轮询：API_COMPLETE / STATE_READY / EXEC_COMPLETE / SERVER_TERMINAL',
            'unknown 达上限/全局 deadline 停止新提交；在途按 operation_timeout drain',
            '写 load.json（events: scheduled/admitted/submit/completed/gap/stop）'],
    'N06': ['steady_check：在 max_wait_s 内判稳态窗口',
            '写 steady.json；不改变已记录测量事实'],
    'N07': ['停采（wait_for stop deadline）并 reconcile 尾样本',
            '刷新在存量、脱敏、完整性清单',
            '封存不可变原始清单 evidence/case_manifest.json'],
    'N08': ['schema/计数/时间域/覆盖检查',
            '局部指标：分位(latency)、吞吐(throughput)、PSS/CPU(resource_series)',
            '异常不删样本、不替代公式'],
    'N09': ['按所有权 cancel/delete/恢复', '核验残留、未知与失败，出清理账本'],
    'N10': ['交付引用、分离状态、覆盖、失败与恢复入口', '签 T6 复核'],
    'T4': ['冲突域/独占锁与租约；并发上限；有限重试', '产工作派发次序'],
    'T5': ['构造最小上下文 + 稳定工作身份', '派发 N00–N10 子图（持久 work job）'],
    'T6': ['验证清单/数据/日志/覆盖/清理闭合', '追加复核，不覆盖子事实'],
    'A3': ['从 N07/N08 封存的原始样本重算', '分位/吞吐/CPU/PSS/密度/误差', '公式与来源可溯源'],
    'A4': ['匹配同方法同条件', '差值/比例/方向/缺口，可比才比较', '不造总排名'],
    'A7': ['模板分表、数据/公式/图表', '单位/覆盖/引用校验', '重读核对后出版版本化 Excel'],
}

PIPELINE_LABELS = {
    'CASE': '用例负载合同', 'SCHED': '负载调度', 'ENGINE': '发压引擎',
    'OBS': '终点完成观察', 'SMP': '资源采样', 'MET': '指标汇总', 'SEAL': '封存证据',
}


def graph_nodes():
    """{graph_key: set(node)}，用于把 receipt 归到哪张图。"""
    return {g['key']: set(g['nodes']) for g in collect()}


def _adjacency(graph):
    adj = defaultdict(list)
    for src, dst in graph['edges']:
        if dst:
            adj[src].append(dst)
    return adj


def _depth(graph):
    adj = _adjacency(graph)
    depth = {graph['entry']: 0}
    queue = deque([graph['entry']])
    while queue:
        node = queue.popleft()
        for nxt in adj[node]:
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                queue.append(nxt)
    for node in graph['nodes']:
        depth.setdefault(node, max(depth.values(), default=0) + 1)
    return depth


def layout(graph):
    depth = _depth(graph)
    columns = defaultdict(list)
    for node in graph['nodes']:
        columns[depth[node]].append(node)
    max_rows = max((len(v) for v in columns.values()), default=1)
    pos, height = {}, 0
    for col, nodes in sorted(columns.items()):
        for row, node in enumerate(nodes):
            offset = (max_rows - len(nodes)) * (NODE_H + GAP_Y) / 2
            pos[node] = (PAD + col * (NODE_W + GAP_X), PAD + offset + row * (NODE_H + GAP_Y))
            height = max(height, PAD * 2 + offset + (row + 1) * NODE_H + row * GAP_Y)
    width = PAD * 2 + (max(depth.values()) + 1) * NODE_W + max(depth.values()) * GAP_X
    return pos, width, max(height, PAD * 2 + max_rows * NODE_H + (max_rows - 1) * GAP_Y)


def _color_map(graph):
    colors = {}
    for i, (_, members) in enumerate(STAGE_GROUPS.get(graph['key'], [])):
        for node in members:
            colors[node] = STAGE_COLORS[i % len(STAGE_COLORS)]
    return colors


def _anchor(src, dst):
    (sx, sy), (dx, dy) = src, dst
    if dx > sx:
        return (sx + NODE_W, sy + NODE_H / 2), (dx, dy + NODE_H / 2)
    if dx < sx:
        return (sx, sy + NODE_H / 2), (dx + NODE_W, dy + NODE_H / 2)
    if dy > sy:
        return (sx + NODE_W / 2, sy + NODE_H), (dx + NODE_W / 2, dy)
    return (sx + NODE_W / 2, sy), (dx + NODE_W / 2, dy + NODE_H)


def render_svg(graph, *, marker_id='arrow'):
    pos, width, height = layout(graph)
    colors = _color_map(graph)
    out = [f'<svg class="graph" data-graph="{graph["key"]}" xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
           f'font-family="ui-sans-serif,system-ui,sans-serif">']
    out.append(f'<defs><marker id="{marker_id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
               f'markerHeight="7" orient="auto-start-reverse">'
               f'<path d="M0,0 L10,5 L0,10 z" fill="#64748b"/></marker></defs>')
    for src, dst in graph['edges']:
        if not dst or src not in pos or dst not in pos:
            continue
        (sx, sy), (dx, dy) = _anchor(pos[src], pos[dst])
        if dx <= sx:
            mid_x = min(sx, dx) - 40
            path = f'M {sx:.0f} {sy:.0f} C {mid_x:.0f} {sy:.0f}, {mid_x:.0f} {dy:.0f}, {dx:.0f} {dy:.0f}'
            dash = ' stroke-dasharray="4 3"'
        else:
            path = f'M {sx:.0f} {sy:.0f} L {dx:.0f} {dy:.0f}'
            dash = ''
        out.append(f'<path d="{path}" fill="none" stroke="#94a3b8" stroke-width="1.4"{dash} '
                   f'marker-end="url(#{marker_id})"/>')
    for node in graph['nodes']:
        if node not in pos:
            continue
        x, y = pos[node]
        stroke = '#dc2626' if node == graph['entry'] else colors.get(node, '#475569')
        label = NODE_LABELS.get(node) or PIPELINE_LABELS.get(node) or node
        out.append(f'<g class="node" data-graph="{graph["key"]}" data-node="{node}">'
                   f'<rect x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{NODE_H}" rx="10" '
                   f'fill="{STATUS_COLORS["pending"]}" stroke="{stroke}" stroke-width="2"/>'
                   f'<text x="{x + 11:.0f}" y="{y + 19:.0f}" font-size="11" font-weight="700" fill="{stroke}">{node}</text>'
                   f'<text x="{x + NODE_W/2:.0f}" y="{y + 41:.0f}" text-anchor="middle" '
                   f'font-size="13" font-weight="600" fill="#0f172a">{escape(label)}</text>'
                   f'<title>{escape(graph["title"])} · {node} {escape(label)}</title></g>')
    out.append('</svg>')
    return ''.join(out)


def render_internals_html():
    """节点内部步骤：把 N02–N10 / T4–T6 / A3–A7 的真实动作展开。"""
    out = []
    for node, steps in NODE_INTERNALS.items():
        label = NODE_LABELS.get(node, node)
        lis = ''.join(f'<li>{escape(s)}</li>' for s in steps)
        out.append(f'<details><summary><b>{node}</b> · {escape(label)}</summary><ol>{lis}</ol></details>')
    return ''.join(out)


def render_mermaid(graph):
    lines = ['```mermaid', 'flowchart TD']
    for node in graph['nodes']:
        label = NODE_LABELS.get(node) or PIPELINE_LABELS.get(node) or node
        lines.append(f'    {node}["{node}·{label}"]')
    for src, dst in graph['edges']:
        lines.append(f'    {src} --> {dst}' if dst else f'    {src} --> END(["END"])')
    lines.append('```')
    return '\n'.join(lines)
