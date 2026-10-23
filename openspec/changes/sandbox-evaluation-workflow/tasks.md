# 图编排与沙箱测试代码实施任务

2026-10-22 新增节点生命周期与候选演进任务 **42–49**，依据 [详细设计 ALD](agent-lifecycle-design.md) 和 [测试门禁 ATG](agent-lifecycle-test-gates.md)。这些任务全部 planned；本次只补设计，不以文档校验代替代码实施。既有 1–41 组进度保留。新增依赖：42→43→44→45；42→46→47；44/45/47→48→49。每组同时提交实现、对应测试及合同/操作文档，不把测试和文档延后到最后一组。

本清单由 OpenSpec propose 在 2026-10-21 生成，现按用户授权进入 apply；勾选表示整条验收已有证据，未勾选可能已有部分代码，具体缺口见 [实施记录](../../../docs/orchestration/progress.md)。设计依据：[主设计](design.md)、[编排代码设计](orchestration-design.md)、[编排测试设计](orchestration-test-design.md) 及三份 delta spec。Q3 已确认：旧任务原图收尾，新图创建关联的新运行，复用经验收证据，不热切换活动拓扑。

当前实施范围包括通用图编排、脚本调用协议、状态／证据管理，以及 B01–B10 测试执行代码、三产品适配、资源采集、指标计算及对应契约测试；E01–E06 保留低优先级执行骨架。设计依据增加 [测试代码设计](benchmark-design.md) 和 [测试契约设计](benchmark-contract-design.md)。采用 D01 的 Python 异步／LangGraph／PostgreSQL 工程基线；版本记录在 pyproject.toml/requirements.lock。以下是目标交付和验收要求，不能仅凭文件存在或受控测试通过，推定真实环境验收完成。

`orchestration-design.md` 以下简称 D，`orchestration-test-design.md` 的用例简称 ORC。每项实现连同对应测试、合同文档一起交付；每个测试结果记录版本、fixture、断言、日志与失败证据。统一测试目录为 `tests/orchestration/`，文件名按测试设计 T01 放入 unit/graphs/persistence/recovery/e2e 对应层；跨层用例允许分文件实现，常规验收入口拟为 `python3 -m pytest tests/orchestration/<层>/<文件>`；数据库、故障和端到端测试使用独立标记及临时命名空间。未执行、环境缺失和 skipped 不登记为通过。

依赖顺序：1 → 2/3 → 4/5/6 → 7 → 8/9 → 10/11 → 12 → 13；14 为跨组件验收。测试执行新增组15–26：15→16/17→18→19/20/21，22→23，24→25→26；25依赖图接口与各执行模块，真实26.2/26.3依赖环境合同；27 依赖 17／19，其真实构建适配随 26 验收。组间共享模型或数据库迁移须先稳定接口，不能据此默认并行写同一文件。实施前 `sandbox_bench/` 是暂停的脚本草稿，基线中 `tests/test_runner.py` 引用的 runner 缺失；本轮已补入口并保留原测试回归，不隐藏旧失败。

## 1. 工程边界、模型和独立测试基座

- [ ] 1.1 建立 `sandbox_graph/` 包、项目依赖与锁文件，锁定 Python、LangGraph、saver 和 PostgreSQL 支持范围；更新 `docs/orchestration/setup.md` 的启动与测试要求。验收：隔离环境安装成功，记录实际版本，真实图／数据库最小连通检查通过，缺后端明确失败而非退回内存。
- [x] 1.2 在 `domain/models.py` 定义 D04 的运行、图、计划、工作、attempt、operation、证据及决定模型，严格字段／单位／时钟域校验与规范化 hash；同步 `docs/orchestration/contracts.md`。验收：`test_models.py` 覆盖身份稳定、未知字段、秘密引用、重复实验与重试分离，ORC-018、019、062。
- [x] 1.3 在 `domain/transitions.py` 实现 D06 分离状态与错误分类、scope_disposition、有限重试及 CAS 输入合同。验收：`test_transitions.py` 覆盖 ORC-016–019、050、051，性能不达标不会触发“重试到达标”，等待不冒充终态；状态表同步合同文档。
- [x] 1.4 在 `tests/orchestration/conftest.py` 建立 T02 的三组环境、共享宿主、独立 oracle、FakeClock 和 SpyModel；设计受控 runner 夹具的持久接口。验收：夹具自检可制造 accepted≠complete、跨时钟、缺测和共享域反例，预期值不调用生产计算函数；记录 fixture hash 与合成数据标签。

## 2. 节点目录与选择计划编译

- [x] 2.1 在 `catalog/registry.py` 注册 D07 的全部 45 个工作节点、方法绑定及 S/M/H 职责，路由菱形不建立额外 agent。验收：`test_registry.py` 参数化核对 ORC-002、027、070，B01–B10/E01–E06 均有绑定或明确缺适配状态，目录与节点文档一致。
- [x] 2.2 在 `planning/compiler.py` 实现优先级／环境组／沙箱类型／用例筛选，同轴并集、跨轴交集、排除及授权边界。验收：`test_selection.py` 覆盖 ORC-001–005、010，空集合／未知值不回退全测，预览与执行使用同一 plan hash；文档给出有限选择例子。
- [x] 2.3 实现依赖补齐、有效证据预检、能力与适配缺口、有限条件矩阵及 stage-only 计划。验收：`test_planning.py` 覆盖 ORC-006–009、015、052，准备动作不扩成额外性能场景，缺真实合同只阻塞对应工作；说明依赖原因和覆盖分母。
- [x] 2.4 实现 GraphDefinition 校验与 get/list/propose_patch/validate/diff，区分依赖 DAG 与有界控制循环，禁止未知 handler 和删除必要清理路径。验收：`test_graph_definitions.py` 覆盖 ORC-002、008、009、055，历史定义不可变、未验证适配不能激活；同步图定义格式文档。

## 3. 业务持久化与产物提交

- [ ] 3.1 在 `persistence/repository.py` 及数据库迁移中建立 D15 的账本、版本、scope、attempt、operation、资源、交付、激活谱系等约束及 UnitOfWork。验收：真实 PostgreSQL 的 `test_repository.py` 覆盖 ORC-019、033、039，重复业务键拒绝／幂等、旧 revision 写入失败；文档列出迁移及恢复方法。
- [ ] 3.2 实现 visit_id 的持久分配、receipt 唯一键及“结果＋下一步 intent”事务，恢复时对账 checkpoint/receipt 缺口。验收：`test_receipts.py` 注入 ORC-029 的两侧崩溃切点，同一次 replay 不重复副作用，新循环 visit 可以执行；记录事务边界。
- [x] 3.3 在 `artifacts/store.py` 实现分段写入、摘要、不可变 manifest 发布、作用域读取和可达性验证。验收：`test_artifacts.py` 覆盖 ORC-040、058，文件成功数据库失败产生 orphan，坏摘要／缺文件不能变有效交付；文档区分实验清理与证据 GC。
- [x] 3.4 实现事件、审阅和分析输入的不可变版本查询及精确引用，不允许消费 latest 替代冻结版本。验收：`test_versioned_inputs.py` 覆盖 ORC-046、048、049，交付 v2 到达不会污染正在消费 v1 的计算；同步查询与保留合同。

## 4. 所有权、冲突域与 checkpoint fencing

- [ ] 4.1 在 `runtime/leases.py` 实现 thread driver 租约与跨组／跨版本资源域原子预约、续租、接管及隔离；锁顺序固定。验收：真实双连接 `test_leases.py` 覆盖 ORC-033、035、036，无部分锁、双 owner 或过期即释放真实资源；记录共享宿主冲突策略。
- [ ] 4.2 在 `persistence/checkpointer.py` 实现 D10 的 FencedPostgresSaver，所有 checkpoint 和 pending writes 写事务与 claim/takeover 锁同一 lease 行，禁止独立连接先查后写。验收：`test_fenced_saver.py` 强制 ORC-034 两种事务顺序，覆盖同步／异步写入、interrupt、子图，旧 epoch 无法提交新 owner 后的写入；文档标明锁定 saver API。
- [ ] 4.3 实现失去 owner 后停止新派发、对账在途操作、保留实际资源状态和未知域隔离。验收：`test_owner_takeover.py` 覆盖 ORC-034、035、041，接管不改 operation_id、不绕过远端未知效果；记录恢复步骤。

## 5. 持久任务、事件及等待

- [x] 5.1 在 `runtime/tasks.py` 实现 enqueue/claim/finish、activation 去重及有界失败处理；在 `runtime/events.py` 实现 outbox/inbox/cursor、来源与 seq 校验。验收：`test_tasks_events.py` 覆盖 ORC-033、037、039，重复／乱序／通知丢失仍可重扫恢复，无“计数加一”式重复完成；同步事件合同。
- [x] 5.2 实现 D09 的机器等待登记、cursor 与状态 recheck 协议，END 只结束本次 activation，事件建立后续 activation。验收：`test_machine_waits.py` 用 barrier 覆盖 ORC-038，等待登记前后事件均不丢唤醒，无忙轮询；更新唤醒时序文档。
- [x] 5.3 在 `runtime/waits.py` 实现独立人工 interrupt 节点、DecisionEnvelope 版本／问题匹配与重复答复幂等。验收：`test_human_waits.py` 覆盖 ORC-043、044，恢复前不重做副作用，过期／错问题／旧计划答复不能批准新范围；记录人工必要条件。
- [x] 5.4 实现等待 TTL/watchdog、保留期限与已授权的撤压／清理或隔离调度。验收：`test_wait_watchdog.py` 覆盖 ORC-035、043、045，owner 崩溃后仍有限处置，超时绝不等于批准；同步资源等待合同。

## 6. 确定性脚本调用协议与受控执行器

