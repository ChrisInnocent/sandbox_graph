# Agent 生命周期首批实现验证

日期：2026-10-22。分支 `feat/agent-lifecycle`，基线 `2dfe8ae`，工作树 `.worktrees/agent-lifecycle`。本记录仅覆盖首批实现，完整生命周期方案仍在实施。

## 规格、设计与任务

沿用 [sandbox-evaluation-workflow](../../../openspec/changes/sandbox-evaluation-workflow/proposal.md)，保留 WF-39 与任务编号，避免分叉成两份生命周期规格。该决策的代价是 change 较大；按 42–49 分组跟踪本增量。

- [详细设计 ALD-01–10](../../../openspec/changes/sandbox-evaluation-workflow/agent-lifecycle-design.md)：正常、注册修复、隔离候选演进三条路径；状态机、预算、对账、恢复、provider、晋级及交接问题映射。
- [门禁 ATG-01–07](../../../openspec/changes/sandbox-evaluation-workflow/agent-lifecycle-test-gates.md)：G0–G6，ALU/ALI 用例、可信证据、负例与真实后端验收。
- [任务清单](../../../openspec/changes/sandbox-evaluation-workflow/tasks.md)：当前 change 共 209 项，150 项勾选。本轮完整完成 42.1、43.6；44.1/45.1 等仅局部接线，未勾选。

## 实现与复审

42.1 提供严格版本化模型及纯事件转换，不执行数据库事务、模型调用、现场授权或预算扣除。TransitionGuards 必须由未来服务从可信账本产生，不能接受模型自报为授权。

43.6 为 RemedyJobHandler 获取并续期真实 thread lease，分开 job claim 与 thread epoch，提交 node receipt/outcome/continuation 时复核两种所有权。失效和取消会停止执行并仅释放本次租约；测试仓储使用独立 UUID，避免 owner 字符串掩盖伪造 token。

复审拒绝和脚本执行失败分别触发监督，保留原始 NodeResult；图转入收尾，修复后验拒绝时不继续业务节点。新增 node_supervisor_id 是身份字段，尚不等于完整 provider 或 agent team。

独立代码审阅发现两项状态序列缺陷，已通过先失败后通过的回归修正：停止后未知结果保留原恢复入口；停止事件必须绑定并保留原 operation 身份。审阅未发现本批租约改动的其他阻断项。长期续租成功路径及新 claim 下结果重放可继续补强测试，当前未把这两项测试建议计作已完成。

## 可复查结果

命令在此工作树根执行，使用 `../../.venv/bin/python`。通过数量不换算成需求完成率。

| 范围 | 命令 | 结果与原始输出 |
|---|---|---|
| 编排回归 | `../../.venv/bin/python -m pytest tests/orchestration -q` | 1023 passed、19 skipped，15.87s，退出 0；[原始输出](orchestration.txt) |
| benchmark 集成（容量两例单列） | `../../.venv/bin/python -m pytest tests/benchmark/integration -q -m 'not postgres and not live and not linux' --ignore=tests/benchmark/integration/test_capacity_wake.py --durations=5` | 27 passed，70.09s，退出 0；[原始输出](benchmark-integration.txt) |
| 当前实现容量两例 | `../../.venv/bin/python -m pytest tests/benchmark/integration/test_capacity_wake.py -q --durations=2` | 2 passed，59.33s，退出 0；[原始输出](capacity-current.txt) |
| 原主工作区容量基线 | `.venv/bin/python -m pytest tests/benchmark/integration/test_capacity_wake.py -q --durations=2` | 2 passed，254.95s，退出 0；[原始输出](capacity-baseline.txt)。此行不是当前代码验收 |

纯合同/状态机的 756 项、租约/复审的 14 项均为上述编排回归子集，不重复加总。全库离线命令曾在容量测试期间被人工中断：140 passed，退出 2；不能声明全库通过。没有为本批生成逐测试 JUnit，原始输出仅证明对应命令的汇总。

OpenSpec 全部三个 change 的严格校验通过，任务总数及勾选数通过 CLI 核对。[OpenSpec 输出](openspec.json)、[310 个本地链接与任务编号检查](checks.json)、[11 个变更源码/测试文件摘要](source-manifest.json) 已归档；`git diff --check` 通过。历史 `scripts/verify_planning.py` 写死 96 项任务，本批采用独立链接/编号核查与 OpenSpec CLI 统计，没有把该历史脚本登记为当前门禁通过。容量用例两次耗时受运行条件影响，不据此推导性能改进。

## 未完成边界

持久生命周期服务、修复派发原子事务、双层预算、资源处置证明接线、RecoveryPlan、provider/agent team、隔离候选编码、可信门禁执行及版本晋级尚未完成。旧代码仍有交接列明的 C1–C4 等缺口，本批不构成自动生产自愈启用依据。

真实 PostgreSQL、多 worker 硬杀、真实隔离后端及模型/宿主 provider 未验收；19 项 skip 不计通过。未调用当前宿主的 dispatch agent 来证明本项目已支持该能力。

## 2026-10-23 文档一致性复核

首批代码验收后发现 proposal/design/D21 仍残留早期“未开始编码/没有 LangGraph/全部 planned”的陈述，本次已逐处修正，并在主工作区 proposal/design 加入独立工作树入口。Spec 保留目标要求，Purpose 关联实施差额，明确线程与共享 work 租约是独立要求。此次无运行时代码变更，未重跑运行时测试，任务计数维持主工作区 148/208、实现工作树 150/209。两工作区严格 OpenSpec、链接和任务计数的复核结果见 [文档一致性记录](coherence-2026-10-23.json)。上文 2026-10-22 测试与摘要记录按原日期保留。
