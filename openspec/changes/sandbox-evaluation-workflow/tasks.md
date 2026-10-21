# 图编排与沙箱测试代码实施任务

本清单由 OpenSpec propose 在 2026-10-21 生成，现按用户授权进入 apply；勾选表示整条验收已有证据，未勾选可能已有部分代码，具体缺口见 [实施记录](../../../docs/orchestration/progress.md)。设计依据：[主设计](design.md)、[编排代码设计](orchestration-design.md)、[编排测试设计](orchestration-test-design.md) 及三份 delta spec。Q3 已确认：旧任务原图收尾，新图创建关联的新运行，复用经验收证据，不热切换活动拓扑。

当前实施范围包括通用图编排、脚本调用协议、状态／证据管理，以及 B01–B10 测试执行代码、三产品适配、资源采集、指标计算及对应契约测试；E01–E06 保留低优先级执行骨架。设计依据增加 [测试代码设计](benchmark-design.md) 和 [测试契约设计](benchmark-contract-design.md)。采用 D01 的 Python 异步／LangGraph／PostgreSQL 工程基线；版本记录在 pyproject.toml/requirements.lock。以下是目标交付和验收要求，不能仅凭文件存在或受控测试通过，推定真实环境验收完成。

`orchestration-design.md` 以下简称 D，`orchestration-test-design.md` 的用例简称 ORC。每项实现连同对应测试、合同文档一起交付；每个测试结果记录版本、fixture、断言、日志与失败证据。统一测试目录为 `tests/orchestration/`，文件名按测试设计 T01 放入 unit/graphs/persistence/recovery/e2e 对应层；跨层用例允许分文件实现，常规验收入口拟为 `python3 -m pytest tests/orchestration/<层>/<文件>`；数据库、故障和端到端测试使用独立标记及临时命名空间。未执行、环境缺失和 skipped 不登记为通过。

依赖顺序：1 → 2/3 → 4/5/6 → 7 → 8/9 → 10/11 → 12 → 13；14 为跨组件验收。测试执行新增组15–26：15→16/17→18→19/20/21，22→23，24→25→26；25依赖图接口与各执行模块，真实26.2/26.3依赖环境合同。组间共享模型或数据库迁移须先稳定接口，不能据此默认并行写同一文件。实施前 `sandbox_bench/` 是暂停的脚本草稿，基线中 `tests/test_runner.py` 引用的 runner 缺失；本轮已补入口并保留原测试回归，不隐藏旧失败。

## 1. 工程边界、模型和独立测试基座

- [ ] 1.1 建立 `sandbox_graph/` 包、项目依赖与锁文件，锁定 Python、LangGraph、saver 和 PostgreSQL 支持范围；更新 `docs/orchestration/setup.md` 的启动与测试要求。验收：隔离环境安装成功，记录实际版本，真实图／数据库最小连通检查通过，缺后端明确失败而非退回内存。
- [ ] 1.2 在 `domain/models.py` 定义 D04 的运行、图、计划、工作、attempt、operation、证据及决定模型，严格字段／单位／时钟域校验与规范化 hash；同步 `docs/orchestration/contracts.md`。验收：`test_models.py` 覆盖身份稳定、未知字段、秘密引用、重复实验与重试分离，ORC-018、019、062。
- [ ] 1.3 在 `domain/transitions.py` 实现 D06 分离状态与错误分类、scope_disposition、有限重试及 CAS 输入合同。验收：`test_transitions.py` 覆盖 ORC-016–019、050、051，性能不达标不会触发“重试到达标”，等待不冒充终态；状态表同步合同文档。
- [ ] 1.4 在 `tests/orchestration/conftest.py` 建立 T02 的三组环境、共享宿主、独立 oracle、FakeClock 和 SpyModel；设计受控 runner 夹具的持久接口。验收：夹具自检可制造 accepted≠complete、跨时钟、缺测和共享域反例，预期值不调用生产计算函数；记录 fixture hash 与合成数据标签。

## 2. 节点目录与选择计划编译