- [x] 6.1 在 `runtime/runner.py` 定义注册脚本、结构化输入、秘密引用、submit/reconcile/cancel/collect 合同及 D11 操作状态机；实现 intent/submission_started 的持久化顺序。验收：`test_runner_protocol.py` 覆盖 ORC-030–032，unknown 不自动重新提交，不接受任意 shell；文档列出执行器须具备的幂等／查询能力。
- [x] 6.2 实现仅用于编排验收的持久 ControlledRunner fixture，独立进程保存副作用账本，支持断连、响应丢失、迟到、取消失败和恢复对账。验收：`test_controlled_runner.py` 覆盖 ORC-030–032、041，controller 硬杀后真实 fixture 副作用次数可独立核对；不接产品远端。
- [x] 6.3 分离观测 handle 与发压 operation，落实停止／收集、序列缺口、跨重启区间和 deadline/watchdog。验收：`test_observation_protocol.py` 覆盖 ORC-025、042、062，采样重启不得拼成连续稳态，缺测不是零；更新采样合同。
- [x] 6.4 实现所有权约束的 cancel/reconcile/cleanup 流程及资源残留交付。验收：`test_cleanup.py` 覆盖 ORC-024、028、035、045，取消确认和清理确认分别记录，不清理其他组资源；异常与残留有可恢复入口。
- [x] 6.5 将固定请求预算、并发、滑窗／分批与吞吐事件合同透传到执行器并校验返回结构，禁止编排层自行填测量数值。验收：`test_measurement_contract.py` 覆盖 ORC-026、059–064 的独立合成数据，失败消耗额度、accepted 不算完成、末桶时长和真实存活分母保持不变；公式实现与真实发压精度仍见第 13 组边界。

## 7. LangGraph 构建与分层调度

- [x] 7.1 在 `graphs/factory.py` 建立 Run/Group/Work/Analysis/Report 的真实 StateGraph 和小型引用 State，图编译只使用已注册 handler。验收：`test_graph_factory.py` 覆盖 ORC-002、027、070，45 节点映射、状态 schema 及合法出边完整，无 fixed edge 与 Command 重复派发；同步 Mermaid 映射。
- [x] 7.2 在 `runtime/driver.py` 实现 start/resume、租约绑定、receipt 恢复与 activation 版本冻结。验收：`test_driver.py` 覆盖 ORC-029、034、055、056h，真实图／持久 saver 恢复保持原输入身份，不让新图解释旧 checkpoint；记录 driver 启停约束。
- [x] 7.3 实现 R0/R1/RP/R2/R3/R6/R7，R2 持久派发独立组任务、R3 增量消费、R6 局部返工。验收：`test_run_graph.py` 覆盖 ORC-047、052、053，一组先交付即可分析，不等待所有组子图处在同一 superstep；控制状态没有全量样本。
- [x] 7.4 实现 G0/T0/T1/T2/T4/T5/T6/T8/TC/TB/GB/GW/GE，以独立工作任务调度、能力缺口和二次审阅闭合组状态。验收：`test_group_graph.py` 覆盖 ORC-015、036、046、051、070，组角色不按机器拆成无限 agent，原始执行事实不被复核覆盖；更新组事件合同。

## 8. 环境与测试工作节点

- [x] 8.1 实现 E1/E2/C1/C2/C3 的脚本调度 handler，覆盖连通、密钥配置、在位核验、已授权安装配置、复检报告的边界，所有实际命令通过注册适配。验收：`test_environment_graph.py` 覆盖 ORC-011–015，未知安装状态不自动重装，已有证据和真实安装历史区分，缺真实适配明确 blocked；同步节点职责表。
- [x] 8.2 实现 N00–N03 的输入守卫、源产物／规格核验、外部操作对账与准备状态。验收：`test_work_preflight.py` 覆盖 ORC-015、018、024、026、027，状态就绪与命令可执行分开，缺合同／未知副作用阻止依赖操作；更新内部节点图。
- [x] 8.3 实现 N04–N07 的基线观测、发压调用、稳态等待、有限停止与证据封存顺序。验收：`test_work_measurement.py` 覆盖 ORC-024–027、042、059–064，baseline 在操作前、稳态在停止前，异常仍进入收集及处置；不把合成结果登记成沙箱指标。
- [x] 8.4 实现 N08–N10 的自检、清理和交付，包括校验失败仍执行清理及硬崩溃后的恢复调度。验收：`test_work_delivery.py` 覆盖 ORC-024、028、041、046，每条失败出口具有证据与资源处置；节点 execution、data_quality、performance、cleanup 分列。

## 9. Agent 上下文与模型／人工边界

- [x] 9.1 在 `runtime/agents.py` 实现 root/group/work 的 owner 身份、NodeContext allowlist、证据范围读取与存储权限校验，不共享 messages。验收：`test_agent_context.py` 覆盖 ORC-020、021、069，跨组越权拒绝，确定性路径模型调用为零，万条日志只传引用和有界片段；更新上下文清单。
- [x] 9.2 实现 DiagnosisProposal 与语义复核输出结构，区分事实／假设／注册建议，模型不得覆盖测量值、扩大范围或运行未注册脚本。验收：`test_agent_proposals.py` 覆盖 ORC-022、023、046、057，伪造引用和越权提案被拒，无法判断保持显式缺口；记录审阅来源。
- [x] 9.3 落实 token、轮次、诊断时间预算与人工问题精确作用域；进入诊断前调用资源处置协议。验收：`test_agent_budgets.py` 覆盖 ORC-023、043–045、069，无无限模型轮询，未提供 usage 标记缺失而非编造 token 数；同步预算配置说明。

## 10. 分层验收、分析及报告编排

- [x] 10.1 在 `services/reviews.py` 实现 WorkDelivery→T6→GroupDelivery、manifest 重读、审阅追加及失败缺口分类。验收：`test_reviews.py` 覆盖 ORC-046、051、053，仅“成功”摘要不能通过，组复核不覆盖样本；更新审阅规则合同。
- [x] 10.2 实现 A1/A2/A3/A4/A5/A7 的版本冻结、规则校验、已注册统计／比较／Excel 服务调用与返工路由。验收：`test_analysis_graph.py` 覆盖 ORC-048、052–054、057、060–064，fixture 版本固定、不可比项保留、缺公式或单位明确阻塞，实际公式与工作簿服务不能被 stub 验收冒充；同步输入输出 schema。
- [x] 10.3 实现 F1/F2/F4 的事实装配接口、模型解释约束、引用及覆盖校验、归档 manifest；在 `services/closure.py` 实现 closure_revision CAS 和 partial archive。验收：`test_report_closure.py` 覆盖 ORC-049–052、057、058，未处理 running/waiting 不收口，部分归档有缺口，Excel／报告引用同版分析；更新归档合同。
- [x] 10.4 实现增量交付 dirty 标记及分析／报告专用重跑路径，复用已验收输入而不触发真实测量。验收：`test_incremental_analysis.py` 覆盖 ORC-047–049、052、053、068，迟到组版本生成新分析，旧版保留，rerender 的 runner submit=0；记录版本查询方法。

## 11. 图版本激活、收尾与证据复用

- [x] 11.1 在 `planning/changes.py` 实现 ImpactPlan、兼容规则与 reuse_from/reuse_candidate，区分阈值重算、规格重测、完成事件缺失。验收：`test_change_impact.py` 覆盖 ORC-054、055、056f，复用不改变原 work 身份或重复计数；同步兼容规则清单。
- [ ] 11.2 实现 D14.1 激活事务、lineage/supersedes、新 run/thread、request_id 幂等和 predecessor CAS。验收：真实数据库 `test_revision_activation.py` 覆盖 ORC-056a/c/d，两并发候选只有一个成功，事务前后硬杀可恢复且无半激活；记录接口及失败码。
- [x] 11.3 将 dispatch_generation/gate 与副作用提交许可接入 tasks/runner，区分未开始 superseded、已有许可在途、drain 和 cleanup 许可。验收：`test_dispatch_cutoff.py` 用 barrier 覆盖 ORC-056b/e，两种线性顺序副作用归属均正确，已关闭普通派发仍可做必要清理；同步竞态时序图。
- [x] 11.4 实现旧图有限收尾、旧等待撤销／到期、跨版本资源门控、旧交付订阅后的独立验收及复用候选期限。验收：`test_revision_drain.py` 覆盖 ORC-056e/f，无共享域抢跑、旧问题批准新范围、无限复用等待或迟到结果覆盖快照；文档列出处置状态。
- [x] 11.5 实现同图新计划 activation、回退创建新谱系和旧代码／定义保留检查。验收：`test_revision_rollback.py` 覆盖 ORC-056g/h、055，旧 activation 输入固定，回退不重放已发生操作，删除必要旧定义被阻止；更新运行手册。

## 12. 服务入口与可观察性

- [x] 12.1 在 `interfaces/service.py` 暴露 RunService、GraphService、QueryService，共用版本化输入与 request_id；提供可调用的最小命令入口及说明。验收：`test_services.py` 覆盖 ORC-001、010、044、052、056d，preview/start 使用相同 hash，status 可定位阻塞和证据，重复命令不重复派发。
- [x] 12.2 实现 D16 的结构化日志、trace、阶段耗时、token usage 可用性及复用计数；秘密脱敏、原始日志独立存储。验收：`test_observability.py` 覆盖 ORC-021、023、069，控制状态和上下文有界，调度指标不混入沙箱吞吐；文档列出字段及缺测表示。
- [x] 12.3 提供编排部署／恢复手册、临时数据库和受控 runner 启动配置，声明仅编排验证模式及真实模式适配检查。验收：从空测试库可运行最小五阶段 fixture 并查询谱系／证据，缺适配的真实测试请求明确拒绝；相关命令与实际输出存档。

## 13. 主变更覆盖账本与其余环境／报告边界

本组维护本轮编排里程碑与主 change 全量需求的边界，不能把设计完成等同业务脚本完成。测试执行代码及契约现已在15–26组展开；实际Excel／事实报告已接图，具体环境安装与真实数据验收仍须相应合同和实施证据；在全部对应 spec 获得实现和验收证据前，本 change 不满足归档条件。

- [x] 13.1 建立 `docs/orchestration/requirement-coverage.md`：逐条 WF/ENV/RT 映射 handler、测试层和实际业务实现状态，列出真实探测／安装／配置、产品适配、测量、PSS/Guest 归属、统计／Excel／报告渲染、L5 的未交付项及后续交付引用。验收：无仅凭 ORC 通过而将 ENV/RT 标记完成的条目；未实现项全部保留 planned/blocked 原因。
- [x] 13.2 建立后续脚本合同清单，逐节点记录输入输出、公式／完成事件、资源身份、幂等／取消／清理、未决 Q1/Q2/Q4–Q8/Q10–Q12 和验收样例要求；已审计结论直接引用，不安排泛化“重做源码审计”。验收：缺项能够精准阻塞相关真实操作，合成 fixture 值不自动成为真实默认值；将 `align-sandbox-benchmarks` 的旧任务标为由本清单替代的规划入口，避免双重执行。
- [x] 13.3 增加里程碑完成检查和归档覆盖核对，分别输出编排 L0–L4 状态、真实适配 L5 状态及全量 spec 缺口。验收：即使本清单所有编排测试通过，缺真实脚本证据时仍报告主变更未完成，不自动同步为已交付能力或归档；在手册中说明后续任务扩展入口。

