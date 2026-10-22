#!/usr/bin/env python3
"""从真实图定义生成静态可视化：自包含 HTML（内联 SVG）+ Mermaid Markdown。

实时查看请用 `scripts/graph_server.py`。
用法：.venv/bin/python scripts/render_graphs.py [--out ./graph-viz]
"""
import argparse
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox_graph.viz import collect, render_svg, render_mermaid, render_internals_html


def render_html(graphs):
    tabs = ''.join(f'<button class="tab" data-t="{g["key"]}">{html.escape(g["title"].split("（")[0])}</button>'
                   for g in graphs) + '<button class="tab" data-t="internals">节点内部步骤</button>'
    panels = ''.join(f'<section class="panel" id="{g["key"]}"><h2>{html.escape(g["title"])}</h2>'
                     f'<div class="canvas">{render_svg(g, marker_id="arrow-" + g["key"])}</div>'
                     f'<details><summary>Mermaid 源码</summary><pre>{html.escape(render_mermaid(g))}</pre></details>'
                     f'</section>' for g in graphs)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>sandbox_graph 图工程总览</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-sans-serif,system-ui,sans-serif}}
 header{{padding:16px 20px;border-bottom:1px solid #1e293b}}
 h1{{margin:0 0 4px;font-size:18px}} .sub{{color:#94a3b8;font-size:13px}}
 nav{{display:flex;gap:6px;padding:10px 20px;flex-wrap:wrap;border-bottom:1px solid #1e293b}}
 .tab{{background:#111c33;color:#cbd5e1;border:1px solid #1e293b;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:13px}}
 .tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
 .panel{{display:none;padding:16px 20px}} .panel.active{{display:block}}
 h2{{font-size:15px;color:#93c5fd;margin:0 0 12px}}
 .canvas{{overflow:auto;background:#fff;border-radius:12px;padding:8px;display:inline-block;max-width:100%}}
 pre{{background:#111c33;border:1px solid #1e293b;border-radius:8px;padding:12px;overflow:auto;font-size:12px}}
 .internals details{{background:#111c33;border:1px solid #1e293b;border-radius:8px;padding:8px 12px;margin:6px 0}}
 .internals summary{{cursor:pointer}} .internals ol{{margin:8px 0 4px 20px}}
</style></head><body><header><h1>sandbox_graph 图工程总览</h1>
<div class="sub">由 scripts/render_graphs.py 从真实图定义生成（workflows.py 与 build_work）。回边/失败边为虚线。</div></header>
<nav>{tabs}</nav>{panels}
<section class="panel" id="internals"><h2>节点内部步骤（发压/采集/指标，来自 session.py / load.py / measure.py / sampler.py）</h2>
<div class="internals">{render_internals_html()}</div></section>
<script>
 const tabs=[...document.querySelectorAll('.tab')];
 function show(k){{tabs.forEach(t=>t.classList.toggle('active',t.dataset.t===k));
   document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id===k));}}
 tabs.forEach(t=>t.onclick=()=>show(t.dataset.t)); show(tabs[0].dataset.t);
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('graph-viz'))
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    graphs = collect()
    (out / 'graphs.html').write_text(render_html(graphs), encoding='utf-8')
    md = ['# sandbox_graph 图工程总览', '',
          '由 `scripts/render_graphs.py` 从真实图定义生成（GitHub 可直接渲染 Mermaid）。', '']
    for g in graphs:
        md += [f'## {g["title"]}', '', render_mermaid(g), '']
    (out / 'overview.md').write_text('\n'.join(md), encoding='utf-8')
    print(f'HTML    : {out / "graphs.html"}')
    print(f'Markdown: {out / "overview.md"}')
    for g in graphs:
        print(f'  - {g["key"]:9s} nodes={len(g["nodes"]):2d} edges={len(g["edges"]):2d}  {g["title"]}')


if __name__ == '__main__':
    main()