- [ ] 2.1 在 `catalog/registry.py` 注册 D07 的全部 45 个工作节点、方法绑定及 S/M/H 职责，路由菱形不建立额外 agent。验收：`test_registry.py` 参数化核对 ORC-002、027、070，B01–B10/E01–E06 均有绑定或明确缺适配状态，目录与节点文档一致。
- [x] 2.2 在 `planning/compiler.py` 实现优先级／环境组／沙箱类型／用例筛选，同轴并集、跨轴交集、排除及授权边界。验收：`test_selection.py` 覆盖 ORC-001–005、010，空集合／未知值不回退全测，预览与执行使用同一 plan hash；文档给出有限选择例子。
- [ ] 2.3 实现依赖补齐、有效证据预检、能力与适配缺口、有限条件矩阵及 stage-only 计划。验收：`test_planning.py` 覆盖 ORC-006–009、015、052，准备动作不扩成额外性能场景，缺真实合同只阻塞对应工作；说明依赖原因和覆盖分母。
- [ ] 2.4 实现 GraphDefinition 校验与 get/list/propose_patch/validate/diff，区分依赖 DAG 与有界控制循环，禁止未知 handler 和删除必要清理路径。验收：`test_graph_definitions.py` 覆盖 ORC-002、008、009、055，历史定义不可变、未验证适配不能激活；同步图定义格式文档。

## 3. 业务持久化与产物提交

- [ ] 3.1 在 `persistence/repository.py` 及数据库迁移中建立 D15 的账本、版本、scope、attempt、operation、资源、交付、激活谱系等约束及 UnitOfWork。验收：真实 PostgreSQL 的 `test_repository.py` 覆盖 ORC-019、033、039，重复业务键拒绝／幂等、旧 revision 写入失败；文档列出迁移及恢复方法。
- [ ] 3.2 实现 visit_id 的持久分配、receipt 唯一键及“结果＋下一步 intent”事务，恢复时对账 checkpoint/receipt 缺口。验收：`test_receipts.py` 注入 ORC-029 的两侧崩溃切点，同一次 replay 不重复副作用，新循环 visit 可以执行；记录事务边界。
- [ ] 3.3 在 `artifacts/store.py` 实现分段写入、摘要、不可变 manifest 发布、作用域读取和可达性验证。验收：`test_artifacts.py` 覆盖 ORC-040、058，文件成功数据库失败产生 orphan，坏摘要／缺文件不能变有效交付；文档区分实验清理与证据 GC。
- [x] 3.4 实现事件、审阅和分析输入的不可变版本查询及精确引用，不允许消费 latest 替代冻结版本。验收：`test_versioned_inputs.py` 覆盖 ORC-046、048、049，交付 v2 到达不会污染正在消费 v1 的计算；同步查询与保留合同。

## 4. 所有权、冲突域与 checkpoint fencing

- [ ] 4.1 在 `runtime/leases.py` 实现 thread driver 租约与跨组／跨版本资源域原子预约、续租、接管及隔离；锁顺序固定。验收：真实双连接 `test_leases.py` 覆盖 ORC-033、035、036，无部分锁、双 owner 或过期即释放真实资源；记录共享宿主冲突策略。
- [ ] 4.2 在 `persistence/checkpointer.py` 实现 D10 的 FencedPostgresSaver，所有 checkpoint 和 pending writes 写事务与 claim/takeover 锁同一 lease 行，禁止独立连接先查后写。验收：`test_fenced_saver.py` 强制 ORC-034 两种事务顺序，覆盖同步／异步写入、interrupt、子图，旧 epoch 无法提交新 owner 后的写入；文档标明锁定 saver API。
- [ ] 4.3 实现失去 owner 后停止新派发、对账在途操作、保留实际资源状态和未知域隔离。验收：`test_owner_takeover.py` 覆盖 ORC-034、035、041，接管不改 operation_id、不绕过远端未知效果；记录恢复步骤。

## 5. 持久任务、事件及等待