## 14. 跨组件故障、端到端与最终回归

本组只验证前述已实现组件的组合行为；各模块单元／合同测试已放在对应实施组。

- [ ] 14.1 用真实 LangGraph、正式 PostgreSQL 后端及持久 ControlledRunner，构建双 worker 硬杀、连接中断、重复／乱序事件、artifact 提交失败及图版本切换组合故障测试。验收：`test_recovery_integration.py` 联合验证 ORC-029–045、056，独立副作用账本与最终所有权一致，未知资源无越权释放。
- [ ] 14.2 实现五阶段双环境组端到端、部分失败、人工等待、增量分析、only-analysis/report 场景。验收：`test_end_to_end.py` 覆盖 ORC-065–068，所有已选项都有显式处置、Excel／报告接口的产物同版本可追溯，真实产品测试状态仍单列。
- [x] 14.3 执行 T11 的有限种子属性与关键变异验收，并汇总全部 ORC-001–070（含 ORC-056a–h）映射与结果。验收：accepted 当完成、删除 fencing、缺测补零、跳过清理、latest 混读、超时批准等变异均被相关断言捕获；45 节点覆盖与受限上下文检查通过，seed 和失败记录可复现。
- [x] 14.4 执行限定范围测试集、OpenSpec 严格校验及文档／图／任务一致性检查，整理实施版本、运行命令、结果、未测／阻塞清单和后续业务脚本缺口。验收：可复查每项完成证据；既有草稿测试问题单列，未跑全库不声称全库通过，未通过 L5 不声称任何产品性能已验证。

## 15. 测试器合同、持久会话及独立 oracle

- [x] 15.1 实现 `sandbox_bench/contracts.py` 的方法条件、事件、资源、MetricValue schema 和 schema_version，承接图输入；同步 contracts 文档。验收：BTC-001/002/015 缺项、单位、bool/NaN、未知字段负例通过，真实参数不继承 fixture。
- [x] 15.2 实现 `registry.py` 的 B01–B10 子方法及 E01–E06 目录、三候选能力和适配状态、源码／部署版本绑定。验收：BTC-014/056 方法全量参数化覆盖，无方法时明确 blocked/unsupported；节点职责索引一致。
- [x] 15.3 实现 `journal.py` 与 `runner.py` 的阶段 receipt、稳定子 operation、预算 slot、resume/collect/cancel 接口，桥接 D11/D14。验收：BTC-019/049/050 的进程切点无重复提交／预算重置；文档区分正式仓储与离线文件夹具。
- [x] 15.4 建立 `tests/fixtures/benchmark/`、ManualClock、独立 ScriptedSandboxService 和 BCT-02 黄金预期文件，记录 provenance/hash；实现契约元数据校验。验收：预期不调用生产算法，fixture 可注入迟到／缺测／部分成功，BTC-016/017/034/041/042/045/047 可逐值核对。

## 16. 传输、完成观察和日志解析

- [x] 16.1 改造 `transport.py` 为有界流式读取，保留 headers/body/首数据时刻、错误类别、原始bytes与秘密隔离；同步协议说明。验收：真实本地HTTP服务运行 BTC-003 的慢滴／断流／redirect／超限，无自动重发副作用。
- [x] 16.2 实现 envd Connect 增量codec与结束语义，区分 JSON/protobuf 适配能力和 native exec 方法。验收：BTC-004/028 及既有协议回归覆盖分片、非零exit、缺end、流错误和完整hash；来源版本可追溯。
- [x] 16.3 实现 `completion.py` 四种终点及独立后验，替换旧强制exec路径；记录process_epoch、poll/deadline、迟到及unknown。验收：BTC-005–007 精确事件序列与槽位断言通过，状态成功不会被命令后验失败抹掉；更新完成合同说明。
- [x] 16.4 实现 `logs.py` Kuasar terminal parser、关联键、去重和起点证据检查。验收：BTC-008 的错kind/sid/run/profile、失败和缺起点不生成有效内部耗时，事件与观察时间分列；其他产品缺内部证据不造字段。

## 17. 三套产品生命周期与镜像适配

- [x] 17.1 将 adapter 草稿迁移为协议与三个产品实现，统一 inspect/list/effective_spec/placement/execute/reconcile/cleanup 接口；处理同名模块迁移。验收：BTC-014/015 与已有 adapter 测试可区分兼容和已废弃假设，文档明确接口版本。
- [x] 17.2 实现 Cube 的模板build、创建、暂停／恢复、fork、snapshot、删除和资源／节点映射，所有权清单保留成功child。验收：BTC-009/010/025/027/030 的受控协议测试通过，单位、分页限制、remoteStatus和部分失败均不掩盖；源码证据入文档。
- [x] 17.3 实现 Kuasar builder注册／触发／status持久ID、create状态观察、pause/connect/export及实际规格证据入口。验收：BTC-011/012 通过，保留keepSource与memory模式，不能以detail全局默认假证规格；缺runtime证据明确受限。
- [x] 17.4 实现 AgentENV 模板／cold、暂停恢复、fork、snapshot、分页、TTL和节点归属，按部署codec预检。验收：BTC-013/024/025/030 通过，能力未验证不静默选JSON，实际规格和配置额度区别保存；更新适配合同。

## 18. 发压算法与有限预算

- [x] 18.1 实现 `load.py` WINDOW，按完成事件即时补槽、持久预算和实际并发轨迹；同步算法时序文档。验收：BTC-016/019/020 黄金序列精确，accepted不释放槽，unknown外部在途上限生效。
- [x] 18.2 实现 BATCH 的批内限流、全部终结后gap、余数批和结束逻辑。验收：BTC-017 的B=C/B>C/失败场景通过，总wall=10黄金例及尾批无gap；记录batch参数和实际轨迹。
- [x] 18.3 实现有限开环scheduled计划、queue/drop策略和发生器延迟统计。验收：BTC-018 达不到目标λ明确记录，队列有上界、未发起与失败分开；文档列出可比条件。

## 19. 创建、暂停恢复、删除和命令用例

- [x] 19.1 实现 `fixtures.py` 和受控 `guest_probe.py`：来源/spec/cache核验、实际触页、内存nonce/文件hash和FIRST资格。验收：BTC-015/021/024/028/030，不能从磁盘重建nonce伪内存连续，不额外跑准备场景性能矩阵；记录复位规则。
- [x] 19.2 实现 B01 TEMPLATE/SNAPSHOT、B02 FULL/PREPARED 及共享build方法阶段机。验收：BTC-021/022 通过，三候选必要构建成本不遗漏，恢复只接管原build，独立job与共享成本分组；更新方法目录。
- [x] 19.3 实现 B03 PAUSE 与 B04 MEMORY/DISK 的非唤醒观察、状态断言及首次工作接口。验收：BTC-023/024 通过，源状态与进程内容证据完整，API／状态／首次工作时间分开；复入不新造近零样本。
- [x] 19.4 实现 B06 RUNNING/PAUSED 删除多终点与按资源归属的释放核验。验收：BTC-027/051 通过，权限失败非已删、共享源非泄漏、创建成功事实保留；记录残留处置。
- [x] 19.5 实现 B07 FIRST/STEADY/OUTPUT/ERROR、命令任务身份、完整输出与退出断言、超时对账。验收：BTC-028/029 通过，完整bytes/hash/exit匹配，远端未知命令不重发；同步用户／连接／协议条件。

## 20. 克隆和快照用例

- [x] 20.1 实现 B05 FULL/PREPARED/NATIVE，持久父／child操作、部分结果、源恢复和引用计数清理。验收：BTC-025/026/051 通过，源与兄弟隔离断言、父/child吞吐和FULL源恢复成本正确；文档说明组合实现。
- [x] 20.2 实现 B08 LOCAL/PUBLISHED，工作集触页、捕获与发布、独立恢复后验及源副作用检查。验收：BTC-030 通过，无独立内部事件不拆分假耗时，本地ready不当远端ready；产物大小口径与清理关系记录。

## 21. 底噪与全部密度方法

- [x] 21.1 实现 B09 PLATFORM/IDLE/PAUSED 的基线、cohort和窗口链，接资源服务。验收：BTC-031/044 通过，采集前置顺序正确，Guest净值缺证明时不可用而原始PSS保留；说明暂停卸载零值依据。
- [x] 21.2 实现 B10 GROWTH_LIST/GROWTH_READY 的独立停止状态机、存量身份差集、预算下界与原规则兼容偏差记录。验收：BTC-032/033/057 通过，净增长不替换新增健康，旧实例损失和不完整列表可识别；不运行官方全局reset。
- [x] 21.3 实现 B10 PROJECTION 的两公式、整数MiB兼容取数、锁定awk参考舍入及统一bytes计算。验收：BTC-034/057 黄金数16/18/14、阈值和部分健康负例通过；每值带模型／窗口版本和非实测标签。
- [x] 21.4 实现 B10 FIXED_REQUESTS.WINDOW/BATCH 与 SLO_VERIFY，接相同load engine和最终cohort。验收：BTC-035/036/056 三产品×方法矩阵通过，Q固定且失败消耗预算；无新建吞吐N/A、无阈值SLO blocked；条件和能力缺口完整。

## 22. PSS、CPU、实例归属与稳态采样

- [x] 22.1 实现 observe inventory/procfs，按boot/PID/start及sandbox generation关联角色组件，解析smaps_rollup/stat及必要辅助资源。验收：BTC-037–039 冻结proc树与受控Linux进程测试通过，读取竞态和权限失败不补零，文档明确支持平台。
- [x] 22.2 实现cgroup计数与去重范围、sampler持久句柄／有限spool、采样器开销及缺样事件。验收：BTC-039/040/050 通过，进程/cgroup重叠不相加，数据库失联无新副作用，丢样保留；部署读取权限明确。
- [x] 22.3 实现 cohort和steady模块，真实实例集合、首个合格窗口、时间覆盖与退出／重启切段。验收：BTC-038/042/043 通过，部分采集不得除完整N，中断不能拼成连续稳态；更新窗口配置合同。
- [x] 22.4 实现GuestDeductionProof校验和净值服务，保留raw、减项及来源，不内置未知减项。验收：BTC-044 对同域／跨域／越界／缺定义分类正确；无Q12合同的真实输入输出unavailable，不能声称净底噪验证完成。

