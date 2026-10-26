# 生命周期测试检查维护：本地证据

本组独立证据采集于 2026-10-23，包含当前工作树未提交增量。范围是独立 fixture/schema 兼容性、规划检查维护与当前已实现生命周期测试的收集，不是完整 G1–G6 门禁证明。

定向执行 6 个文件得到 **245 passed**，其中新增 schema 29 项、planning 17 项。实际规划检查得到 209 个唯一且格式有效的任务、45 个节点、330 个本地链接无断链；真实 OpenSpec strict 通过。

| 证据 | 来源与范围 |
|---|---|
| [规划检查 JSON](verification-maintenance-20261023T042700Z-planning.json) | 指定完整 Python 和 OpenSpec CLI 实际执行，原文件保存在 `/tmp/agent-lifecycle-verification-planning-20261023.json` |
| [收集 JSON](verification-maintenance-20261023T042700Z-collection.json) | 22 个现有增量文件；1,392 个唯一 nodeid、1,185 个参数化用例；包含完整 argv、源摘要、参数 ID、各文件计数及 16 个尚缺设计路径 |
| [实际 collect 输出](verification-maintenance-20261023T042700Z-collect.txt) | `python -m pytest --collect-only` 子进程的原始 stdout/stderr；只证明收集 |
| [本地核对源码](verification-maintenance-20261023T042700Z-collect-audit.py.txt) / [收集观察插件](verification-maintenance-20261023T042700Z-collect-observer.py.txt) | 本地采集工具；新增 40 个参数变体与字面清单对账，并实际拒绝空、重复、遗漏和意外项反例；不承担 47.* 的可信 runner 职责 |
| [首次失败输出](verification-maintenance-20261023T042345Z-collect.txt) | `/tmp` 直接调用 pytest 的导入路径错误，退出码 2；纠正入口后成功，保留失败历史 |

完整中文实施报告位于 `.superpowers/sdd/tasks/task-lifecycle-verification-report.md`。本次没有变更任务勾选、覆盖旧证据、生成 GateManifest、运行全库或用环境 skip 代替验收。未来固定必需集合、真实 PostgreSQL、候选隔离、晋级和真实 provider 验收仍按原任务执行。
