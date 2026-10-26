# 实施记录 — sandbox-evaluation-workflow

当前状态以 [生命周期能力账本](lifecycle-status.md) 和 [2026-10-24 根目录实施记录](../verification/agent-lifecycle-runtime/implementation-increment-20261024.md) 为准。代码已从工作树合入项目根；下列旧计数和“尚未实现”属于各次历史记录，不代表当前状态。

2026-10-24 后续：普通 worker 的 coding/review/可信观察器与候选清理已装配；新增 rollout（off/actions/scoped/candidate）、rollback 命令与持久等待 worker，关闭 rollout 不阻断安全预算内的清理。Kuasar 集群 group 配置及管理角色组证明已补齐本地路径。验证与剩余代码项见 [继续实施记录](../verification/agent-lifecycle-runtime/continuation-20261024.md)，不将它们统一归因于真实环境缺失。


## 2026-10-24 当前状态：端到端检查 R1–R3 已修复

最新修复位于 `.worktrees/agent-lifecycle`：drain 原期限内只查询、到期与显式取消分开；角色请求在上下文构建前及入队事务拒绝停止后的新任务；父 run/group 汇总子 provider 且不接管子权限。25项专项通过，相关回归阶段819项通过；最终离线全量2639 passed、20 skipped、110 subtests passed，见 [修复记录](../verification/agent-lifecycle-runtime/stop-fixes-20261024.md)。README、能力对照、proposal/spec/design/gates/tasks 已同步。任务仍160/209，45.4为部分完成；未提交、合并或推送。

## 2026-10-24 前一阶段：provider 取消测试定位已解决

本节保留前一阶段时点。先补现有 OpenSpec 的 ALD-11、ATG-08/09、WF-39 与 45.4-A–F，随后继续修正 provider 测试，无 agent 复审。

- T6 子交付已有 11 项专项和 278 项固定回归的历史通过证据；取消/版本替换的后续草稿尚未通过整体验收。
- 已解决上述失败：多生命周期夹具中首行属于其他 lifecycle；按原 request/lifecycle 读取后验证真实 waiting→closed 路径。新增父任务 running/cancelled 但子任务未结束两种反例；187项相关测试和278项测量/T6门禁通过，均无 skip。证据见[验证记录](../verification/agent-lifecycle-runtime/provider-stop-fix-20261024.md)。
- 返工原因：夹具接口/调度顺序准备不足，以及资源归属、迟到回执、安全预算与事务边界未在实现前具体化；不把这些全部解释成正常 TDD 红灯或环境阻塞。
- [详细代码设计](../../openspec/changes/sandbox-evaluation-workflow/agent-lifecycle-design.md) 固定职责/状态/归属/事务/权限/内部拓扑；[门禁代码设计](../../openspec/changes/sandbox-evaluation-workflow/agent-lifecycle-test-gates.md) 固定 ST-01–20、独立夹具、runner 接口及本地/持久/晋级证据边界。
- 任务仍为 160/209；45.4 保持未完成，43/46–49 的真实持久化、候选隔离、可信门禁和发布缺口仍按原清单记录。QEMU/KVM smoke 的成功不等于三种产品沙箱已部署或生产性能已验证。

用户在 2026-10-21 明确要求设计后直接 apply。规划为26组96任务，未经真实验收不勾选。设计独立审阅两项已修正：自然结束统计包含最后事件；原生fork按child原子预留预算。审阅复核无重要缺口。

- 当前工作区已有未提交规划和代码草稿，按用户“就在这里”及自主实施要求原地工作，保留全部历史文件，不自动commit/push。
- Python3.14/HarmonyOS；.venv安装锁定LangGraph、pytest、psycopg等成功，实际import通过。
- 基线：45 tests collected，tests/test_runner.py因sandbox_bench.runner不存在而collection error（原有草稿缺口），将按新合同实现该入口。
- PostgreSQL服务、Docker及真实沙箱环境目前未提供；正式持久集成与真实Linux/沙箱验证不以模拟代替。
- 分工按文件边界执行；编排、测量计算、负载／场景分别由独立代理实现与复核，主代理负责合同、适配、持久本地账本及跨组件集成。