## 23. 指标计算与可追溯数据交付

- [x] 23.1 实现 latency/quality 的逐请求分位数、失败分类、重复轮次分层和可比性键。验收：BTC-047 黄金分位数及全失败／混方法负例通过；N08/A3共用实现，oracle独立；同步公式版本说明。
- [x] 23.2 实现吞吐固定秒桶、最后桶、覆盖、迟到、overall与active区间并集。验收：BTC-045/046 黄金1.6/s与末桶2/s通过，未知桶不写0，分子分母同窗口；密度曲线具证据引用。
- [x] 23.3 实现资源时间加权、CPU秒／核占用／采样峰值、直接及共享摊销和增量视图。验收：BTC-040–044/046 的9核峰值、25MiB直接均值、缺覆盖及跨时钟负例通过；共享控制面只计一次。
- [x] 23.4 实现case evidence清单、不可变封存、自检与局部summary，并提供离线重算命令。验收：BTC-048/052 同一raw重复计算一致，篡改和缺序列拒收，原始日志不进模型上下文；产物schema与图交付一致。

## 24. 低优先级扩展执行骨架

- [x] 24.1 实现E01/E02/E03的版本化profile、固定工作量／reset断言、有限生命周期序列和中断分段。验收：BTC-053/054 的受控场景通过，未选择零执行，缺实际工作负载或适配blocked；不自动进入真实扩展实验。
- [x] 24.2 实现E04/E05/E06的注册压力／干扰／故障动作与持久补偿栈、作用域及期限校验。验收：BTC-055 每一步失败都能恢复或明确隔离，未知动作拒绝，未选择无副作用；文档列出所需真实合同。

## 25. 执行器和图的恢复集成

- [x] 25.1 把真实BenchSession接入N00–N10及D11 RunnerGateway，N04采样跨N05/N06，N07停止，N09清理；补接口文档。验收：BTC-058与ORC-024–028/059–064联验，不用fixture摘要冒充实际公式。
- [x] 25.2 实现迟到资源、取消与失败清理的恢复入口，校验同一operation／所有权与图版本提交许可。验收：BTC-049–051/058进程硬杀后无双提交、无清理他组资源，旧图收尾仍可执行；失败和残留可审查。

## 26. 跨产品合同、真实接入与最终验证

- [x] 26.1 执行三适配器受控合同矩阵与BCT-08关键变异测试，汇总BTC-001–058逐用例状态及源码证据。验收：accepted释放槽、gather滑窗、错分母、缺测补零等变异被断言捕获；所有B/E方法均有代码／合同／任务映射，未跑不写pass。
- [ ] 26.2 在已提供的真实环境合同下执行三产品最小功能及数据面／规格／placement／采集预检。验收：BTC-059输出产品版本限定证据；缺连接／权限／版本则记录blocked并保留未勾选，不以受控HTTP替代。
- [ ] 26.3 按已选有限条件执行基础性能、密度双方法及固定Q／资源联合测量，独立抽查ID与原始事件并重算。验收：BTC-060的可追溯结果、缺口、干扰及真实分母齐全；未确认Q12时净底噪不输出假数，扩展未选不执行。
- [x] 26.4 联合编排与测试器执行回归、严格spec校验、文档链接和任务覆盖检查，交付实现／验证／真实实测分层清单。验收：任务勾选逐项有证据，未完成真实用例及环境脚本不被归档进已实现能力，既有草稿失败不隐藏。

## 27. 来源产物语义与构建场景

来源产物身份由产物 kind、启动路径与生产者步骤决定，不按接口参数名称；模板与快照必须按实际 kind 拆方法（详见 runtime-evaluation RT-16／RT-24／RT-40／RT-41）。脚本接口、文件骨架与路由表见 [benchmark-design.md](benchmark-design.md) BCD-12；独立测试见 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-09。本组新增代码、脚本及独立单元／契约测试；未跑真实环境前只登记为 locally_verified。

- [x] 27.1 `sandbox_bench/contracts.py`：按 BCD-12 §12.1 新增 `SourceArtifactKind`／`LaunchPath`／`ProducerStep`／`CostScope` 与 `SourceResolution`（`source.v2`），扩展 `Source`（可选 `artifact_kind`／`launch_path`／`producer_step`／`producer_ref`／`resolution_ref`），并在 `CaseContract.validate_method` 加 B01.／B00. 条件校验。验收：BCT-09-REJECT 通过；既有 `kind ∈ {template,snapshot,image}` 用例不报错；缺 kind／path、或 `B01.SNAPSHOT.MEMORY` 配 `disk_restore`、`B01.TEMPLATE` 配 `snapshot_manifest` 均报错。
- [x] 27.2 `sandbox_bench/source_resolution.py`：实现 `Resolution`、`resolve_source`、`method_for_source`、`source_from_resolution`（BCD-12 §12.2／§12.4）。验收：BCT-09-SRC、BCT-09-ROUTE 通过；Kuasar `img`/`sbx`/`snp`、Cube 模板／快照同命名空间、AgentENV `template_id` 实为 snapshot 均能被区分；未覆盖组合 → `ScenarioBlocked('unresolved_source_identity')`；无法解析输出 `unverified` 而非按名称推断。
- [x] 27.3 在 `sandbox_bench/registry.py` 与 `sandbox_graph/catalog/registry.py` 登记 `B00.TEMPLATE_BUILD`、`B00.SNAPSHOT_BUILD`、`B01.SNAPSHOT.DISK`、`B01.SNAPSHOT.MEMORY`，并保留 `B01.TEMPLATE` ＋ `artifact_kind` 维度。验收：两处 registry 的 **B 系列** 方法键集一致（脚本比对，幂等）；`capability()` 对新方法默认 `unverified`；未知方法返回明确错误。注：E 系列命名本就不一致（`sandbox_bench` 为 `E01.WORKLOAD…`，`catalog` 为 `E01…`），属既有问题，不在本组修复，也不得因比对失败而改动 E 系列。
- [x] 27.4 实现 `sandbox_bench/scenarios_build.py` 的 `BuildScenario` 与 `Adapter.build_source`（BCD-12 §12.4／§12.5）：提交构建／捕获 job、轮询 ready／error 终点、持久产物身份与预算。不建 `scenarios/` 包（现有 `scenarios.py` 是模块，被 `from sandbox_bench.scenarios import ScenarioExecutor` 引用），`scenarios.py` 只做转发。验收：BCT-09-BUILD-READY／ERROR／LOST 通过；register／trigger 前先写 `phase=intent`、回执写 `phase=receipt`；ready 前持久产物身份；Kuasar 非 `kind='img'` 或缺 `e2b-img-` 前缀报 `error`；job 超时走 unknown＋对账。
- [x] 27.5 接线：`scenarios.py` dispatch 增 `B00.*`，B01／B02 走 `method_for_source` 路由，并把产物身份写入 `target_bindings.json`（BCD-12 §12.6）。验收：BCT-09-COST、BCT-09-SNAP 通过；同 `producer_ref` 复用只计一次并记 `reuse_of`；同一“模板”不同 kind 产生不同 method_id；既有 `template/snapshot/image→B01/B01/B02` 映射保持兼容。
- [x] 27.6 三产品 `Adapter.resolve_source`／`build_source` 覆写与入口子命令：Kuasar（`/v3/templates`→`/v2/templates/{tid}/builds/{bid}`→status，ready 取 `state.templateID`）、Cube（`POST /templates` build job）、AgentENV（`template_builder` sandbox→`publish_captured`）。验收：在 `runner.main` 新增 `resolve`／`build` 子命令（`--case`／`--output`，**不引入 `--mode`**），退出码沿用 `0`／`2`／`1` 约定；不走 legacy `validate_config` 分支；脚本幂等、按 job／产物 ID 对账、失败标 blocked；不照抄原仓库 fixture；适配缺失时不伪造成功。
- [x] 27.7 独立单元／契约测试：`tests/benchmark/unit/test_source_resolution.py`、`tests/benchmark/contract/test_build_scenario.py`（import `sandbox_bench.scenarios_build`）、`tests/benchmark/integration/test_b00_build_reentry.py`（独立 marker，不共用数据库／故障夹具）。验收：BCT-09 全组通过；oracle 不调用被测函数生成预期；错 kind、重复计税、缺 kind 与命名所致误归类均被断言捕获；受控通过只提升代码契约状态，不声称真实环境验证。

## 28. 创建端点拓扑与就绪信号

创建方法结论受入口拓扑与同步／异步语义约束，不得按接口名称推断（详见 runtime-evaluation RT-42 与 [alignment.md §9](../../../docs/source-audit/alignment.md)）。脚本接口、决策表与文件骨架见 [benchmark-design.md](benchmark-design.md) BCD-13；独立测试见 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-10。本组新增代码、脚本及独立单元／契约测试；未跑真实环境前只登记为 locally_verified。

