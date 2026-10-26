# 使用与维护指南

总入口见 [README](../README.md)。本指南说明现有工程的修改位置、变更步骤与验收要求，不代表尚未实现的能力已经可用。

## 选择范围 ≠ 修改图（先分清）

| 你想做的 | 选哪条路 | 入口 |
|---|---|---|
| 本次多测/少测、改优先级/环境组/类型/拓扑/来源 kind/阶段 | **选择（不改图）** | `Selection`（[选择器](../sandbox_graph/planning/compiler.py)）；`python -m sandbox_graph catalog` / `preview selection.json`（只读） |
| 新增测试能力、改变节点依赖、增删图节点或连线 | **图版本变更（改图）** | `GraphService.propose_patch` → `RevisionService.activate_revision`；新版本＋新 run，**不热切换**（[编排设计](../openspec/changes/sandbox-evaluation-workflow/orchestration-design.md) D08/D14） |

选择只在**既有图与授权范围**内筛选：同字段多值取并集、异字段取交集、再减排除；自动补齐**必要依赖**（如选 B04 补暂停 fixture）但**不扩为**其它场景的性能测试；`preview` 不改环境。按单个 N00–N10 子节点选也不属于选择——N00–N10 是每个工作单元的固定合同子图。详见 WF-28–30、WF-38。

**环境检查/配置能否跳过**：测量运行中 **E1/E2（环境检查）是必经门禁**（组图 `G0→E1→E2` 无条件），**不可**通过选择跳过；**C1–C3（环境配置）是条件性的**，仅当 E2 报差异才执行，环境满足时 `E2→T0` 直接测量。绕开检查/配置/测量只能选 `stage='analysis'|'report'`；重入是**快速复检**（读缓存证据），不是跳过。“假定已就绪”的运行模式属图/合同变更，当前未提供。

## 先判断是哪种修改

| 目标 | 首选修改位置 | 要检查什么 |
| --- | --- | --- |
| 本次多测/少测一个用例、改变优先级或环境范围 | 输入目录与 `Selection`，见 [选择器](../sandbox_graph/planning/compiler.py) | 离线 preview 的展开结果、依赖、授权组和能力缺口 |
| 修改规格、并发、Q、窗口/批次模式、终点 | 新用例配置，见 [CaseContract](../sandbox_bench/contracts.py) | 生成新身份/运行，保留旧配置和证据；可比性是否改变 |
| 增加/修改环境组 | 环境配置、角色与能力证据、部署服务接线 | 组与节点身份、冲突域、凭证引用、版本、inventory 和安装后复查 |
| 增加测试方法 | [方法目录](../sandbox_bench/registry.py)、[场景](../sandbox_bench/scenarios.py)、[密度场景](../sandbox_bench/scenario_density.py)、[执行会话](../sandbox_bench/session.py) | 方法合同、适配、完成条件、预算、恢复、清理及证据封存 |
| 增加沙箱类型/修正协议适配 | [适配器](../sandbox_bench/adapters.py)、[传输](../sandbox_bench/transport.py)、[配置接线](../sandbox_bench/wiring.py) | 精确版本与协议证据、异步完成、未知结果对账、真实部署核验 |
| 新增或修改图节点与连线 | [节点目录](../sandbox_graph/catalog/registry.py)、[图工厂](../sandbox_graph/graphs/factory.py)、[工作流服务](../sandbox_graph/services/workflows.py)、[启动装配](../sandbox_graph/runtime/bootstrap.py) | 状态转换、输入输出、分支、等待、失败和收尾路径、新图版本 |
| 修改指标、Excel或报告 | [指标计算](../sandbox_bench/metrics/)、[分析](../sandbox_bench/analysis.py)、[可比性](../sandbox_bench/comparison.py)、[分析服务](../sandbox_bench/workflow_services.py) | 单位、分母、时钟、覆盖率、黄金值、重算和来源追溯 |

