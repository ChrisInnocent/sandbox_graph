#!/usr/bin/env python3
"""图工程实时查看：本地 HTTP 服务，浏览器看 5 张图，跑 mock 时节点实时变色。

用法：
  .venv/bin/python scripts/graph_server.py                     # 默认 mock 实时模式
  .venv/bin/python scripts/graph_server.py --port 8770
  .venv/bin/python scripts/graph_server.py --dsn "$SANDBOX_GRAPH_POSTGRES_DSN" --run-id <run>

浏览器打开 http://127.0.0.1:8770 ，点“运行 mock（实时）”。
只读、离线；不写工程目录，不起真实实验。
"""
import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox_graph.viz import collect, render_svg, STATUS_COLORS, graph_nodes, render_internals_html

GRAPHS = collect()
NODE_INDEX = graph_nodes()
STATE = {'status': {g: {} for g in NODE_INDEX}, 'log': [], 'running': False,
         'tick': 0, 'mode': 'idle', 'run_id': None}
LOCK = threading.Lock()


def _log(message):
    with LOCK:
        STATE['log'].append({'t': time.strftime('%H:%M:%S'), 'm': message})
        STATE['log'] = STATE['log'][-200:]


def _set_status(status):
    with LOCK:
        STATE['status'] = status


def start_mock(delay=0.0):
    with LOCK:
        if STATE['running']:
            return False
        STATE.update({'running': True, 'mode': 'mock', 'tick': 0, 'status': {g: {} for g in NODE_INDEX}, 'run_id': None})

    def worker():
        import mock_pipeline

        def progress(status, tick):
            _set_status(status)
            with LOCK:
                STATE['tick'] = tick
            _log(f'tick {tick} 已执行')

        async def on_tick(tick):
            if delay:
                await asyncio.sleep(delay)

        try:
            summary = asyncio.run(mock_pipeline.run_mock(progress=progress, on_tick=on_tick))
            with LOCK:
                STATE['run_id'] = summary['run_id']
            _log(f"完成：run_id={summary['run_id']} ticks={summary['ticks']}")
        except Exception as exc:  # demo：把错误显示到页面
            _log(f'失败：{type(exc).__name__}: {exc}')
        finally:
            with LOCK:
                STATE['running'] = False

    threading.Thread(target=worker, daemon=True).start()
    _log('已启动 mock 全链路运行')
    return True


def db_status(dsn, run_id):
    from sandbox_graph.persistence.repository import PostgresRepository
    repo = PostgresRepository(dsn)
    status = {g: {} for g in NODE_INDEX}
    with repo.transaction() as tx:
        for row in tx.scan('receipts'):
            ident = row['body']['identity']
            if run_id and ident.get('run_ref') != run_id:
                continue
            node = ident['node_id']
            st = row['body']['result']['execution_status']
            for g, nodes in NODE_INDEX.items():
                if node in nodes:
                    prev = status[g].get(node)
                    status[g][node] = st if prev in (None, 'succeeded') else prev
    return status