- [x] 28.1 `sandbox_bench/contracts.py`：按 BCD-13 §13.1 新增 `Topology`／`EndpointKind`／`ReadinessSignal` 字面量，`CaseContract` 追加 `topology`／`endpoint_kind`／`readiness_signal`／`endpoint_ref` 及条件校验。验收：BCT-10-BLOCKED、BCT-10-COMPARABILITY 合法性分支通过；`co_located_cli` 必须 `ready_fd`＋`guest_ready`，远端拓扑禁止 `ready_fd`；既有用例不受影响。
- [x] 28.2 `sandbox_bench/endpoint.py`：实现 `EndpointResolution`、`resolve_endpoint`、`endpoint_requires_poll`、`endpoint_comparable`（BCD-13 §13.2／§13.4）。验收：BCT-10-TOPO、BCT-10-COMPARABILITY 通过；Kuasar router／node_direct、Cube、AgentENV 语义正确；未覆盖→`ScenarioBlocked('endpoint_unresolved')`；无法确认→`unverified`。
- [x] 28.3 `sandbox_bench/readiness.py`：实现 `read_ready_fd`，独立解析 `control_ready→ready→EOF`（BCD-13 §13.4／§13.5）。验收：BCT-10-READY-FD 通过；协议错／超时→`unverified`，不补零；不触发任何创建副作用。
- [x] 28.4 `sandbox_bench/adapters.py`：新增只读 `Adapter.probe_topology`（核验管理面/ingress 角色是否存在，不触发创建）与 Cube／AgentENV 同步语义声明；Kuasar router 同步、node 异步分列。验收：无管理面角色→`blocked` 不静默回退；不照抄原仓库 fixture。
- [x] 28.5 `sandbox_bench/measure.py`：`create_sync` 按 `endpoint_kind` 决定是否轮询；同步入口不得再报额外就绪延迟；异步入口 `api_complete_s` 与 `state_ready_s` 分列；`guest_ready` 单列并标白盒。验收：BCT-10-SYNC-ROUTER、BCT-10-ASYNC-NODE 通过；分子分母同端点。
- [x] 28.6 `sandbox_bench/runner.py`：新增 `endpoint` 子命令（`--case`／`--output`，**不引入 `--mode`**），退出码沿用 `0`／`2`／`1`；不走 legacy `validate_config` 分支。验收：只输出 `EndpointResolution`；缺证据标 `unavailable`。
- [x] 28.7 独立单元／契约测试：`tests/benchmark/unit/test_endpoint_resolution.py`、`tests/benchmark/contract/test_ready_fd.py`（独立 marker，不共用数据库／故障夹具）。验收：BCT-10 全组通过；oracle 不调用被测函数生成预期；跨端点误合并、受理当完成、协议错补零均被断言捕获；受控通过只提升代码契约状态。

## 29. 创建端点与来源身份的图侧绑定

本组是 D17 的**图侧**实现，与组 28 互补而不重叠：**组 28 只改 `sandbox_bench/`（脚本侧算法、CLI 与脚本契约测试）**；**本组只改 `sandbox_graph/`（计划编译、身份、目录与计划校验、图侧测试）**。二者共享同一 `CaseContract` 字段集（BCD-13 §13.1），但不互写对方模块。本组不重算脚本算法（那是组 28 与 BCD-12／BCD-13），只保证图把来源与端点身份**完整、无误、可区分**地传入并进入可比性键。契约见 [orchestration-design.md](orchestration-design.md) D17，测试设计见 [orchestration-test-design.md](orchestration-test-design.md) T13（ORC-071–073）与不变量 I13／I14。本组新增代码与独立图侧测试；未接真实环境前只登记为 locally_verified。

- [x] 29.1 `sandbox_graph/planning/compiler.py` `Case`：按 D17 新增 `artifact_kind`／`launch_path`／`producer_step`／`cost_scope`／`topology`／`endpoint_kind`／`readiness_signal`／`endpoint_ref`，且这些字段纳入 `method_id`／计划 hash。验收：ORC-073 通过；同一“模板”参数解析为 `image_artifact+cold_boot` 与 `snapshot_manifest+memory_restore` 产出不同 case／method_id，不合并；`B01.` 缺 kind／path 在编译期拒绝或标 gap，不默认补值。
- [x] 29.2 `sandbox_graph/planning/compiler.py` `Selection` 与 axes：把 `topology`／`artifact_kind`（可选）作为选择轴与条件，未指定不得放宽默认。验收：ORC-071 通过；远端拓扑出现 `ready_fd` 被拒；`co_located_cli` 必须 `ready_fd`＋`guest_ready`；未解析来源／端点不被名称填充。
- [x] 29.3 `sandbox_graph/services/scheduling.py` `expand_work` 的 `WorkKey.condition_hash`（`WorkKey` 定义在 `domain/models.py`，经 `planning/identity.py` `condition_identity` 计算）：来源与端点身份进入 `WorkKey` 与 `condition_hash`，使不同 kind／path／topology／endpoint 分属不同工作实例；`method_id` 含端点变体。验收：I13、ORC-073 通过；不能靠事后标签区分；重复轮次用新 repeat_id 而不改变身份语义。
- [x] 29.4 `sandbox_graph/catalog/registry.py`：登记来源与端点身份维度及实现状态；`capability()` 未验证默认 `unverified`。验收：ORC-071 通过；不按接口参数名称推断 kind／path／同步性；未知方法返回明确错误。
- [x] 29.5 `sandbox_graph/planning/identity.py` `missing_required_identity` ＋ `services/workflows.py` `validate_catalog`（R1）＋ `compiler.py` `Case.validate_identity`：自洽校验（`co_located_cli ⇒ ready_fd ∧ guest_ready`，远端拓扑禁 `ready_fd`）在计划期失败；缺必需来源身份→`blocked('SOURCE_IDENTITY_MISSING')`。只读探测／记录 `management_plane_present` 与 `readiness_signal` 由脚本侧（组 28 `Adapter.probe_topology`）提供，图侧不触发创建／构建副作用。验收：ORC-071、I14 通过；不静默回退 node_direct。
- [x] 29.6 `sandbox_graph/services/scheduling.py` `expand_work`：work body 经 `planning/identity.py` `binding_payload` 携带 `endpoint_ref` 与来源／端点身份，供 N05 构造 `Operation.input`（`runtime/runner.py` 的 `input_hash` 随之覆盖该绑定）；同一身份进入 `condition_hash`，重入沿用同一身份。验收：ORC-072 通过；分子分母同端点。
- [x] 29.7 `sandbox_graph/planning/identity.py` `comparability_key`／`is_whitebox`／`BLACKBOX_ENDPOINTS`：比较键显式含端点 kind 与白盒／黑盒标记；白盒 `guest_ready` 不入黑盒比较集合。验收：ORC-072、I13 通过；异端点样本不得算同一分位数。
- [x] 29.8 图侧独立契约测试：在 `tests/orchestration/unit/`（`test_endpoint_source_binding.py`）覆盖 ORC-071–073 与不变量 I13／I14；oracle 用显式字面量，不调用被测归约／路由函数生成预期。验收：BCT-09／BCT-10 的图侧对应断言通过；变异反例（合并白盒／黑盒、无管理面仍放行、按名称推断 kind）均被捕获；不声称 benchmark 脚本算法或真实产品语义已验证。
- [x] 29.9 `sandbox_graph/planning/changes.py`：身份字段（`artifact_kind`／`launch_path`／`producer_step`／`cost_scope`／`topology`／`endpoint_kind`／`readiness_signal`／`endpoint_ref`）变更即要求新测量（D17→D14）；复用判定依赖 `condition_hash`（已含身份，`services/reuse.py` 比较该键）拒绝跨身份复用。验收：身份变更用例通过；旧 work 归属不变；白盒／黑盒端点不互相复用。

## 30. cgroup 真峰值采集与 mock 端到端指标验收

回应「无真实环境也能用 mock 验收」「开 cgroup（不设 quota）即可拿真峰值」。本组新增库能力与 mock 端到端图编排 UT；**不声称真实环境已验证**（`environment_verified=false`）。

- [x] 30.1 `sandbox_bench/resources.py`：新增 `parse_cgroup_value`／`sample_cgroup`，在**不设 quota** 的 cgroup 下读 cgroup v2 `memory.peak`／`memory.current`（v1 回退 `memory.max_usage_in_bytes`／`memory.usage_in_bytes`）与 `cpu.stat` 累计 CPU。验收：冻结 cgroup fixture 下 v2／v1 正确；缺峰值→`unavailable` 不补零；相对路径／非法值拒绝。注意：`memory.peak` 是**真高水位**但**不是 PSS**，不得替代 PSS 合同。
- [x] 30.2 `sandbox_bench/metrics/resources.py` `cgroup_true_peak` ＋ `metrics/__init__` 导出：对有效 cgroup 样本取最大真峰值。验收：max over valid；空→`unavailable`；与 `pss.sampled_peak_bytes` 分列，均不得互相替代。
- [x] 30.3 mock 端到端图编排 UT：`tests/benchmark/integration/test_create_metrics_orchestration.py`，真 `build_work`＋真 N00–N10 `work_handlers`／`OperationService`／`BenchGateway`／`BenchSession`／`analysis`，仅 mock 适配器（`Scenario`）与采样器（`FakeSampler`）。验收：N00–N10 全 `succeeded`；`analysis.json` 同时具 `latency` 分位数、`throughput.overall_per_s`、`cpu.sampled_peak_cores`、`pss.mean`；≥2 资源采样；不声真实产品语义已验证。
- [x] 30.4 把 cgroup 真峰值接入 `analysis` 报告：让 `records['resource_series']` 可包含 cgroup 样本，并在 `resources` 下输出 `cgroup_true_peak`／ `cpu.cgroup_seconds`，与 PSS 均值与采样峰值**分列**。验收：带回溯与覆盖；缺 cgroup 证据标 unavailable 不补零；不得将 cgroup 峰值当作 PSS 峰值。需真实 cgroup 才能取得 environment_verified。

## 31. sched_switch 真 CPU 峰值（按进程类）

内核无 CPU 峰值高水位计数器（与 `memory.peak` 不对称）；唯一精确方法是从 `sched_switch` 重建每个任务的 on-CPU 区间并取最大并发。采集**按进程类**：如 Kuasar 的沙箱（work）进程与沙箱管理（management）进程分别归属，复用采样器的 `roles`／`components` 模型。本组新增纯重建库与单测；eBPF attach 路径需特权与真环境，只登记为 planned／live。