- [ ] 5.1 在 `runtime/tasks.py` 实现 enqueue/claim/finish、activation 去重及有界失败处理；在 `runtime/events.py` 实现 outbox/inbox/cursor、来源与 seq 校验。验收：`test_tasks_events.py` 覆盖 ORC-033、037、039，重复／乱序／通知丢失仍可重扫恢复，无“计数加一”式重复完成；同步事件合同。
- [ ] 5.2 实现 D09 的机器等待登记、cursor 与状态 recheck 协议，END 只结束本次 activation，事件建立后续 activation。验收：`test_machine_waits.py` 用 barrier 覆盖 ORC-038，等待登记前后事件均不丢唤醒，无忙轮询；更新唤醒时序文档。
- [ ] 5.3 在 `runtime/waits.py` 实现独立人工 interrupt 节点、DecisionEnvelope 版本／问题匹配与重复答复幂等。验收：`test_human_waits.py` 覆盖 ORC-043、044，恢复前不重做副作用，过期／错问题／旧计划答复不能批准新范围；记录人工必要条件。
- [ ] 5.4 实现等待 TTL/watchdog、保留期限与已授权的撤压／清理或隔离调度。验收：`test_wait_watchdog.py` 覆盖 ORC-035、043、045，owner 崩溃后仍有限处置，超时绝不等于批准；同步资源等待合同。

## 6. 确定性脚本调用协议与受控执行器

- [x] 6.1 在 `runtime/runner.py` 定义注册脚本、结构化输入、秘密引用、submit/reconcile/cancel/collect 合同及 D11 操作状态机；实现 intent/submission_started 的持久化顺序。验收：`test_runner_protocol.py` 覆盖 ORC-030–032，unknown 不自动重新提交，不接受任意 shell；文档列出执行器须具备的幂等／查询能力。
- [ ] 6.2 实现仅用于编排验收的持久 ControlledRunner fixture，独立进程保存副作用账本，支持断连、响应丢失、迟到、取消失败和恢复对账。验收：`test_controlled_runner.py` 覆盖 ORC-030–032、041，controller 硬杀后真实 fixture 副作用次数可独立核对；不接产品远端。
- [ ] 6.3 分离观测 handle 与发压 operation，落实停止／收集、序列缺口、跨重启区间和 deadline/watchdog。验收：`test_observation_protocol.py` 覆盖 ORC-025、042、062，采样重启不得拼成连续稳态，缺测不是零；更新采样合同。
- [ ] 6.4 实现所有权约束的 cancel/reconcile/cleanup 流程及资源残留交付。验收：`test_cleanup.py` 覆盖 ORC-024、028、035、045，取消确认和清理确认分别记录，不清理其他组资源；异常与残留有可恢复入口。
- [ ] 6.5 将固定请求预算、并发、滑窗／分批与吞吐事件合同透传到执行器并校验返回结构，禁止编排层自行填测量数值。验收：`test_measurement_contract.py` 覆盖 ORC-026、059–064 的独立合成数据，失败消耗额度、accepted 不算完成、末桶时长和真实存活分母保持不变；公式实现与真实发压精度仍见第 13 组边界。

## 7. LangGraph 构建与分层调度

- [ ] 7.1 在 `graphs/factory.py` 建立 Run/Group/Work/Analysis/Report 的真实 StateGraph 和小型引用 State，图编译只使用已注册 handler。验收：`test_graph_factory.py` 覆盖 ORC-002、027、070，45 节点映射、状态 schema 及合法出边完整，无 fixed edge 与 Command 重复派发；同步 Mermaid 映射。
- [ ] 7.2 在 `runtime/driver.py` 实现 start/resume、租约绑定、receipt 恢复与 activation 版本冻结。验收：`test_driver.py` 覆盖 ORC-029、034、055、056h，真实图／持久 saver 恢复保持原输入身份，不让新图解释旧 checkpoint；记录 driver 启停约束。
- [ ] 7.3 实现 R0/R1/RP/R2/R3/R6/R7，R2 持久派发独立组任务、R3 增量消费、R6 局部返工。验收：`test_run_graph.py` 覆盖 ORC-047、052、053，一组先交付即可分析，不等待所有组子图处在同一 superstep；控制状态没有全量样本。
- [ ] 7.4 实现 G0/T0/T1/T2/T4/T5/T6/T8/TC/TB/GB/GW/GE，以独立工作任务调度、能力缺口和二次审阅闭合组状态。验收：`test_group_graph.py` 覆盖 ORC-015、036、046、051、070，组角色不按机器拆成无限 agent，原始执行事实不被复核覆盖；更新组事件合同。

