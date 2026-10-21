# 编排图引擎（tasks 组 1–14）差距清单

2026-10-21。范围：`sandbox_graph/` + `tests/orchestration/` + `docs/orchestration/` 的编排图引擎。
本文是**只读审视**结论，不声称任何未运行的真实环境验收；与 `implementation-report.md`、`requirement-coverage.md` 交叉核对。

## 0. 总览

| 维度 | 现状 |
|---|---|
| 设计 | `orchestration-design.md` D01–D17、`orchestration-test-design.md` T01–T13（含不变量 I01–I14、ORC-001–073）齐全 |
| 代码 | 55 模块 / 6189 LOC；组 1–14 相关 ≈30 模块，核心路径都在，部分模块相对设计偏紧凑 |
| 验收测试 | 任务点名 40 个测试文件，**缺 33 个** |
| 文档 | `docs/orchestration/` 7 份（implementation-report / requirement-coverage / script-contracts / setup / contracts / reuse-rework / progress） |

**结论**：设计在、核心代码在、**验收大面积缺**。多数薄模块实现了设计核心（非空壳），差距集中在**验收缺失**与**边界/竞态未覆盖**。

## 1. 逐任务差距

判定：✅ 设计+代码+测试齐；◐ partial（代码在、验收缺或边界未齐）；⛔ blocked（需真实 PostgreSQL/产品）。