- [x] 31.1 `sandbox_bench/sched_peak.py`：`reconstruct_on_cpu`（从 `{t_ns,prev_pid,next_pid}` 重建 on-CPU 区间，处理 double_on／窗口外边界）与 `peak_from_sched`（扫线法取最大并发；`true_peak_cores` 为 scope 内任务**并集**，`by_role`／`by_component` 为**可重叠视图**）。验收：冻结 sched 事件轨迹下 1µs×3 尖峰的真峰值为 3，而 100ms 累计采样等价值 <0.001（证明无采样误差）；空/非法输入拒绝。
- [x] 31.2 分类归属：`attribution_from_inventory` 由采样器风格 inventory 行构建 `pid→{roles,components,sandbox_id,generation}`；支持线程归到已验证 leader。验收：management 与 work 不同类峰值正确（管理=1、work=3）；冲突/空 inventory 拒绝；分类视图不求和。
- [x] 31.3 `BpfSchedSwitchCollector`（只读、`sched_switch` tracepoint）：`availability()` 检测 root／clang／bcc／tracefs；不可用→`unavailable` **不伪造峰值**；`start` 在不可用时抛错。验收：无特权/无 bcc 环境下返回明确 reason；不 attach 也能跑完单测。
- [ ] 31.4 接入与真实验证：将采集器接入 N04–N07（与 `ResourceSampler` 并行），在 `analysis` 下输出 `cpu.true_peak_cores`／`by_role`／`by_component` 与 `sampled_peak_cores` **分列**，并用 pre-roll/post-roll 保证窗口内 on/off 完整。验收：在**特权＋真内核**环境 attach `sched_switch` 并与 sampled 对比；缺 eBPF/特权时回退 sampled 并标 unavailable；不得把 trace 时钟与 Python monotonic 混算。

## 32. 归属完整性：无主数据不得进入分量运算

保证采集数据能**脚本批量分析**、**一一对应到组件进程**、**分量运算只用有主数据**。身份是完整进程元组（`environment,node,boot_id,pid,start_ticks`），pid 单独不够（复用会串主）。脚本侧 `analyze_case`／`recompute_case` 与在线路径共用同一归属报告，可从冻结证据逐案重算。

- [x] 32.1 `sandbox_bench/attribution.py`：`identity_key`／`process_key`／`expected_identity_keys`／`inventory_index`／`attributed_series`／`attribution_report`。验收：缺字段/类别为空/pid 复用均拒绝；未在 inventory 的进程被隔离到 `unattributed` 且**从分量运算中移除**；inventory 声明的类归属**回注**样本行。
- [x] 32.2 `sandbox_bench/analysis.py`：接入归属层——无主行被过滤且标 `resources.status=partial`；`expected_components` 用 `expected_identity_keys`（声明 inventory，非观测集），缺已声明组件→覆盖面下降而非静默丢弃；输出 `resources.attribution`。验收：无主 pid 不现于 `by_component`，分量集合只含声明类。
- [x] 32.3 `sandbox_bench/session.py` `records()` 落存 `inventory`；mock 端到端图编排 UT 断言图→封存→`recompute_case` 的 `resources.attribution` **逐字节一致**且 `unattributed_rows==0`。验收：批量重算与在线分析同口径。
- [x] 32.4 `sandbox_bench/sched_peak.py`：`attribution_from_inventory` 保留完整 `identity` 并拒绝 pid 复用；采集器归属同样“一一对应进程”。验收：pid 对应两个身份时报错；类视图不求和。
- [ ] 32.5 真实环境 inventory 发现适配：用各产品真实探针（进程/cmdline/cgroup/boot_id/start_ticks/sandbox generation）生成带 `roles`／`components` 的 inventory，并验证与真实沙箱／管理进程一一对应。验收：需真实环境；缺探针则组件归属 blocked，不得把未知进程静默归总。

## 33. 拓扑感知的组件清单与 inventory 目标

采集对象是**产品 × 集群拓扑**的函数：哪些节点存在、管理面还是数据面、是否共置，决定该在哪些节点采哪些组件。目录与映射见 `sandbox_bench/topology.py`；in-guest 进程永不算宿主机沙箱进程。

- [x] 33.1 `sandbox_bench/topology.py`：声明三产品 × 三分类的 `Component` 目录（`management_plane`／`data_plane_management`／`sandbox_runtime`／`shared_auxiliary`，含 `per_instance`／`shared`／`process_names`／`argv_markers`／`confidence`），并标记 in-guest 排除项。验收：Kuasar `cluster-ctl{registry,router,placer}` 归管理面、`node-ctl *` 归数据面、`cloud-hypervisor` 为沙箱运行时；Cube=`cube-runtime`／`cube-hypervisor`；AgentENV=`firecracker`。
- [x] 33.2 `Topology` 模型 ＋ `planned_components`／`expected_component_ids`／`inventory_plan`：按节点角色给出声明组件（共置不重复）。验收：多机拆分管理／数据面；单机共置不重；空节点/未知角色拒绝。
- [x] 33.3 `match_process`／`build_inventory`：只读发现提示；共享二进制（`node-ctl`／OpenResty）需 argv marker 区分，否则 unresolved；per-instance 组件缺 `sandbox_id` → unresolved；**不得归属未知进程**；声明未发现→`missing`。验收：无主进程被隔离，保留行均带组件。
- [x] 33.4 `runner.py` 新增 `topology` 子命令（`--config`／`--output`，不引 `--mode`）：脚本批量输出拓扑→组件清单与 inventory 目标。验收：退出码沿用 `0`／`2`；只读无副作用。
- [ ] 33.5 真实拓扑发现：用只读探针（systemd unit／`/proc` cmdline·cgroup／`boot_id`·`start_ticks`／容器运行时）把声明组件映射到真实宿主机进程与 `sandbox generation`，并验证单机与多机两种部署。验收：需真实环境；不可解析的组件标 `blocked`，不得静默归总或补零。

## 34. 测试间环境还原（小批量多轮删除＋残留核验）

测试与测试之间还原环境：只删除**本轮 run_id 归属**的沙箱，小批量多轮，删后核验残留；预热池／空闲／基线实例永不删除（审计见 [alignment.md §10](../../../docs/source-audit/alignment.md)）。规格 `RT-43`，设计 [benchmark-design.md](benchmark-design.md) BCD-14，测试合同 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-11。

- [x] 34.1 `sandbox_bench/restore.py`：`select_batch`／`restore_environment`，小批量多轮删除＋基线排除＋残留重列。验收：BCT-11-BATCH／WARM／ASYNC／FAIL／SCOPE 通过；非空残留→`incomplete`；失败计入残留。
- [x] 34.2 `runner.py` 新增 `restore` 子命令（`--config`／`--output`，不引 `--mode`），退出码沿用 `0`／`2`（incomplete）／`1`；缺 `run_id`／`batch_size`／`max_rounds` 报合同错误。
- [x] 34.3 独立单元测试：`tests/benchmark/unit/test_restore.py`（独立 marker `restore`，字面量 oracle）。验收：BCT-11 全组通过；不调用被测函数生成预期。
- [ ] 34.4 接入 N09／测试间还原：将 `restore_environment` 接入会话收尾（与所有权清理区分；预热池不列入）。验收：真实环境下单机与多机删除与残留核验；缺真实环境时保持 blocked。

## 35. 测试间环境还原的图编排接线

规格 `evaluation-workflow` WF-37、详设 `orchestration-design.md` D19、图侧测试设计 `orchestration-test-design.md` T14（ORC-074／075）。

- [x] 35.1 图侧 N09 接线：`session._N09` 在按账本逐资源清理后，当环境声明 `restore` 还原合同时执行出账本扫描（`restore_environment`），并集入 `cleanup_status`；未配置时跳过。验收：ORC-074（孤儿被扫描删除、预热／基线不删）通过。
- [x] 35.2 图侧单元测试：`tests/benchmark/unit/test_restore_sweep.py`（ORC-074／075）：扫描删除孤儿、排除预热池、残留→`incomplete`。
- [x] 35.3 残留到归档链：残留使 `cleanup_status=incomplete`，`ClosureService.classify_scope` 判 `partial`。验收：端到端断言通过。
- [ ] 35.4 真实环境：单机与多机删除与残留核验，验证预热池（Kuasar runner 单元／Cube TAP-cgroup 池／AgentENV 设备池）在真实部署下不被扫描删除。验收：需真实环境；缺环境时保持 blocked。

## 36. 可选指标目录、逐场景矩阵与 B00 指标/背景密度补全

- [x] 36.1 固化可选指标目录与逐场景适用矩阵：需求 RT-44，详设 [benchmark-design.md](benchmark-design.md) BCD-15。验收：目录逐项编号（含单位/类别/来源/实现）；每个场景标 R/O/—；不适用项显式标 `not_applicable`。
- [x] 36.2 补 B00 指标实现（B-03/B-04/B-05、A-01/A-02）：构建期 PSS/CPU 峰值、产物大小与新增存储、冷/热缓存分列；当前 `_build_terminal` 恒为 `None`/`unknown`。验收：三产品受控 fixture 下构建期资源与产物大小可采；缺证据标 `unavailable` 不补零；TDD。
- [x] 36.3 B00 背景沙箱密度条件轴：新增合同字段（预置 N 背景沙箱）＋规格／详设／测试设计（BCT）＋图编排（WF/D/T）＋ TDD；构建测量取**增量**（total − baseline）。验收：不同背景密度档位下构建时延与增量资源可测；不引入 B00 并发构建密度。
- [ ] 36.4 真实环境：在三产品真实部署下采集 B00 构建期资源、产物大小与冷热缓存，并验证背景密度档位。验收：需真实环境；缺环境时保持 blocked。

## 37. 可调背景条件目录与逐场景匹配

- [x] 37.1 固化可调背景条件目录与逐场景适用矩阵：需求 RT-45，详设 [benchmark-design.md](benchmark-design.md) BCD-16。验收：条件逐项编号（含取值/单位/选取方式）；每个场景标 R/O/—；未选维度不得静默放宽。
- [x] 37.2 `case.v1` 合同扩展背景条件字段：N-01/N-02/N-03、N-04、C-01–C-05、W-01/W-02、SP-01/SP-02、D-01–D-04、L-01/L-02、TL-01–TL-04、EP-01/EP-02 与校验；与现有 `density`/`resources`/`source` 字段融合不重复。验收：各场景 R 集必填、O 集可选；未选标 `not_selected`；不得默认笛卡尔积；TDD。
- [x] 37.3 逐场景条件选择接入：将矩阵接入用例→合同转换（N00/T2），并以实际实现值对账（SP-03、实际 N/C/λ）。验收：声明与实现不一致标条件受限；未达成档位不当达成。
- [ ] 37.4 真实环境：在三产品真实部署下按各场景 R 集声明并采集背景条件，验证单机共置与多机。验收：需真实环境；缺环境时保持 blocked。

## 38. 逐场景详细规格与合同样例