def page():
    panels = ''.join(f'<section class="panel" id="{g["key"]}"><h2>{g["title"]}</h2>'
                     f'<div class="canvas">{render_svg(g, marker_id="arrow-" + g["key"])}</div></section>'
                     for g in GRAPHS)
    tabs = ''.join(f'<button class="tab" data-t="{g["key"]}">{g["title"].split("（")[0]}</button>' for g in GRAPHS) + '<button class="tab" data-t="internals">节点内部步骤</button>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>sandbox_graph 实时图视图</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{{color-scheme:dark}}
 body{{margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-sans-serif,system-ui,sans-serif}}
 header{{padding:14px 20px;border-bottom:1px solid #1e293b;display:flex;gap:14px;align-items:center;flex-wrap:wrap}}
 h1{{margin:0;font-size:17px}} .sub{{color:#94a3b8;font-size:12px}}
 button.go{{background:#16a34a;color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}}
 button.go:disabled{{background:#334155;cursor:default}}
 nav{{display:flex;gap:6px;padding:10px 20px;flex-wrap:wrap;border-bottom:1px solid #1e293b}}
 .tab{{background:#111c33;color:#cbd5e1;border:1px solid #1e293b;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:13px}}
 .tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
 .panel{{display:none;padding:14px 20px}} .panel.active{{display:block}}
 h2{{font-size:14px;color:#93c5fd;margin:0 0 10px}}
 .canvas{{overflow:auto;background:#fff;border-radius:12px;padding:8px;display:inline-block;max-width:100%}}
 .legend{{display:flex;gap:14px;padding:8px 20px;font-size:12px;color:#cbd5e1;flex-wrap:wrap}}
 .chip{{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px;border:1px solid #64748b}}
 #log{{position:fixed;right:0;bottom:0;max-height:30vh;width:520px;overflow:auto;background:#0f172aee;border-top:1px solid #1e293b;
       border-left:1px solid #1e293b;padding:8px 12px;font:12px/1.5 ui-monospace,monospace;color:#94a3b8}}
 #meta{{font-size:12px;color:#94a3b8}}
 .internals details{{background:#111c33;border:1px solid #1e293b;border-radius:8px;padding:8px 12px;margin:6px 0}}
 .internals summary{{cursor:pointer}} .internals ol{{margin:8px 0 4px 20px;line-height:1.7}}
</style></head><body>
<header><h1>sandbox_graph 实时图视图</h1>
  <button class="go" id="run">运行 mock（实时）</button>
  <span id="meta" class="sub">状态：空闲</span>
  <span class="sub">节点/边来自代码：workflows.py 与 build_work；回边为虚线</span></header>
<nav>{tabs}</nav>
<div class="legend">
  {''.join(f'<span><span class="chip" style="background:{c}"></span>{n}</span>' for n, c in
           [('pending', STATUS_COLORS['pending']), ('succeeded', STATUS_COLORS['succeeded']),
            ('blocked', STATUS_COLORS['blocked']), ('failed', STATUS_COLORS['failed']),
            ('cancelled', STATUS_COLORS['cancelled'])])}
</div>
{panels}
<section class="panel" id="internals"><h2>节点内部步骤（发压/采集/指标）</h2><div class="internals">{render_internals_html()}</div></section>
<div id="log"></div>
<script>
 const COLORS={json.dumps(STATUS_COLORS)};
 const tabs=[...document.querySelectorAll('.tab')];
 function show(k){{tabs.forEach(t=>t.classList.toggle('active',t.dataset.t===k));
   document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id===k));}}
 tabs.forEach(t=>t.onclick=()=>show(t.dataset.t)); show(tabs[0].dataset.t);
 function paint(state){{
   document.querySelectorAll('g.node').forEach(g=>{{
     const st=(state[g.dataset.graph]||{{}})[g.dataset.node];
     const rect=g.querySelector('rect');
     rect.setAttribute('fill', COLORS[st]||COLORS.pending);
   }});
 }}
 async function poll(){{
   const s=await (await fetch('/api/state')).json();
   paint(s.status);
   document.getElementById('meta').textContent=
     `状态：${{s.running?'运行中':'空闲'}}  模式：${{s.mode}}  tick=${{s.tick}}`+(s.run_id?`  run_id=${{s.run_id}}`:'');
   const log=await (await fetch('/api/log')).json();
   document.getElementById('log').innerHTML=log.map(l=>`[${{l.t}}] ${{l.m}}`).join('<br>');
 }}
 document.getElementById('run').onclick=async()=>{{await fetch('/api/mock/start',{{method:'POST'}});}};
 setInterval(poll,600); poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    dsn = None
    run_id = None
    delay = 0.0

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音默认日志
        pass

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._send(200, page().encode('utf-8'), 'text/html; charset=utf-8')
        elif self.path.startswith('/api/state'):
            if self.dsn:
                with LOCK:
                    status = STATE['status']
                try:
                    status = db_status(self.dsn, self.run_id)
                    with LOCK:
                        STATE['mode'] = 'postgres'
                except Exception as exc:
                    _log(f'DB 读取失败：{type(exc).__name__}: {exc}')
                body = json.dumps({'status': status, 'running': STATE['running'], 'mode': 'postgres',
                                   'tick': STATE['tick'], 'run_id': self.run_id}).encode()
            else:
                with LOCK:
                    body = json.dumps({'status': STATE['status'], 'running': STATE['running'],
                                       'mode': STATE['mode'], 'tick': STATE['tick'],
                                       'run_id': STATE['run_id']}).encode()
            self._send(200, body, 'application/json')
        elif self.path.startswith('/api/graphs'):
            self._send(200, json.dumps(GRAPHS, ensure_ascii=False).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/log'):
            with LOCK:
                self._send(200, json.dumps(STATE['log']).encode(), 'application/json')
        else:
            self._send(404, b'not found', 'text/plain')

    def do_POST(self):
        if self.path.startswith('/api/mock/start'):
            ok = start_mock(self.delay)
            self._send(200, json.dumps({'started': ok}).encode(), 'application/json')
        else:
            self._send(404, b'not found', 'text/plain')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--dsn', default=None)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--delay', type=float, default=0.15, help='mock 每 tick 间隔秒数，便于观看')
    args = parser.parse_args()
    Handler.dsn = args.dsn
    Handler.run_id = args.run_id
    Handler.delay = args.delay
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = 'PostgreSQL 账本' if args.dsn else 'mock（内存）'
    print(f'图视图：http://{args.host}:{args.port}   模式：{mode}')
    if not args.dsn:
        print('提示：打开页面后点“运行 mock（实时）”，节点会实时变色。')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