目录注册只提供发现和校验信息，不会自动实现方法或接通执行器。具体部署的探测、密钥配置和安装脚本仍有缺口，见 [交付记录](verification/README.md)。

## 增加一个测试用例

已有方法只需新增实验参数时，优先复用现有合同和实现；例如新增 2v4G、固定 Q 下的新并发度，通常不需要增加 LangGraph 节点。

新增一种方法或新的行为时：

1. 写清测量目的、产品能力、来源、规格、完成事件、计时起止、请求与子资源预算、成功/失败/未知分类。
2. 更新 OpenSpec 需求、测试节点和设计；明确每个节点哪些工作由脚本完成，哪些需要模型解释或人工决策。
3. 扩展合同、注册目录、场景执行器和明确的产品适配；将实现接入会话/编排服务，保持原始证据可重算。
4. 验证正常结果、异步终态、超时/未知、重复恢复不重发、资源所有权和清理路径；指标用独立黄金数据验证。
5. 先受控协议测试，再在明确部署上做小规模真实验收。只有对应范围的证据齐全，才更新能力状态和任务勾选。

参考 [测试代码设计](../openspec/changes/sandbox-evaluation-workflow/benchmark-design.md)、[测试契约设计](../openspec/changes/sandbox-evaluation-workflow/benchmark-contract-design.md) 和 [测试节点](../openspec/changes/sandbox-evaluation-workflow/test-nodes.md)。

## 删除或停用用例、节点

- 只是不参加本次实验：从本次选择范围移除，不删除已有方法、代码或历史产物。重新预览，检查依赖和覆盖缺口。
- 停用某种能力：在新目录/计划中停止准入；保留原运行需要的代码、方法定义和清理能力。
- 删除图节点：发布新的图定义，检查所有入边/出边、状态字段、交付者和消费者，以及中断/失败分支；保留旧图运行的处理路径。
- 历史运行仍有未知提交、租约、资源或复用引用时，不能删除其恢复、对账和清理实现。产物删除需要独立保留策略，不能把它当作“删除测试用例”的附带动作。

## 修改图和恢复运行

项目采用“旧图收尾，新图新运行”的版本切换方案：

1. 固定原运行的 graph_ref、plan、合同及代码版本，旧线程继续原图。
2. 新拓扑/测量语义建立新版本和新 run/thread，不把旧 checkpoint 交给新图直接解释。
3. 需要复用结果时，通过明确的复用合同核验来源、规格、方法、终点、条件、脚本、有效期及接受状态；只建立证据引用，不能重复计入原始样本。
4. 恢复原运行时先读取账本和 receipt，对未知外部操作执行 reconcile；不能清空输出目录或直接重复 POST。

不可变 bundle、worker 版本领取、后继 deployment 绑定和失败后新 run/thread 回退服务及 CLI 已交付；部署者仍需保留原版本 worker 以完成对账。真实发布/回退验收单列，不能把本地测试当现场证据。详见 [生命周期操作](orchestration/lifecycle-operations.md)、[复用与返工](orchestration/reuse-rework.md)、[图编排设计](../openspec/changes/sandbox-evaluation-workflow/orchestration-design.md)。

## 修改统计口径与报告

修改前先确认它是修复计算错误，还是改变实验定义。后者需要新版本，不能无提示覆盖历史结论。

- 延迟只在同一完成终点、时钟口径和明确样本集合内比较；不要平均多轮 P95 来代替合并样本的分位数。
- 密度分别保存实际达到值、增长判定与资源投影；实际平均资源开支使用真实最终 cohort，不使用发出的请求数量。
- 保留按节点角色/组件的 PSS、CPU 时间、峰值和稳态窗口；缺测不是 0，RSS 不替代 PSS。
- GuestOS 减项必须有同域同窗口证据，无法证明时保留原始值并标缺口。
- 横向对比先判可比性，再计算与解释差异；原始数据、派生 Excel 和模型结论保持可追溯。