| 任务 | 设计 | 代码 | 点名测试 | 判定 | 真缺 |
|---|---|---|---|---|---|
| 1.1 包/依赖/锁版本 | — | 环境 | — | ⛔ | 正式 PG 最小连通、平台支持矩阵 |
| 1.2 严格模型 | D04 | models.py | test_models ✅ | ◐ | 全模型逐条、单位/时钟矩阵 |
| 1.3 分离状态/重试/CAS | D06 | transitions.py | test_transitions ❌ | ◐ | 状态维度矩阵、重试负例 |
| 1.4 T02 夹具 | T02 | conftest | — | ◐ | 三组环境/oracle/SpyModel/provenance 矩阵 |
| 2.1 45 节点/方法目录 | D07 | registry.py | test_registry ❌ | ◐ | 逐节点 S/M/H+验收矩阵 |
| 2.3 依赖/证据/stage-only | D08 | compiler.py | test_planning ✅ | ◐ | 复杂依赖 DAG、有效证据语义 |
| 2.4 图校验/CRUD | D07/D14 | factory.py | test_graph_definitions ❌ | ◐ | 完整 CRUD/版本激活矩阵 |
| 3.1 PG 账本/UoW | D15 | repository.py | test_repository ❌ | ⛔ | 真实 PostgreSQL |
| 3.2 visit/receipt/intent | D05 | repository.py | test_receipts ❌ | ⛔ | checkpoint/receipt 两侧硬杀 |
| 3.3 产物不可变发布 | D15 | artifacts/store.py | test_artifacts ✅ | ◐ | orphan 组合故障、无 GC |
| 4.1 租约/冲突域 | D10 | leases.py | test_leases ❌ | ⛔ | 真实双连接事务顺序 |
| 4.2 FencedPostgresSaver | D10 | checkpointer.py | test_fenced_saver ❌ | ⛔ | lease 行同事务/pending writes |
| 4.3 owner 接管 | D10 | worker.py | test_owner_takeover ❌ | ⛔ | 接管不改 id、未知域隔离 |
| 5.1 tasks/events | D09 | tasks.py/events.py | test_tasks_events ❌ | ◐ | 去重/seq/outbox 竞态矩阵 |
| 5.2 机器等待 | D09 | waits.py | test_machine_waits ❌ | ◐ | barrier 唤醒不丢/不忙轮询 |
| 5.3 人工 interrupt | D12 | waits.py | test_human_wait ✅ | ◐ | 持久 PG interrupt |
| 5.4 等待 TTL/watchdog | D12 | waits.py/worker.py | test_wait_watchdog ❌ | ◐ | owner 崩溃后有界处置 |
| 6.2 ControlledRunner | D11 | controlled_runner.py | test_controlled_runner ✅ | ◐ | controller+PG 硬杀 |
| 6.3 观测协议 | D11 | observation.py | test_observation_protocol ❌ | ◐ | 序列缺口/跨重启/截断 |
| 6.4 cancel/reconcile/cleanup | D11 | runner.py | test_cleanup ❌ | ◐ | 不清理他组资源 |
| 6.5 测量合同透传 | D11 | runner.py | test_measurement_contract ❌ | ◐ | ORC-059–064 合成数据 |
| 7.1 五类真实图 | D07 | factory.py | test_graph_factory ❌ | ◐ | handler 注册/编译矩阵 |
| 7.2 driver | D10 | driver.py | test_driver ✅ | ◐ | 原版本恢复身份冻结 |
| 7.3 R0–R7 | D07 | workflows.py | test_run_graph ❌ | ◐ | 一组先交付即分析、R6 |
| 7.4 G0–GE | D07 | workflows.py | test_group_graph ❌ | ◐ | 组角色不拆无限 agent |
| 8.2 N00–N03 | D11 | session/bridge | test_work_preflight ❌ | ◐ | 守卫/对账/准备状态 |
| 8.3 N04–N07 | D11 | session/bridge | test_work_measurement ❌ | ◐ | baseline→操作→稳态→停止 |
| 8.4 N08–N10 | D11 | session/bridge | test_work_delivery ❌ | ◐ | 失败出口证据+处置 |
| 9.1 agent 上下文 | D12 | agents.py | test_agent_context ❌ | ◐ | allowlist/存储权限 |
| 9.2 诊断提案 | D12 | agents.py | test_agent_proposals ❌ | ◐ | 越权/伪造引用拒绝 |
| 9.3 预算 | D12 | agents.py | test_agent_budgets ❌ | ◐ | 无无限轮询、usage 缺失 |
| 10.2 A1–A7 | D13 | workflows.py | test_analysis_graph ✅ | ◐ | 单位/不可比/F 端到端 |
| 10.3 F1/F2/F4+closure | D13 | closure.py | test_report_closure ❌ | ◐ | CAS/partial 归档竞态 |
| 10.4 增量分析 | D13 | reviews.py/workflows.py | test_incremental_analysis ❌ | ◐ | 迟到版本、rerender |
| 11.1 ImpactPlan/复用 | D14 | changes.py | test_change_impact ❌ | ◐ | 阈值/规格/事件缺失区分 |
| 11.2 激活事务 | D14 | revisions.py | test_revision_activation ❌ | ⛔ | 并发候选 CAS（真实 PG） |
| 11.3 dispatch gate | D14 | revisions.py/tasks.py | test_dispatch_cutoff ❌ | ◐ | superseded/drain/cleanup |
| 11.4 旧图 drain | D14 | worker.py | test_revision_drain ❌ | ◐ | 跨版本资源门控 |
| 11.5 回退新谱系 | D14 | revisions.py | test_revision_rollback ❌ | ◐ | 旧定义保留 |
| 12.2 可观测性 | D16 | runtime/* | test_observability ❌ | ◐ | 统一 trace/耗时/usage |
| 12.3 部署/恢复手册 | D16 | — | — | ◐ | 空真实 PG 起五阶段 |
| 13.2 脚本合同清单 | — | script-contracts.md ✅ | — | ◐ | 逐节点合同+未决 Q |
| 13.3 里程碑覆盖核对 | — | requirement-coverage.md ✅ | — | ◐ | L0–L4/L5 分层缺口 |
| 14.1 组合故障 | T06/T09 | — | test_recovery_integration ❌ | ⛔ | 真实 PG 双 worker |
| 14.2 五阶段 E2E | T09 | — | test_end_to_end ❌ | ◐ | 双环境组/增量 |
| 14.3 属性/变异验收 | T11 | — | — | ◐ | ORC-001–070 汇总 |

## 2. 三类差距

**① 薄/边界未覆盖（需补设计→补实现→补测试，离线可做）**
`transitions.py`(D06 全状态维度)、`changes.py`(D14 ImpactPlan)、`closure.py`(D13 CAS/partial)、`observation.py`(D11 观测协议)、`events.py`/`tasks.py`(D09 去重/seq/supersede)、`leases.py`/`checkpointer.py`(D10 fencing)。

**② 缺离线验收测试（实现可能对，无证据）**
组 1.3、2.1/2.4、3.3、5.1/5.2/5.4、6.3/6.4/6.5、7.1/7.3/7.4、8.2/8.3/8.4、9.1/9.2/9.3、10.3/10.4、11.1/11.3/11.4/11.5、12.2。

**③ 仅缺真实环境（blocked，不可离线假勾）**
`test_repository`、`test_receipts`、`test_leases`、`test_fenced_saver`、`test_owner_takeover`、`test_revision_activation`、`test_recovery_integration`。

## 3. 补设计/补测试优先级

1. **① 薄模块设计**：D06 状态维度、D09 事件/任务竞态、D10 fencing、D11 观测协议、D13 closure CAS、D14 ImpactPlan。
2. **② 离线验收测试**（TDD，按组）：从 1.3 → 5.1 → 6.3 → 10.3 → 11.1 起步，逐步扩到其余组。
3. **③ 保持 blocked**，直到提供真实 PostgreSQL/三产品环境。