## 本轮实现进展

- 已实现 LangGraph 五类图、持久 job/activation/receipt、提交许可与 fencing、等待／恢复、图版本新谱系切换、独立组交付及冻结分析输入。正式 PostgreSQL 仓储和 saver 已编码，尚无真实服务端验收证据。
- 已实现版本化 CaseContract、三产品协议适配、四种完成终点、WINDOW/BATCH/有限开环、生命周期与密度执行、持久 parent/child/stage 意图与结果、PSS/CPU/稳态/Guest 减项合同和实际指标公式。
- 已接 BenchSession N00–N10 与 D11 RunnerGateway；失败进入停止观测与清理。全失败且缺规格证据不再误报 valid；源恢复未知与采样未落盘保留缺口。
- `bindings.RegisteredBenchmarks` 将固定模板与已展开 work 绑定，T2 仅构造注册会话，拒绝未注册／变更的合同，不从模型输出导入代码。
- 已接 A3/A4/A7/F1/F4：冻结交付物化、原始证据复算、横向可比性判断、实际 Excel、事实报告及同分析版本归档。未知在途、未处置缺口与新证据使归档阻塞；明确失败或隔离只能进入部分归档。
- 本地硬退出测试覆盖提交意图、外部效果、child receipt 三个切点。它验证本地文件执行账本，不替代 PostgreSQL 多 worker 硬杀验收。
- 独立审查已驱动修复：Driver lease 释放与精确 interrupt resume、T6/GE 冻结版本交付、closure CAS、pending sampler spool、迟到资源和清理后旧 seal 复用、clone 源恢复、原始输出校验和未知副作用预算。

## 当前验证边界

最终全库原始输出、逐测试JUnit、代码摘要、严格spec及链接检查统一保存在 [验证记录](../verification/README.md)。并行 TDD 中间结果不作为最终验收结论。完整任务的勾选与代码已部分实现分别登记。

本轮后续已接：B08有预算独立恢复和unknown资源对账；单宿主来源引用与N09/N10；基于实际测量合同的accepted复用及原样本去重；有限已注册重算派发。独立审查复现并修正B08后验丢失、恢复时间污染性能窗口、借入源引用释放、归档处置跨run、复用review重放冲突等问题，均加入回归。

仍需明确保留：真实 PostgreSQL 并发／断连验收，真实 Linux procfs 权限与节点部署，三产品线上最小功能／性能验证。共享build、快照产物大小和完整源副作用、snapshot/image删除和物理释放、远端命令未知对账、补样／重测执行器等仍有代码缺口，不能归咎于仅缺环境。完整清单见统一验证记录、requirement-coverage和实施报告；tasks只按整条验收勾选。


## 环境适配 change（2026-10-21）

另一个在途 change [`implement-sandbox-environment-adapters`](../../openspec/changes/implement-sandbox-environment-adapters/tasks.md) 目前为 **22/65 tasks**。已落地的是环境检查/配置链路依赖的通用底层模块，不是完整环境检查或环境配置产品能力。三产品 adapters、产品级安装/调参/修复、环境 LangGraph/CLI 接线和真实部署验收仍未完成；详细边界见 [环境适配说明](../environment-adapters.md)。

## 2026-10-22 生命周期增量首批（实施中）

隔离工作树位于 .worktrees/agent-lifecycle，分支 feat/agent-lifecycle，起点 2dfe8ae。保留主工作区原有设计和第41组实现。已将 HANDOFF 的 C1–C4/G1–G8 与真实租约问题 L1 映射到 ALD-10/ATG-07；本批不以 OpenSpec planning complete 当代码完成。