- [x] 38.1 产出逐场景详细规格：[scenario-spec.md](scenario-spec.md)，合并指标矩阵（BCD-15）与条件矩阵（BCD-16），每场景含定义／方法身份／指标(R/O/—)／条件(R/O/—)／完成判据／正确性断言／失败重入。验收：覆盖 B00–B10 与 E01–E06；ID 与 RT-44／RT-45 一致。
- [x] 38.2 逐场景合同样例与校验：为每个场景产出 `case.v1` 样例（含 R 集指标/条件）并纳入校验测试；缺 R 集则合同错误、O 集缺省标 `not_selected`。验收：三产品×方法矩阵的样例可解析；不默认笛卡尔积；TDD。
- [ ] 38.3 真实环境：逐场景在真实部署下按样例采集并回收证据。验收：需真实环境；缺环境时保持 blocked。

## 39. 真实环境验收手册与测试文件骨构

- [x] 39.1 产出真实环境验收操作手册：[acceptance-manual.md](../../../docs/acceptance-manual.md)，按 PostgreSQL／三产品／特权机／真实发现／还原／B00／条件分类，逐项给出先决条件、命令、期望证据与验收判据，并标出待补测试文件。
- [x] 39.2 补真实环境组测试文件骨构（离线可写、运行需 DSN/产品）：`test_repository.py`／`test_receipts.py`／`test_leases.py`／`test_fenced_saver.py`／`test_owner_takeover.py`／`test_revision_activation.py`／`test_recovery_integration.py`／`test_end_to_end.py`；缺 DSN/产品时显式 `skip`（不写 pass）。验收：无 DSN 时 skip；有环境时执行手册中的验收。

## 40. 操作同步/异步终点映射

规格 RT-46，详设 [benchmark-design.md](benchmark-design.md) BCD-17，测试合同 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-13，图侧详设 [orchestration-design.md](orchestration-design.md) D20，图侧测试 [orchestration-test-design.md](orchestration-test-design.md) T15（ORC-076）。审计 [alignment.md §11/§12](../../../docs/source-audit/alignment.md)。

- [x] 40.1 `sandbox_bench/operation_sync.py`：`completion_plan(product, operation, *, topology)` 返回同步性/端点/`ready_state`/`poll_required`/`verified`；`await_operation(adapter, handle, plan, ...)` 对异步操作轮询到 `ready_state`。验收：BCT-13-SYNC/ROUTER/POLL/GONE 通过；未覆盖→`verified=False`。
- [x] 40.2 图侧 N05 接入：N05 按 `completion_plan` 决定是否轮询，同步不重复计量、异步分列 `api_complete_s`/`state_ready_s`，入口分列（D20）。验收：ORC-076 通过。
- [x] 40.3 独立单元测试：`tests/benchmark/unit/test_operation_sync.py`（BCT-13）。验收：全组通过；字面量 oracle，不调用被测函数生成预期。
- [ ] 40.4 真实环境：在三产品真实部署下确认暂停/恢复/删除/快照的同步性与终点（含 Kuasar router/node 与 AgentENV fork、Kuasar export、Cube snapshot 的待实证项）。验收：需真实环境；缺环境时保持 blocked。

## 41. 创建时延客户端口径（对齐官方基准）

规格 RT-47，详设 [benchmark-design.md](benchmark-design.md) BCD-18，测试合同 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-14。对齐 Cube 官方 `examples/cube-bench/runner.go`（`client.Do` 停表 + 连接复用 + `RunWarmup`）与 AgentENV E2B 基准（持久连接）。审计见 [alignment.md](../../../docs/source-audit/alignment.md)。

- [x] 41.1 `sandbox_bench/transport.py`：新增 run 级 `Session` 连接池——按 `(scheme,host,port)` 复用 keep-alive，`max_per_host>=并发`、`idle_timeout_s` 显式，run 结束 `close()`；不再每请求新建连接。验收：BCT-14-REUSE 通过（假连接计数器，N 请求建连 ≤ `max_per_host`）。
- [x] 41.2 停表点对齐：同步创建 `create_latency = headers_s`（响应头）；`body_read_s = complete_s - headers_s` **分列**，不计入创建时延。`Adapter._sandbox`/`measure.create_sync`/`scenarios` 的 `accepted.observed_at` 改用响应头时刻。验收：BCT-14-HEADER 通过（T1<T2，完成时延取 T1）。
- [x] 41.3 预热：`measure.warmup(adapter, count, ...)` 建立并复用连接且**排除统计**；记录轮次/条数/是否可用。验收：BCT-14-WARMUP 通过（warmup=3、实测=10 → `n_attempts==10`）。
- [x] 41.4 口径版本与可比性：`caliber_version`（`create.header_stop.reuse.warmup.v1` / `create.header_stop.oneoff.v1`）进入可比性键；跨版本→不可比；缺口径/缺复用/缺预热证据→`unverified`。验收：BCT-14-CALIBER 通过。
- [x] 41.5 独立测试：`tests/benchmark/unit/test_create_caliber.py`（BCT-14，独立 marker；手动时钟/计数器，不用真实 sleep 猜时序）。验收：BCT-14 全组通过；字面量 oracle，不调用被测函数生成预期。
- [x] 41.6 文档：更新 [benchmark-wiring.md](../../../docs/benchmark-wiring.md)（客户端口径与预热）、[benchmark-matrix.md](../../../docs/benchmark-matrix.md)（口径版本为轴）、[acceptance-manual.md](../../../docs/acceptance-manual.md)（同环境与官方口径对照步骤）。
- [ ] 41.7 真实环境对照：在同一环境用**官方口径**与**本口径**各测一轮 B01 同步创建，确认收敛（同规格/并发/请求，排除预热），并记录 `api_headers_s`/`api_complete_s`/复用与预热证据。验收：需真实环境；缺环境时保持 blocked。

## 42. 生命周期模型、策略目录与状态合同

- [ ] 42.1 新增 domain/agent_lifecycle.py 严格模型、事件状态表与版本化 RepairPolicy，区分生命周期 phase 与 execution/data/performance/cleanup；同步 docs/orchestration/contracts.md。验收：ALU-01、04、20 覆盖全部事件边/非法跳转、最后一次成功、超期/无进展/safety_budget 处置和等待不算闭合。
- [ ] 42.2 新增 catalog/repair_actions.py，登记 N01/N07/N09 原有动作的 handler、schema、效果类别、权限、postcheck/reconcile/cleanup；建立 45 节点 policy 条目，未实现动作标 unavailable。验收：ALU-01/03、ORC-077/078，不能仅按 action 名执行未登记代码。
- [ ] 42.3 新增 domain/evolution.py 的候选、门禁、审查与晋级合同；输入摘要与 provenance 分离，拒绝未知字段；同步合同文档。验收：ALU-14/16/18 的候选身份与摘要变更反例。
- [ ] 42.4 在 tests/orchestration/conftest.py 建独立失败事件、假模型、效果账本和预算夹具，测试预期用字面值和手工依赖图；新增 schema 兼容测试。验收：旧记录可只读，新增字段不重解释历史；G1 收集清单非空且包含全部参数变体。

## 43. 持久事务、预算与修复派发

- [ ] 43.1 新增 services/agent_lifecycle.py 及 ledger kinds，失败 receipt 到 lifecycle 唯一；实现 advance CAS 和 decision_ready outbox 原子提交，消费幂等；同步状态/恢复文档。验收：ALU-06/07、ALI-04 的 decision 落盘切点不会丢修复意图。
- [ ] 43.2 重构 services/remediation.py，使 request_id/输入冲突、owner fence、work revision、gate、额度预留、attempt 分配、order/job/outbox 同事务；删除固定 dispatch_generation。验收：ALU-07/08 与 ALI-03，双连接竞争最后额度仅一个有效 order。
- [ ] 43.3 实现 lifecycle/lineage 双层预算、模型最大费用预留、usage 未知扣预留、观察与候选独立计数、授权追加额度；同步配额说明。验收：ALU-04/05 与 ALI-03，换 context/policy/run 和并发调用不能刷新或透支。
- [ ] 43.4 实现 ResourceDispositionReceipt 的生产/时效复核和共享 work lease，禁止 resources_disposed 布尔值绕过；未知效果保持原 operation 对账。验收：ALU-08/09、ALI-04，旧 owner 拒绝写入；无现场许可仍可做受限离线诊断。
- [ ] 43.5 实现 outcome/postcheck/预算结算/后继 outbox 原子提交及 backoff/watchdog；明确效果失败、未知、无进展和耗尽分流。验收：ALU-06、ALI-04，修复 A 失败可换 B，未知操作不重发，硬杀接管不重复计次；同步故障手册。

## 44. 节点监督、provider 和受限 agent team

- [ ] 44.1 扩展 runtime/agents.py、node_agent.py，生成 node_supervisor_id 和最小 ContextPacket，按证据 capability 读取并限字节/脱敏；保留现有 owner 身份兼容。验收：ALU-02/03，跨组、私有父会话、路径穿越及秘密内容均拒绝；正常成功调用模型为零。
- [ ] 44.2 新增 runtime/agent_bridge.py 及应用内 provider 接口，实现 capability 检查、幂等 submit、按 request_id lookup、status/cancel/collect；记录模型版本与 usage 上界。验收：ALU-12，丢响应不换 ID 重发，取消须终结回执，缺能力只阻塞依赖该能力的路径。
- [ ] 44.3 实现本项目 HTTP/RPC bridge 协议与配置/认证/scope 文档、受控协议服务测试；不猜测宿主私有 API。验收：ALU-12 的协议错误、认证失效、超时、重复回执反例；应用内 provider 可独立验收，真实宿主留给 49.4。
- [ ] 44.4 实现 supervisor/investigator/coder/reviewer 角色任务、共享预算和默认一层团队限额；接入统一 TaskBackend/Worker 而非内存 spawn。验收：ALU-13、ORC-088，任务/并发/深度超限原子拒绝，reviewer 与 coder 上下文隔离。
- [ ] 44.5 接通已知规则直达修复和未知问题诊断，提案仅选已注册动作或候选路线；更新 provider 操作指南。验收：ORC-081、ALU-03/06，无模型时确定性修复仍工作，越权提案不执行，模型不可用不无限排队。

## 45. 图内修复循环、恢复计划与分层收口