计算变更先用固定输入独立算出预期值，再检查离线重算。输出新分析/报告版本，保留原报告及其证据引用。参见 [资源口径](benchmark-resources.md) 和 [分析报告接线](analysis-report.md)。

## OpenSpec 与智能体协作

当前主 change 是 [sandbox-evaluation-workflow](../openspec/changes/sandbox-evaluation-workflow/tasks.md)，尚未完成。与既有任务直接相关的补充应更新当前需求/设计/任务；独立的新能力可以建立单独 change，并说明对当前未完成工作的依赖。

本项目文档分工：

- `proposal.md`：目的和范围；`specs/*/spec.md`：需求与验收场景。
- `design.md`、`orchestration-design.md`、`benchmark-design.md`：总体、图编排和测试执行设计。
- `orchestration-test-design.md`、`benchmark-contract-design.md`：图与测试脚本自身的验证设计。
- `node-workflows.md`、`node-automation.md`、`test-nodes.md`：节点子流程、职责和测试口径。
- `tasks.md`：可执行任务和验收状态；不能因存在实现文件就勾选完成。

安装了对应 skills 时，可用 explore 澄清、propose 建立新 change、update-change 修订现有设计、apply-change 实现、archive-change 归档已验收的 change。名称和调用语法以本机实际安装为准；这些是智能体技能，不是本项目 Python CLI 子命令。没有 skills 时也可以直接读写这些文档并运行 OpenSpec CLI 校验。

给智能体的维护提示词：

```text
请修改 sandbox_graph 的【具体能力/节点/用例】，目标是【预期行为】。
先读 README.md、docs/maintenance.md、当前 change 的 tasks.md，
再只读取相关需求、设计、实现和测试，列出对应代码与未实现项。
先判断能否只改选择器/参数；需要改行为时，同步需求、设计、测试契约和任务。
每个受影响节点明确输入、输出、脚本动作、校验、失败恢复、模型/人工职责。
保持旧运行可恢复；拓扑和测量语义变更使用新版本、新运行。
实现并运行相关验证，记录执行命令和证据。不要用合成数据冒充真实验收。
最后给出修改位置、验证结果、剩余缺口和运行方法。
```

每个工作 agent 只接收相关文件路径、输入引用、任务 ID、验收条件和输出位置；统筹 agent 接收摘要与交付引用。大日志保存在文件中，按需读取。执行状态以持久账本为准；人工维护交接记录应包含 run/work ID、版本、产物位置、未知操作和下一步，不能替代账本。

## 验证与提交

根据影响范围先执行针对性测试，例如：

```bash
.venv/bin/python -m pytest tests/benchmark -q -rs
.venv/bin/python -m pytest tests/orchestration -q -rs
```

行为变更集成后运行完整回归；纯文档修改检查链接、对应命令和格式即可。下列规划检查需要可用的 OpenSpec CLI；不在 PATH 时可通过 `--openspec-bin /absolute/path/to/openspec` 指定：

```bash
.venv/bin/python -m pytest -q -rs
.venv/bin/python scripts/verify_planning.py --output /tmp/sandbox-graph-planning-checks.json
git diff --check
git diff --stat
git status --short
```

`verify_planning.py` 当前固定检查 45 个节点和 96 个任务。新增/删除节点或任务后，应同步更新这些预期数量并审查实际目录与唯一性，不能为了通过检查删除有效需求，也不能删除校验逻辑。

已有 `docs/verification/` 中的测试输出和源码清单属于历史验收记录。新验证单独保存，明确代码版本、命令、平台、通过/失败/skipped 和缺口，不将旧结果写成新版本的验收。

真实 PostgreSQL 测试需要专用 `SANDBOX_GRAPH_TEST_POSTGRES_DSN`；Linux 资源采集和真实产品实验需要对应环境。没有条件时保留 skipped/blocked，不换成合成结果宣称通过。

提交时同时检查代码、设计、任务、测试和说明文档的一致性。真实凭证、环境私密信息及未脱敏日志不应进入公共仓库；环境配置和运行证据遵循各自的持久化与备份策略。