- 已编码：严格生命周期模型和纯事件转换（42.1）；真实 remedy 线程租约、续租、失效取消、job/thread 双 fence 和释放（43.6）；质量拒绝监督事件、节点 supervisor 身份、图内收尾路由和修复后验拒绝。
- TDD：租约初始 4 失败/3 通过，证据后验反例 1 失败/8 通过，失去 job claim 的 receipt 发布反例 1 失败/8 通过；针对性修复后租约与复审 14 通过。模型/状态机由独立字面状态表验证；独立审阅发现的恢复入口覆盖和停止事件操作身份两项均已补序列回归修复。最终编排回归 1023 passed、19 skipped（15.87s）；benchmark/integration 排除 capacity_wake 两例后 27 passed（70.09s），另行运行这两例为 2 passed（59.33s）。纯合同/状态机 756 passed 为前述编排套件子集，不重复加总。
- 仍未实现：持久生命周期服务及全量状态推进、request_remediation 原子授权/预算、ResourceDispositionReceipt 接线、可信 RecoveryPlan、provider/agent team、隔离编码/门禁执行和版本晋级。现有旧 order/预算/入口仍有交接列明的缺口，不能据本批改动启用全自动生产修复。
- 真实 PostgreSQL/隔离后端/宿主 provider 未在本批验收；环境相关跳过不登记通过。任务只在整条验收满足后勾选。

验证命令：

~~~bash
../../.venv/bin/python -m pytest tests/orchestration -q
../../.venv/bin/python -m pytest tests/benchmark/integration -q -m 'not postgres and not live and not linux' --ignore=tests/benchmark/integration/test_capacity_wake.py --durations=5
~~~

以上命令在隔离工作树根目录运行，共用主仓库已安装的虚拟环境。全库离线命令曾运行到 140 passed 后在 capacity_wake 测试期间人工中断（退出2），不记录为全库通过；原基线 capacity_wake 对照为 2 passed（254.95s），当前实现为 2 passed（59.33s）；耗时差异不作为性能提升证据。正式 DB/宿主/隔离 gate 未运行。原始结果及实现边界见 [首批验证记录](../verification/agent-lifecycle-first-batch/README.md)。

## 2026-10-23 文档一致性修正

复核发现首批实现后仅在部分文件开头补状态，proposal 正文、design Context/Non-Goals 和 D21 仍残留“未开始编码/没有 LangGraph/全部 planned”，造成同一工作树内自相矛盾。已逐处替换成当前实现和待实现边界，WF-39 保留目标 SHALL，在 Purpose 中链接差额说明并区分线程与共享 work 租约；明确 thread 租约已实现而共享 work 租约仍待 43.4。主工作区 proposal/design 增加跨工作树审阅入口，未复制任务勾选或代码。此次只修正文档，未新增能力，任务仍为 150/209。

## 2026-10-23 继续实施

当前工作树 154/209；42.2 动作目录及 42.3 候选合同已通过定向测试和缺陷复审并勾选。N09 未知清理不能通过 postcheck；N01/N07 亦须独立权威证据；G6.post 绑定实际 run/activation。目录测试 68 通过，候选合同 80 通过。

新增持久生命周期服务、原子修复请求、双层预算、共享 work 租约/处置服务、HTTP provider 协议；这些是部分实施，不再列为全无代码。现代修复执行、原图互斥与真实收尾事实已接线并完成定向验证。恢复计划编译器和 Driver/StateGraph 重入已编码；复审发现的缺少当前收尾回执、通用成功误作源节点验收已修复，相关测试 150 通过，修复复审中。完整调度消费者、团队/候选执行/门禁/晋级及真实后端验收仍待完成，43–45 尚不能整条勾选。前述首批及文档修正段为历史记录，不作为当前缺口清单。

归档校验已纳入活动 cleanup/reconcile job、由操作及环境组清单发现的作用域，以及绑定隔离标记的具体生命周期；同名但明确属于另一 run 的 provider、sampler 和资源租约不再误阻塞当前归档，未知或矛盾归属继续保守检查。105 项相关测试通过，限定范围的缺陷复审通过。完整 T6/cancel/supersede 接线仍待完成，未提供真实 PostgreSQL/沙箱/隔离后端的验收项继续保持未勾选。

44.2/44.3 provider 协议子交付已完成并经缺陷复审：64 项本地协议测试通过。v1 的负向 lookup 仅表示当前观察不到，不能证明永不执行，也不能退还预算；重放只检查所需查询能力，已有终结结果无需连接 provider。实际授权/预算接线仍在 43/44.1/44.4/44.5，实际 adapter 验收仍是 49.4，未勾选。