- [ ] 45.1 在 supervision/remedy handlers、bootstrap 和 work graph 接通 draining→diagnosing→remediating→postcheck 循环，原图与修复共享 work 锁。验收：ALI-01 的真实 StateGraph 节点/事件序列、另一组持续运行；失败 receipt 不被覆盖。
- [ ] 45.2 实现 compile_recovery 和恢复启动复核，透传 input_refs、合成 result_refs，按失效依赖重验 N01/N02/N03，替代固定“下一编号节点”。验收：ALU-10、ALI-02，fixture 已删、TTL 过期、输入版本改变不跳过准备；同步恢复合同。
- [ ] 45.3 接入 N04–N08 测量/采样保护、原 operation 接管和新 measurement_generation 授权，禁止性能失败筛选重试。验收：ALU-11、ORC-084、ALI-02，断段/基线失效保留 insufficient，有效性能 fail 不新增测量请求。
- [ ] 45.4 接通 N07/N09/N10 必经收尾、attempt delivery 与 work fulfilled 分离、T6 缺口返工；closure/cancel/supersede 查询所有生命周期及隔离域。验收：ALU-20、ALI-02，等待/未知资源不能假收口，取消保留 cleanup/reconcile。
- [ ] 45.5 为 E/C、R/G、A/F 节点接入同一 policy 与自身 scope，缺具体动作走显式 unavailable/候选；更新节点自动化及内部 Mermaid 文档。验收：参数化图测试验证越 scope 派发拒绝、A/F 不创建测量、环境安装未知不盲目重复。

## 46. 隔离候选编码与修订循环

- [ ] 46.1 实现 ChangeProposal、base commit/输入快照冻结、候选 workspace 和允许路径 diff；不修改用户脏工作树。验收：ALU-14，base 不一致拒绝、未登记改动不混入、每次冻结 revision 不可变；同步候选合同。
- [ ] 46.2 新增 runtime/candidate_worker.py 及隔离后端协议，限制挂载/网络/秘密/argv/时间/产物；交付至少一种真实隔离实现和明确 capability 检查。验收：ALU-14 与 ALI-06，worktree 本身不算安全隔离，生产 canary 不可读、主树不可写。
- [ ] 46.3 接入 coding provider，使 agent 真正编辑候选代码/图/测试，冻结 revision 后调用可信测试；失败以最小证据反馈并生成新 revision。验收：ALU-15、ALI-05，真实 patch r1 fail→r2 pass，最后预算内成功允许继续，失败历史保留。
- [ ] 46.4 实现实际 diff/影响分类 R0–R3 与 independent reviewer，检测测试/CI/依赖/权限/口径变化，复用覆盖范围明确的旧授权。验收：ALU-16、ORC-086，模型低风险自评无效，修改可信基线/削弱门禁不因文件类型而自动晋级。
- [ ] 46.5 实现候选取消、provider 响应未知对账、无进展停止及隔离环境回收；保留候选和日志。验收：ALU-12/15、ALI-05/06，终结前不释放正在运行的隔离实例身份；更新隔离部署与故障文档。

## 47. 测试门禁执行与可信证明

- [ ] 47.1 实现可信风险 profile、required_gates/test_ids 和 N/A 规则，候选外固定 suite/命令/依赖锁；同步 G0–G6 配置说明。验收：ALU-16/17，缺文件、零收集、关键 skip/xfail、参数变体遗漏均拒绝。
- [ ] 47.2 实现可信 runner 的 collect/run/日志/JUnit 对账，候选新增测试单独执行；结果以受信服务身份写 GateManifest。验收：ALU-17/18、ALI-06，自造报告、修改基线、候选写输出路径和进程超时不能得到 pass。
- [ ] 47.3 实现候选/base/suite/policy/环境/bundle/producer/有效期绑定及兼容证明复用，区分 fail/blocked/not_applicable。验收：ALU-18，任何关键摘要不符失效，人工授权不能覆盖 failed test。
- [ ] 47.4 提供受控 lifecycle gates 执行/查询入口与开发、持久化、隔离、发布验收 profile；登记所需 pytest markers，实际验证 ATG-04 argv。验收：G1/G2 完整收集；缺 G3 DSN 明确 blocked，不连接默认数据库、不用离线 skip 宣称生产门禁通过。
- [ ] 47.5 将 ATG-06 关键变异纳入可重复反例测试，记录可信基线与候选测试分离策略；同步验收手册。验收：去 fence、预算归零、fixture 清理后直跳、无限重试、自签 gate、绕过 activation 检查等变异均被指定用例捕获。

## 48. 晋级、版本运行与操作入口

- [ ] 48.1 实现 PromotionService，将 gate/review/发布授权/有效期/候选身份/worker ready 与 RevisionService 激活 CAS 纳入同一事务，显式传入冻结 target_plan/contract/auth 并核验运行权限，封住 GraphService/CLI/worker 旁路。验收：ALU-19、ALI-07，并发两个候选只一个后继，授权撤销或摘要变更拒绝；R3 新合同用于后继，旧 run 保持旧引用，缺授权不回退继承。
- [ ] 48.2 实现不可变 code bundle、worker 能力注册和版本匹配领取；无 ready worker 保留 verified_waiting_worker，不提前关闭旧 gate。验收：ALU-19、ALI-08；文档型 R0 不创建空 run，可执行变化必须新 graph_ref。
- [ ] 48.3 实现旧 run drain、新 run/thread、共享资源域解除后的新测量与回退协议；新版本失败停止新派发并保留旧版本对账能力。验收：ALI-08、ORC-087，旧 operation 不被新代码重发或旧 checkpoint 覆盖。
- [ ] 48.4 提供 lifecycle status/request-repair/request-change/decision/cancel 与 gates 查询命令，写入统一授权审计，README 给出真实可执行示例。验收：命令合同测试覆盖重复 request、陈旧 revision、缺权限、具体 blocked 原因及恢复入口。
- [ ] 48.5 实现默认关闭→注册动作→限定 scope→候选演进的迁移开关，兼容旧 records/worker，关闭开关仍允许清理。验收：迁移/回滚集成测试；同步 docs/orchestration/progress.md、setup.md 和 README，分别列 implemented 与 verified 范围。

## 49. 组合验收与发布证据

- [ ] 49.1 运行 G0/G1/G2 全部必需 ALU/ALI 离线组合及受影响旧回归，生成实际 nodeids、版本、日志和 GateManifest；核对设计→任务→测试映射。验收：零丢失必需用例，无新增隐藏 skip，正常路径无额外模型/修复派发。
- [ ] 49.2 使用专用真实 PostgreSQL 完成 ALI-03/04/07 的双 worker、预算竞争、CAS 和逐切点硬杀恢复；保留 SQL 事实与外部效果账本。验收：无丢任务/重复副作用/预算重置，未知效果不盲重发；缺环境保持未完成/blocked。
- [ ] 49.3 在独立隔离环境完成 ALI-05/06 与 G4，并在验收环境执行 ALI-08/G6 激活前 smoke、激活后观察及回退。验收：真实候选失败后修订成功、真实隔离有效、bundle 身份一致；模拟结果不登记 backend_verified。
- [ ] 49.4 对实际选择的应用内模型/编码 provider 做集成验收；若选择宿主 bridge，另做 ALI-09 幂等/查询/取消/usage/context 验收。验收：逐 provider 声明 verified/blocked/not_selected，不以 mock 证明已调用宿主 dispatch agent。
- [ ] 49.5 汇总实施范围、剩余阻塞、门禁证明及可启用动作/profile，复核 OpenSpec strict 与文档链接；仅据实际证据勾选本清单。验收：设计存在、代码实现、离线验证、后端验证和真实适配验证明确分开，无未经证据的“全节点自动自愈已完成”表述。

## 50. 资源观测合同强制与缺失显式化（issue #8）

规格 [RT-48](specs/runtime-evaluation/spec.md)、[RT-49](specs/runtime-evaluation/spec.md)，详设 [benchmark-design.md](benchmark-design.md) BCD-19（接 BCD-09）、[scenario-spec.md](scenario-spec.md) §15，测试合同 [benchmark-contract-design.md](benchmark-contract-design.md) BCT-15。

- [x] 50.1 `sandbox_bench/contracts.py`：明确 `ResourceContract` 字段；为需测资源方法（B09/B10 之外，至少 B01 或引入“显式放弃资源观测”声明位）增加强制规则/缺口入口。验收：BCT-15-REQUIRED。
- [x] 50.2 `sandbox_bench/session.py`：未声明 `resources` 时把跳过写入资源证据缺口 `resource_gaps`（`resource_observation_not_requested`，**不阻断 cleanup/seal**）；声明而未装配采样器保持 `blocked/resource_sampler_required`。验收：BCT-15-REQUIRED/BLOCKED。
- [x] 50.3 `sandbox_bench/wiring.py`：装配期校验 inventory（`node/boot_id/processes/evidence_ref/scope` 一致）与 collector 绑定，不符即拒绝。验收：BCT-15-INVENTORY。
- [x] 50.4 `sandbox_bench/analysis.py`：缺 `resource_series` 的 `unavailable` reason 带缺口引用（`resource_gaps`：未声明/未装配/样本不足）；数值保持 `None`，不补零、不推默认。验收：BCT-15-NOZERO。
- [x] 50.5 短寿命场景：对 B01 创建型显式声明资源窗口覆盖率或改用长寿命场景，不足记 `insufficient_samples`；同步 [scenario-spec.md](scenario-spec.md)（§15 长寿命场景：基准 `B09.IDLE`，备选 `B07.STEADY`/`E03.MIXED_SOAK`/`B10.*`）。验收：BCT-15-WINDOW。
- [x] 50.6 独立单元测试：`tests/benchmark/unit/test_resource_contract.py`（BCT-15）。验收：全组通过；字面量 oracle，不调用被测函数生成预期。
- [ ] 50.7 真实环境：在真实部署提供 inventory + collector 绑定，确认能采集到非零样本或显式 `insufficient_samples`。验收：需真实环境（135.214.237.243）；缺环境保持 blocked。
- [x] 50.8 口径分列（RT-49）：净底噪（去 Guest OS，`net_pss = raw_pss − guest_component_pss`）与每沙箱平均开支（含 Guest OS）分列、不混加；业界方法为 Firecracker `MemoryMonitor` 跳过 guest 内存区。同步 [scenario-spec.md](scenario-spec.md) §10/§11 与 [benchmark-design.md](benchmark-design.md) BCD-09。验收：BCT-15-CALIBER-SPLIT。