## 8. 环境与测试工作节点

- [x] 8.1 实现 E1/E2/C1/C2/C3 的脚本调度 handler，覆盖连通、密钥配置、在位核验、已授权安装配置、复检报告的边界，所有实际命令通过注册适配。验收：`test_environment_graph.py` 覆盖 ORC-011–015，未知安装状态不自动重装，已有证据和真实安装历史区分，缺真实适配明确 blocked；同步节点职责表。
- [ ] 8.2 实现 N00–N03 的输入守卫、源产物／规格核验、外部操作对账与准备状态。验收：`test_work_preflight.py` 覆盖 ORC-015、018、024、026、027，状态就绪与命令可执行分开，缺合同／未知副作用阻止依赖操作；更新内部节点图。
- [ ] 8.3 实现 N04–N07 的基线观测、发压调用、稳态等待、有限停止与证据封存顺序。验收：`test_work_measurement.py` 覆盖 ORC-024–027、042、059–064，baseline 在操作前、稳态在停止前，异常仍进入收集及处置；不把合成结果登记成沙箱指标。
- [ ] 8.4 实现 N08–N10 的自检、清理和交付，包括校验失败仍执行清理及硬崩溃后的恢复调度。验收：`test_work_delivery.py` 覆盖 ORC-024、028、041、046，每条失败出口具有证据与资源处置；节点 execution、data_quality、performance、cleanup 分列。

## 9. Agent 上下文与模型／人工边界

- [ ] 9.1 在 `runtime/agents.py` 实现 root/group/work 的 owner 身份、NodeContext allowlist、证据范围读取与存储权限校验，不共享 messages。验收：`test_agent_context.py` 覆盖 ORC-020、021、069，跨组越权拒绝，确定性路径模型调用为零，万条日志只传引用和有界片段；更新上下文清单。
- [ ] 9.2 实现 DiagnosisProposal 与语义复核输出结构，区分事实／假设／注册建议，模型不得覆盖测量值、扩大范围或运行未注册脚本。验收：`test_agent_proposals.py` 覆盖 ORC-022、023、046、057，伪造引用和越权提案被拒，无法判断保持显式缺口；记录审阅来源。
- [ ] 9.3 落实 token、轮次、诊断时间预算与人工问题精确作用域；进入诊断前调用资源处置协议。验收：`test_agent_budgets.py` 覆盖 ORC-023、043–045、069，无无限模型轮询，未提供 usage 标记缺失而非编造 token 数；同步预算配置说明。

## 10. 分层验收、分析及报告编排

- [x] 10.1 在 `services/reviews.py` 实现 WorkDelivery→T6→GroupDelivery、manifest 重读、审阅追加及失败缺口分类。验收：`test_reviews.py` 覆盖 ORC-046、051、053，仅“成功”摘要不能通过，组复核不覆盖样本；更新审阅规则合同。
- [ ] 10.2 实现 A1/A2/A3/A4/A5/A7 的版本冻结、规则校验、已注册统计／比较／Excel 服务调用与返工路由。验收：`test_analysis_graph.py` 覆盖 ORC-048、052–054、057、060–064，fixture 版本固定、不可比项保留、缺公式或单位明确阻塞，实际公式与工作簿服务不能被 stub 验收冒充；同步输入输出 schema。
- [ ] 10.3 实现 F1/F2/F4 的事实装配接口、模型解释约束、引用及覆盖校验、归档 manifest；在 `services/closure.py` 实现 closure_revision CAS 和 partial archive。验收：`test_report_closure.py` 覆盖 ORC-049–052、057、058，未处理 running/waiting 不收口，部分归档有缺口，Excel／报告引用同版分析；更新归档合同。
- [ ] 10.4 实现增量交付 dirty 标记及分析／报告专用重跑路径，复用已验收输入而不触发真实测量。验收：`test_incremental_analysis.py` 覆盖 ORC-047–049、052、053、068，迟到组版本生成新分析，旧版保留，rerender 的 runner submit=0；记录版本查询方法。