2026-10-23 恢复计划修复复审通过：原操作身份不可被同 work 新操作替换，N08 不得使用新封存的不同 raw 重算；失败仍产生当前 N07/N09/N10 收尾证据。最新恢复图/编译器验证 61 passed，限定范围规格/质量复审均通过。45.2 已勾选，CLI 确认当前 155/209；完整持久调度消费者正在实施。该计数不表示全节点自修复、真实后端或候选代码迭代已完成。

44.1 最小上下文与证据 capability 已通过限定范围复审：原件不变、脱敏派生件限读、持久累计额度、节点/运行/版本隔离、真实处置与离线封存模式。新增36项上下文测试，受影响集合856 passed；规格/质量复审通过后勾选，当前156/209。现代 provider/团队尚未消费 builder，不据此勾44.5。

42.4 独立测试夹具与历史 schema 兼容经规格/质量复审通过后勾选，当前157/209；相关6文件245 passed，新增参数变体按字面清单核对。已修复规划检查工具过时的96任务限制。实际收集/规划原件见[检查维护记录](../verification/agent-lifecycle-runtime/verification-maintenance-20261023.md)，16个缺失设计目标未被隐藏，完整门禁仍未通过。

确定性 work 生命周期调度经第二轮复审通过：真实原失败接管→规则诊断→修复→RecoveryPlan→源节点验收已连通；重复消费不重复计费，恢复子失败不能新建生命周期重置局部预算。8个相关文件209 passed，复审覆盖已复现的三个缺陷。未实现的模型/team、原操作安全对账消费者、完整策略A/B及多scope循环仍不能据此勾选43/45整项；下一批实施44.4/44.5。可调用入口见[生命周期监督](lifecycle-supervision.md)。

可选 investigator 与单层团队合同的子交付经复审通过：真实父/子job-thread绑定、角色/冻结模型预留校验、权威父deadline、原ID有界观察和一次性安全预算取消。修复后相关5文件189 passed（team44项）；随后编排层1692 passed、19 skipped，见[阶段回归证据](../verification/agent-lifecycle-runtime/runtime-interim-20261023.md)。coder/reviewer 工具、生产未知问题换策略和跨job安全续办尚未完成，因此44.4/44.5原整项不因这些子交付而无条件勾选，当前仍157/209。下一批实现site/provider原ID的有限安全对账。

2026-10-23 后续子交付：site/provider 原 ID 有限安全消费者已实现并完成 10 个相关文件 395 passed 的定向验证，**尚待独立复审**。独立安全授权、原 lifecycle/lineage 预算、原 ID 查询/一次性停止与清理、不可变隔离 resolution、追加后验和真实 RecoveryService 编译、provider terminal+children_stopped 关闭原槽位已接线；详见[合同与故障入口](lifecycle-reconciliation.md)。存在 observation 的现场仍缺可信远端 sampler 停止 receipt 合同，明确 blocked；Runner.cleanup 丢失回执仍缺原清理查询身份，保留未知意图/隔离且不重发。这些是代码/适配器合同缺口，不能归为仅缺环境；不勾选原 43.5/45.4 整项，不改变157/209，也不宣称 A/B、T6、多 scope 或真实 PostgreSQL 完成。

2026-10-23 原 ID 安全续办子交付复审完成：修复关闭已提交/任务回执未提交时的重放、provider 原任务未结束时的 outbox 交接两项缺陷。修复后3文件157 passed（消费者49项），限定复审规格/质量均通过；首轮395项为此前快照的相关回归。原43.5/45.4仍未整项勾选，当前157/209。下一步实施真实sampler停止证据生产与编排消费，候选/门禁/晋级及完整组合验收继续保留待办。

2026-10-23 本地 sampler 生命周期子交付经两轮修复复审通过：原结果/停止证明分离、首次stop意图及双预算、N07-only原owner冻结、非采样操作协议兼容、停止超时后的原query取证均已接线。最新限定5文件82 passed；初始354/199、round1的108为各自历史快照，不累加称全库通过。全45.3/45.4、新generation/T6/A-B/多scope及远端adapter仍待后续验收，tasks保持157/209。