## 11. 图版本激活、收尾与证据复用

- [ ] 11.1 在 `planning/changes.py` 实现 ImpactPlan、兼容规则与 reuse_from/reuse_candidate，区分阈值重算、规格重测、完成事件缺失。验收：`test_change_impact.py` 覆盖 ORC-054、055、056f，复用不改变原 work 身份或重复计数；同步兼容规则清单。
- [ ] 11.2 实现 D14.1 激活事务、lineage/supersedes、新 run/thread、request_id 幂等和 predecessor CAS。验收：真实数据库 `test_revision_activation.py` 覆盖 ORC-056a/c/d，两并发候选只有一个成功，事务前后硬杀可恢复且无半激活；记录接口及失败码。
- [ ] 11.3 将 dispatch_generation/gate 与副作用提交许可接入 tasks/runner，区分未开始 superseded、已有许可在途、drain 和 cleanup 许可。验收：`test_dispatch_cutoff.py` 用 barrier 覆盖 ORC-056b/e，两种线性顺序副作用归属均正确，已关闭普通派发仍可做必要清理；同步竞态时序图。
- [ ] 11.4 实现旧图有限收尾、旧等待撤销／到期、跨版本资源门控、旧交付订阅后的独立验收及复用候选期限。验收：`test_revision_drain.py` 覆盖 ORC-056e/f，无共享域抢跑、旧问题批准新范围、无限复用等待或迟到结果覆盖快照；文档列出处置状态。
- [ ] 11.5 实现同图新计划 activation、回退创建新谱系和旧代码／定义保留检查。验收：`test_revision_rollback.py` 覆盖 ORC-056g/h、055，旧 activation 输入固定，回退不重放已发生操作，删除必要旧定义被阻止；更新运行手册。

## 12. 服务入口与可观察性

- [x] 12.1 在 `interfaces/service.py` 暴露 RunService、GraphService、QueryService，共用版本化输入与 request_id；提供可调用的最小命令入口及说明。验收：`test_services.py` 覆盖 ORC-001、010、044、052、056d，preview/start 使用相同 hash，status 可定位阻塞和证据，重复命令不重复派发。
- [ ] 12.2 实现 D16 的结构化日志、trace、阶段耗时、token usage 可用性及复用计数；秘密脱敏、原始日志独立存储。验收：`test_observability.py` 覆盖 ORC-021、023、069，控制状态和上下文有界，调度指标不混入沙箱吞吐；文档列出字段及缺测表示。
- [ ] 12.3 提供编排部署／恢复手册、临时数据库和受控 runner 启动配置，声明仅编排验证模式及真实模式适配检查。验收：从空测试库可运行最小五阶段 fixture 并查询谱系／证据，缺适配的真实测试请求明确拒绝；相关命令与实际输出存档。

## 13. 主变更覆盖账本与其余环境／报告边界

本组维护本轮编排里程碑与主 change 全量需求的边界，不能把设计完成等同业务脚本完成。测试执行代码及契约现已在15–26组展开；实际Excel／事实报告已接图，具体环境安装与真实数据验收仍须相应合同和实施证据；在全部对应 spec 获得实现和验收证据前，本 change 不满足归档条件。

- [x] 13.1 建立 `docs/orchestration/requirement-coverage.md`：逐条 WF/ENV/RT 映射 handler、测试层和实际业务实现状态，列出真实探测／安装／配置、产品适配、测量、PSS/Guest 归属、统计／Excel／报告渲染、L5 的未交付项及后续交付引用。验收：无仅凭 ORC 通过而将 ENV/RT 标记完成的条目；未实现项全部保留 planned/blocked 原因。
- [ ] 13.2 建立后续脚本合同清单，逐节点记录输入输出、公式／完成事件、资源身份、幂等／取消／清理、未决 Q1/Q2/Q4–Q8/Q10–Q12 和验收样例要求；已审计结论直接引用，不安排泛化“重做源码审计”。验收：缺项能够精准阻塞相关真实操作，合成 fixture 值不自动成为真实默认值；将 `align-sandbox-benchmarks` 的旧任务标为由本清单替代的规划入口，避免双重执行。
- [ ] 13.3 增加里程碑完成检查和归档覆盖核对，分别输出编排 L0–L4 状态、真实适配 L5 状态及全量 spec 缺口。验收：即使本清单所有编排测试通过，缺真实脚本证据时仍报告主变更未完成，不自动同步为已交付能力或归档；在手册中说明后续任务扩展入口。

## 14. 跨组件故障、端到端与最终回归

本组只验证前述已实现组件的组合行为；各模块单元／合同测试已放在对应实施组。

- [ ] 14.1 用真实 LangGraph、正式 PostgreSQL 后端及持久 ControlledRunner，构建双 worker 硬杀、连接中断、重复／乱序事件、artifact 提交失败及图版本切换组合故障测试。验收：`test_recovery_integration.py` 联合验证 ORC-029–045、056，独立副作用账本与最终所有权一致，未知资源无越权释放。
- [ ] 14.2 实现五阶段双环境组端到端、部分失败、人工等待、增量分析、only-analysis/report 场景。验收：`test_end_to_end.py` 覆盖 ORC-065–068，所有已选项都有显式处置、Excel／报告接口的产物同版本可追溯，真实产品测试状态仍单列。
- [ ] 14.3 执行 T11 的有限种子属性与关键变异验收，并汇总全部 ORC-001–070（含 ORC-056a–h）映射与结果。验收：accepted 当完成、删除 fencing、缺测补零、跳过清理、latest 混读、超时批准等变异均被相关断言捕获；45 节点覆盖与受限上下文检查通过，seed 和失败记录可复现。
- [x] 14.4 执行限定范围测试集、OpenSpec 严格校验及文档／图／任务一致性检查，整理实施版本、运行命令、结果、未测／阻塞清单和后续业务脚本缺口。验收：可复查每项完成证据；既有草稿测试问题单列，未跑全库不声称全库通过，未通过 L5 不声称任何产品性能已验证。

## 15. 测试器合同、持久会话及独立 oracle

- [ ] 15.1 实现 `sandbox_bench/contracts.py` 的方法条件、事件、资源、MetricValue schema 和 schema_version，承接图输入；同步 contracts 文档。验收：BTC-001/002/015 缺项、单位、bool/NaN、未知字段负例通过，真实参数不继承 fixture。
- [ ] 15.2 实现 `registry.py` 的 B01–B10 子方法及 E01–E06 目录、三候选能力和适配状态、源码／部署版本绑定。验收：BTC-014/056 方法全量参数化覆盖，无方法时明确 blocked/unsupported；节点职责索引一致。
- [ ] 15.3 实现 `journal.py` 与 `runner.py` 的阶段 receipt、稳定子 operation、预算 slot、resume/collect/cancel 接口，桥接 D11/D14。验收：BTC-019/049/050 的进程切点无重复提交／预算重置；文档区分正式仓储与离线文件夹具。
- [ ] 15.4 建立 `tests/fixtures/benchmark/`、ManualClock、独立 ScriptedSandboxService 和 BCT-02 黄金预期文件，记录 provenance/hash；实现契约元数据校验。验收：预期不调用生产算法，fixture 可注入迟到／缺测／部分成功，BTC-016/017/034/041/042/045/047 可逐值核对。

## 16. 传输、完成观察和日志解析

- [ ] 16.1 改造 `transport.py` 为有界流式读取，保留 headers/body/首数据时刻、错误类别、原始bytes与秘密隔离；同步协议说明。验收：真实本地HTTP服务运行 BTC-003 的慢滴／断流／redirect／超限，无自动重发副作用。
- [ ] 16.2 实现 envd Connect 增量codec与结束语义，区分 JSON/protobuf 适配能力和 native exec 方法。验收：BTC-004/028 及既有协议回归覆盖分片、非零exit、缺end、流错误和完整hash；来源版本可追溯。
- [x] 16.3 实现 `completion.py` 四种终点及独立后验，替换旧强制exec路径；记录process_epoch、poll/deadline、迟到及unknown。验收：BTC-005–007 精确事件序列与槽位断言通过，状态成功不会被命令后验失败抹掉；更新完成合同说明。
- [x] 16.4 实现 `logs.py` Kuasar terminal parser、关联键、去重和起点证据检查。验收：BTC-008 的错kind/sid/run/profile、失败和缺起点不生成有效内部耗时，事件与观察时间分列；其他产品缺内部证据不造字段。