2026-10-23 44.5 已完成并勾选：规则按实际证据选择，未知问题走有限investigator；策略与decision/outbox同事务，独立后验拒绝可用原预算切换方案，原回执和稳定失败计数不变。修复后11文件438 passed、无skip，限定复审规格/质量通过，见[验证原件](../verification/agent-lifecycle-runtime/strategy-fix-round1-20261023.md)。当前158/209；正在补ALI-01原文N02源节点组合，45.1未勾选，候选编码与真实后端仍待验收。以上更新取代前文对应阶段的待办状态，历史测试数不累加。

2026-10-23 45.1 完成并勾选：精确N02原失败→收尾→A失败→B成功→依赖重验→源独立验收，含另一组继续和两类后验拒绝反例；179项相关回归及独立复审通过，详见[N02验收原件](../verification/agent-lifecycle-runtime/n02-loop-20261023.md)。仅增加测试与监督说明，运行时此前已支持受信目录注入；生产N02动作仍unavailable。当前159/209，继续45.3新测量代际，T6及多scope后续独立实施。

2026-10-23 新测量代际初版已完成1826项编排回归（19项环境skip，352份源摘要不变），但独立审查复现三处缺口：操作准入未重验撤销/到期授权、版本化任务绕过在途归档检查、失败子代缺少明确重新授权入口。正在第一轮修复；45.3保持未勾选，当前159/209。该阶段绿色回归不能替代针对缺陷的验收，见[初版证据与缺口](../verification/agent-lifecycle-runtime/generation-interim-20261023.md)。

2026-10-23 用户明确停止Superpowers和agent审查，改为TDD及确定性相关测试门禁。已中止generation复审，没有将其记作通过。当前缺陷修复完成，`python scripts/check_measurement_generation.py` 实际176项通过、无skip，45.3本地验收完成并勾选，当前160/209。证据见[本地门禁](../verification/agent-lifecycle-runtime/generation-local-gate-20261023.md)。后续不再启动审查agent或沿用Superpowers流程；真实环境缺口不计作通过。

2026-10-23 核对tmp/codex-review-2026-10-23.md：复现并修正N07旧形状断言，明确无采样仍有not_started证据、零线程/零capture及重复幂等；核验上游源码提交并在worktree建立本地测试引用，三条缺源码失败已排除。对应10项通过，扩展相关门禁210项通过（不累加）。GateManifest已有模型但缺执行服务；44.4/45.4/45.5仍有真实接线缺口，未按报告建议盲目勾选。任务仍160/209；本次未做全仓、真实后端或合并验收。

2026-10-23 按用户要求直接 TDD 完成 T6 精确返工子交付：11项专项覆盖真实图路径、预算、恢复子、缺口归档、新版本解决和迟到重放；固定相关门禁278项全部通过，无 skip。证据 measurement-generation/20261023T151506Z-ee60f986。45.4 跨 scope cancel/supersede 矩阵未完成，仍160/209。另已实测 Harmonybrew QEMU 11.1.2 → TCG ARM64 Linux/KVM → L2 指令执行成功，不能再将本地功能验证整体判为无虚拟化路径；三产品部署仍待验证。


2026-10-24 停止流程继续实施：统一清单、处置版本、有界 Worker 停止任务、迟到失败挂接和归档证明校验已落地。固定本地门禁 514 项通过；14 项 PG/硬杀验收具备代码但缺 DSN，保持 blocked。见 [实施记录](../verification/agent-lifecycle-runtime/stop-workflow-20261024.md)。


## 2026-10-24 连续补齐

当前交付以[连续补齐记录](../verification/agent-lifecycle-runtime/remaining-20261024.md)与[能力账本](lifecycle-status.md)为准。历史“镜像加载、动态产品归属、候选 bundle 启动器、PG 切点代码尚缺”已被本轮实现取代。代码、固定离线门禁、真实后端和产品观测能力分别列示；未提供部署合同的现场验收不勾选，不自动发布或启用生产晋级。