## 17. 三套产品生命周期与镜像适配

- [ ] 17.1 将 adapter 草稿迁移为协议与三个产品实现，统一 inspect/list/effective_spec/placement/execute/reconcile/cleanup 接口；处理同名模块迁移。验收：BTC-014/015 与已有 adapter 测试可区分兼容和已废弃假设，文档明确接口版本。
- [ ] 17.2 实现 Cube 的模板build、创建、暂停／恢复、fork、snapshot、删除和资源／节点映射，所有权清单保留成功child。验收：BTC-009/010/025/027/030 的受控协议测试通过，单位、分页限制、remoteStatus和部分失败均不掩盖；源码证据入文档。
- [ ] 17.3 实现 Kuasar builder注册／触发／status持久ID、create状态观察、pause/connect/export及实际规格证据入口。验收：BTC-011/012 通过，保留keepSource与memory模式，不能以detail全局默认假证规格；缺runtime证据明确受限。
- [ ] 17.4 实现 AgentENV 模板／cold、暂停恢复、fork、snapshot、分页、TTL和节点归属，按部署codec预检。验收：BTC-013/024/025/030 通过，能力未验证不静默选JSON，实际规格和配置额度区别保存；更新适配合同。

## 18. 发压算法与有限预算

- [x] 18.1 实现 `load.py` WINDOW，按完成事件即时补槽、持久预算和实际并发轨迹；同步算法时序文档。验收：BTC-016/019/020 黄金序列精确，accepted不释放槽，unknown外部在途上限生效。
- [x] 18.2 实现 BATCH 的批内限流、全部终结后gap、余数批和结束逻辑。验收：BTC-017 的B=C/B>C/失败场景通过，总wall=10黄金例及尾批无gap；记录batch参数和实际轨迹。
- [x] 18.3 实现有限开环scheduled计划、queue/drop策略和发生器延迟统计。验收：BTC-018 达不到目标λ明确记录，队列有上界、未发起与失败分开；文档列出可比条件。

## 19. 创建、暂停恢复、删除和命令用例

- [ ] 19.1 实现 `fixtures.py` 和受控 `guest_probe.py`：来源/spec/cache核验、实际触页、内存nonce/文件hash和FIRST资格。验收：BTC-015/021/024/028/030，不能从磁盘重建nonce伪内存连续，不额外跑准备场景性能矩阵；记录复位规则。
- [ ] 19.2 实现 B01 TEMPLATE/SNAPSHOT、B02 FULL/PREPARED 及共享build方法阶段机。验收：BTC-021/022 通过，三候选必要构建成本不遗漏，恢复只接管原build，独立job与共享成本分组；更新方法目录。
- [ ] 19.3 实现 B03 PAUSE 与 B04 MEMORY/DISK 的非唤醒观察、状态断言及首次工作接口。验收：BTC-023/024 通过，源状态与进程内容证据完整，API／状态／首次工作时间分开；复入不新造近零样本。
- [ ] 19.4 实现 B06 RUNNING/PAUSED 删除多终点与按资源归属的释放核验。验收：BTC-027/051 通过，权限失败非已删、共享源非泄漏、创建成功事实保留；记录残留处置。
- [ ] 19.5 实现 B07 FIRST/STEADY/OUTPUT/ERROR、命令任务身份、完整输出与退出断言、超时对账。验收：BTC-028/029 通过，完整bytes/hash/exit匹配，远端未知命令不重发；同步用户／连接／协议条件。

## 20. 克隆和快照用例

- [ ] 20.1 实现 B05 FULL/PREPARED/NATIVE，持久父／child操作、部分结果、源恢复和引用计数清理。验收：BTC-025/026/051 通过，源与兄弟隔离断言、父/child吞吐和FULL源恢复成本正确；文档说明组合实现。
- [ ] 20.2 实现 B08 LOCAL/PUBLISHED，工作集触页、捕获与发布、独立恢复后验及源副作用检查。验收：BTC-030 通过，无独立内部事件不拆分假耗时，本地ready不当远端ready；产物大小口径与清理关系记录。

## 21. 底噪与全部密度方法

- [ ] 21.1 实现 B09 PLATFORM/IDLE/PAUSED 的基线、cohort和窗口链，接资源服务。验收：BTC-031/044 通过，采集前置顺序正确，Guest净值缺证明时不可用而原始PSS保留；说明暂停卸载零值依据。
- [ ] 21.2 实现 B10 GROWTH_LIST/GROWTH_READY 的独立停止状态机、存量身份差集、预算下界与原规则兼容偏差记录。验收：BTC-032/033/057 通过，净增长不替换新增健康，旧实例损失和不完整列表可识别；不运行官方全局reset。
- [x] 21.3 实现 B10 PROJECTION 的两公式、整数MiB兼容取数、锁定awk参考舍入及统一bytes计算。验收：BTC-034/057 黄金数16/18/14、阈值和部分健康负例通过；每值带模型／窗口版本和非实测标签。
- [ ] 21.4 实现 B10 FIXED_REQUESTS.WINDOW/BATCH 与 SLO_VERIFY，接相同load engine和最终cohort。验收：BTC-035/036/056 三产品×方法矩阵通过，Q固定且失败消耗预算；无新建吞吐N/A、无阈值SLO blocked；条件和能力缺口完整。

## 22. PSS、CPU、实例归属与稳态采样

- [ ] 22.1 实现 observe inventory/procfs，按boot/PID/start及sandbox generation关联角色组件，解析smaps_rollup/stat及必要辅助资源。验收：BTC-037–039 冻结proc树与受控Linux进程测试通过，读取竞态和权限失败不补零，文档明确支持平台。
- [ ] 22.2 实现cgroup计数与去重范围、sampler持久句柄／有限spool、采样器开销及缺样事件。验收：BTC-039/040/050 通过，进程/cgroup重叠不相加，数据库失联无新副作用，丢样保留；部署读取权限明确。
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

- [ ] 25.1 把真实BenchSession接入N00–N10及D11 RunnerGateway，N04采样跨N05/N06，N07停止，N09清理；补接口文档。验收：BTC-058与ORC-024–028/059–064联验，不用fixture摘要冒充实际公式。
- [ ] 25.2 实现迟到资源、取消与失败清理的恢复入口，校验同一operation／所有权与图版本提交许可。验收：BTC-049–051/058进程硬杀后无双提交、无清理他组资源，旧图收尾仍可执行；失败和残留可审查。

## 26. 跨产品合同、真实接入与最终验证

- [ ] 26.1 执行三适配器受控合同矩阵与BCT-08关键变异测试，汇总BTC-001–058逐用例状态及源码证据。验收：accepted释放槽、gather滑窗、错分母、缺测补零等变异被断言捕获；所有B/E方法均有代码／合同／任务映射，未跑不写pass。
- [ ] 26.2 在已提供的真实环境合同下执行三产品最小功能及数据面／规格／placement／采集预检。验收：BTC-059输出产品版本限定证据；缺连接／权限／版本则记录blocked并保留未勾选，不以受控HTTP替代。
- [ ] 26.3 按已选有限条件执行基础性能、密度双方法及固定Q／资源联合测量，独立抽查ID与原始事件并重算。验收：BTC-060的可追溯结果、缺口、干扰及真实分母齐全；未确认Q12时净底噪不输出假数，扩展未选不执行。
- [x] 26.4 联合编排与测试器执行回归、严格spec校验、文档链接和任务覆盖检查，交付实现／验证／真实实测分层清单。验收：任务勾选逐项有证据，未完成真实用例及环境脚本不被归档进已实现能力，既有草稿失败不隐藏。
