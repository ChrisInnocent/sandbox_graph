# Agent 生命周期测试门禁设计

2026-10-24 最新子交付：ALD-11 的统一 inventory、writer 处置版本、有界停止任务、迟到失败挂接与证明校验已落地，stop-local 514 项通过；固定门禁脚本及 14 个 PG/硬杀用例已提供，PG 实跑因缺 DSN blocked。详见[实施记录](../../../docs/verification/agent-lifecycle-runtime/stop-workflow-20261024.md)。以下较早的待实现描述为设计目标；tasks 45.4 未整项完成，候选晋级仍独立验收。


2026-10-24：新增 ATG-08（45.4 停止/收口实施门禁）与 ATG-09（门禁程序接口），随后修正 provider 测试的生命周期定位。新增两种未终结反例及相关187项通过，当前工作树另重跑测量/T6门禁278项通过；不是完整 stop-local/持久化/晋级验收。

## Context

2026-10-24：可信门禁服务、固定 CLI/profile 和 controller pytest 已有实现。当前 controller 套件在候选外收集并验证实际观察，候选新增测试单独执行；不得用本地 pytest 结果冒充生产 GateManifest。代码与真实后端验证边界见 [能力账本](../../../docs/orchestration/lifecycle-status.md)。下表保留验收目标，缺失故障切点明确列出，不把拟议文件名当现存测试。

## Goals / Non-Goals

**Goals:** 为实施者给出目标文件、独立夹具、测试行为、通过/拒绝判据、发布阶段和失败回路；让 agent 能修改候选并验证改动，不能自行宣称测试通过。

**Non-Goals:** 不用内存测试替代 PostgreSQL 竞态；不把宿主 mock 当真实 agent team；不因产品性能差而拒绝一个如实报告结果的框架版本。

## Decisions

### ATG-01. 门禁 G0–G6

| Gate | 执行位置与内容 | 通过证据 | 失败/缺失处置 |
|---|---|---|---|
| G0 规格与影响 | 可信控制进程：OpenSpec strict、diff、链接、node/handler/清理路径/环预算验证、风险/选测计算 | candidate/base/policy/graph/diff 摘要，完整必需 gate 与测试 ID 清单 | 返回候选修订；未知影响按 R3，未注册 handler 不激活 |
| G1 单元与合同 | 独立测试进程：ALU 全组、ORC 参数化、现有相关回归、schema/状态表/权限 | collect nodeids 与可信预期集合一致，JUnit+日志、关键反例确实失败 | fail 返回 coder；缺文件/零收集/关键 skip 不晋级 |
| G2 真实图集成 | 真实 StateGraph + 真实 lifecycle/Worker，业务外部效果受控 | 完整事件与节点序列、分层状态、操作次数、原证据未改变 | fail 返回候选；不能用全 stub 的 “succeeded” 序列证明闭环 |
| G3 持久并发与恢复 | 独立测试 PostgreSQL、真实 saver、双 worker、子进程硬杀 | attempt/许可/claim/CAS 查询、受控远端副作用账本、崩溃切点日志 | 缺 DSN/权限为 blocked；部署包含持久生命周期能力时不可豁免 |
| G4 候选执行隔离 | 独立候选沙箱和可信外部 runner，固定 suite/依赖、无生产凭据 | 写入/网络/秘密 canary 拒绝、可信 suite 摘要、候选摘要、报告来源 | 隔离不可用 blocked，worktree 本身不算隔离通过 |
| G5 审查与晋级 | PromotionService：风险/独立 review/已有授权/版本绑定/过期检查 | G0–G4 适用项通过，review 与授权绑定同候选，G5 decision receipt | 缺决策仅等待该候选；测试失败不能由审批覆盖 |
| G6 部署与运行恢复 | 限定测试域中的版本 worker 就绪、预生产 smoke、切换/取消/回退演练 | worker bundle 一致、旧图可 drain、新 run 能执行、资源无越权 | 未 ready 不切换旧派发；激活后失败按 D14 停新派发并收尾/回退 |

G0–G4 证明候选可进入审查；G5 产生晋级资格（不等待自身结果或 G6.post）；G6 拆为激活前 G6.pre（版本 worker 就绪及验收环境 smoke/切换演练）和激活后 G6.post（目标 run 观察）。激活要求 G5 与 G6.pre 均通过，G6.post 结果只在激活后产生并决定继续/停止/回退，不形成循环依赖。G6 smoke 不是重跑生产性能矩阵；真实适配变化的专项 smoke 需要测试环境和既有授权。只有满足产品适配专项门禁才标该适配 environment_verified，不把整套产品能力一并标已验证。

| 风险 profile | 必需范围 |
|---|---|
| R0 纯文档/非行为产物 | G0、G5；若新增/修改测试，则 G1 实际收集并执行这些测试。纯文档的 G1–G4/G6 可由可信影响规则标 not_applicable |
| R1 已授权非行为运行代码 | G0/G1/G2/G4/G5/G6；G3 仅在已验同一生命周期/数据库/依赖组合且未触及相关行为时引用兼容证据，否则执行 |
| R2 运行逻辑 | G0–G6；G3 必需且不可用内存替代；独立 review 与发布授权必须满足 |
| R3 敏感变更或影响未知 | G0–G6 + 所影响合同/产品/权限专项；人工 DecisionRecord 或此前仍有效且精确覆盖的授权，变更后重新判定可比性 |

影响分类取所有命中规则的最高等级。门禁策略/测试收集器/可信 suite/CI/依赖修改不能通过伪装纯测试 R0 降低等级。表中不是笼统允许 skip；not_applicable 需要记录规则 ID、输入 diff 和理由。

### ATG-02. 单元测试代码设计

以下路径均相对 tests/orchestration/unit，未来新增文件与现有 test_remediation.py、test_agent_*.py 共同验收。参数化测试的期望值来自字面量表/独立参考账本，不能调用生产状态转换器、预算计算或分类器生成期望。

| ID / 对应 ORC | 拟议文件与函数 | Given / When | 精确断言 |
|---|---|---|---|
| ALU-01 / 077,078 | test_lifecycle_models.py::test_scope_and_policy_validation | 两组、两 node；缺 schema/version、负/布尔预算、跨组 refs | 非法输入拒绝；持久写入/外部 submit=0；合法值精确保留 |
| ALU-02 / 081 | test_lifecycle_context.py::test_node_context_isolation | N02/N09 同 work，各有私有证据和秘密 canary | supervisor_id 不同；各 ContextPacket/读取结果不含另一节点私有内容或父对话，授权共享 refs 可读 |
| ALU-03 / 077,078 | test_remediation_policy.py::test_evidence_bound_proposal | 正确提案/伪造 hash/空事实/任意代码/越权 scope | 有效果动作必须引用失败证据；拒绝项不产生 order；测量原始记录逐字节不变 |
| ALU-04 / 079 | test_lifecycle_budgets.py::test_last_allowed_attempt_can_succeed | 总上限 2，第一次失败、第二次成功 | 修复动作恰好 2；第二次可接受并继续；第三次申请拒绝 |
| ALU-05 / 079,088 | test_lifecycle_budgets.py::test_budget_survives_context_and_revision_changes | 换 context hash、模型、policy、候选 revision、后继 run | lineage/lifecycle 用量不归零；未知 usage 扣足预留量；新授权调整是追加记录 |
| ALU-06 / 079 | test_lifecycle_state.py::test_failure_with_new_strategy_loops | 动作 A 失败，新增证据支持动作 B | 字面序列 diagnosing→remediation_pending→remediating→diagnosing→remediation_pending→remediating→reentering（postcheck 是转换证据，不另设 phase）；同指纹无新证据不空转；参数化遍历 ALD-02a 每条合法/非法边、排队/退避/worker 等待超期和证明失效；耗尽后只允许 safety_budget 处置，仍未知保留隔离；对照用户取消→cancelled 与额度/期限耗尽→收尾→blocked，后者保留 authorized_resume |
| ALU-07 / 080 | test_remediation_requests.py::test_request_replay_and_conflict | 同 request_id 重投和异内容重投 | 同请求同 job/attempt；异内容 Conflict；预算只扣一次 |
| ALU-08 / 081,087 | test_remediation_requests.py::test_authority_and_dispatch_cutoff | 假 owner、旧 epoch、非当前 generation、已关闭 gate | 普通修复无 submit；仅明确 cleanup/drain capability 可通行，不能新建测量 |
| ALU-09 / 081,082 | test_resource_disposition.py::test_proof_required_before_live_repair | caller 声称 disposed=True，ledger 仍运行；过期/错 scope receipt | 不能用布尔值放行；未知资源隔离；已封存证据的离线诊断可执行但没有现场 capability |
| ALU-10 / 082,083 | test_recovery_plan.py::test_destroyed_fixture_invalidates_resume | N02 失败后 N09 删除 fixture；旧 result_refs 仍存在 | 计划重新准备/核验依赖；禁止直接跳 N03/N05；不读取空或过期 target binding |
| ALU-11 / 082,084 | test_recovery_plan.py::test_measurement_continuity_and_generation | baseline 已断、部分 Q 已耗、有效性能 fail 三变体 | 不拼时序、不刷新 Q、不触发“测到通过”；授权新 generation 独立 work 保留原样本 |
| ALU-12 / 081 | test_agent_bridge_contract.py::test_capability_and_lost_reply | scope 不支持、缺状态查询、响应丢失、取消未确认 | 不猜宿主 API；丢 handle 时先按 request_id lookup，已知 handle 查原 task，不双 spawn；缺 provider 标明确 blocked |
| ALU-13 / 088 | test_agent_team.py::test_team_limits_and_result_binding | 超深度、超子任务/并发/token，错误 parent/node/版本结果 | 派发不超过字面上限；错身份交付拒绝；无无限递归 |
| ALU-14 / 085 | test_change_proposals.py::test_candidate_is_bound_to_base | 修改候选文件、脏工作树、路径越界、symlink | 原工作树/证据不变；输入未登记不混入；candidate digest 随内容变化；旧 revision 只读 |
| ALU-15 / 085,086 | test_candidate_revisions.py::test_failed_candidate_can_be_revised | revision 1 测试失败，revision 2 修复并通过 | r1 失败日志保留；r2 新摘要/门禁，原运行版本不变；最后预算内通过可晋级 |
| ALU-16 / 086 | test_gate_policy.py::test_highest_risk_and_existing_authorization | .md 后缀改执行入口、测试修改 skip、多个风险、已有精确授权 | 按实际影响最高风险；不降低可信门禁；授权覆盖则不重复请求，无覆盖才 waiting_decision |
| ALU-17 / 086 | test_gate_results.py::test_required_test_set_cannot_be_weakened | exit=0 但零收集/缺测试/skip/xfail；模型自报成功 | 必需集未全 pass 均不可晋级；N/A 只能由可信规则生成 |
| ALU-18 / 085,086 | test_gate_results.py::test_attestation_binds_exact_candidate | 换 tree/lock/graph/suite/policy/目标 plan/contract/auth hash，伪造 producer 或过期结果 | gate 不适用/拒绝；不能复用旧绿灯激活新内容 |
| ALU-19 / 086,087 | test_promotion_policy.py::test_every_activation_path_checks_gates | 直接 GraphService/CLI/worker 调用、陈旧 review、缺 worker ready | 全入口同样拒绝；无后继 run、旧 gate 不提前关闭；R3 合同变更成功时后继明确使用目标 contract/auth，旧 run 引用不变 |
| ALU-20 / 087 | test_lifecycle_closure.py::test_closure_waits_for_disposition | 原 attempt 已交付但 repair/candidate 仍在运行或未知资源 | work 未 fulfilled；取消只关新增派发；明确 blocked_closed 才部分归档；superseded 需 successor 与旧资源处置证据，满足后可收口 |

代码结构约定：
- 测试使用 fixture 组装真实被测 service；MemoryRepository 仅供 unit，独立 effect ledger 记录实际动作次数。
- test_lifecycle_budgets 用字面上限 2/输入费用 10/输出预留 20 等合成值，不作为生产默认。
- test_recovery_plan 手工声明 fixture 已销毁、不可复用，而非通过生产编译器反推期望。
- test_gate_results 的 oracle 是可信测试 ID 列表和逐项结果，不只检查 pytest 退出码。
- 单元层验证规则；ALU-07/08 的并发和 ALU-14 的实际隔离另由 G3/G4 验证，不在单元标题冒充真实并发/隔离通过。

44.5 当前落地映射：`unit/test_repair_strategy.py` 覆盖规则适用性、无模型明确等待、稳定失败计数、原子选择与独立后验证据；`unit/test_agent_team.py` 的 `test_supervisor_unknown_branch_uses_real_persistent_team`、`test_configured_team_never_calls_provider_for_deterministic_repair`、`test_untrusted_proposal_is_blocked_before_decision_or_effect` 覆盖真实未知诊断、已知零模型和提案拒绝；`graphs/test_work_strategy.py` 覆盖真实 A/B 及成功执行后后验拒绝组合。修复后相关438项通过并复审，证据见[记录](../../../docs/verification/agent-lifecycle-runtime/strategy-fix-round1-20261023.md)。这些实际路径补充上表拟议命名，不代表全部 ALU/G1 已验收；N02 精确组合另由45.1验收。

45.3 当前实现映射（本地门禁已通过）：`unit/test_measurement_generation.py` 覆盖版本计划、授权、真实派发和原预算；`graphs/test_measurement_generation.py` 覆盖旧代不可重开、基线断段、原操作接管和有效性能 fail。初版编排回归记录见[阶段证据](../../../docs/verification/agent-lifecycle-runtime/generation-interim-20261023.md)。操作准入撤销、版本任务归档和失败子代重新授权缺陷已补TDD回归；固定入口 `scripts/check_measurement_generation.py` 共176项通过，见[本地门禁](../../../docs/verification/agent-lifecycle-runtime/generation-local-gate-20261023.md)。按用户要求不再追加agent复审；T6/N07/N09组合另由45.4承担，不能用这些已有文件冒充全部ALI-02通过。

### ATG-03. 组合测试与故障切点

| ID / ORC | 拟议路径 | 场景与通过证据 |
|---|---|---|
| ALI-01 / 081,082 | graphs/test_work_strategy.py、graphs/test_work_remediation.py | 真实 work graph：N02 失败→收尾→诊断 A→修复失败→诊断 B→修复成功→依赖重验→完成；另一组继续。准确事件序列和所有失败 receipt 保留；本地组合已验收，见[N02证据](../../../docs/verification/agent-lifecycle-runtime/n02-loop-20261023.md)，不代表生产N02适配 |
| ALI-02 / 082,084 | graphs/test_work_remediation.py | N04/N05/N06 中断、N07/N09 缺口、T6 拒收参数化；采样/fixture 失效后禁止旧测量继续；性能 fail 不进入修复循环 |
| ALI-03 / 080,083 | persistence/test_lifecycle_budget_transactions.py（预算竞争）；原子 order 全矩阵待补 | 两真实数据库连接 barrier 同时预留最后一个额度/写同请求/接管；只一个 order/permit，旧 epoch 不能发布结果 |
| ALI-04 / 083 | persistence/test_lifecycle_hardkill.py（decision/budget/outbox 提交前后）；修复 submission/outcome 全矩阵待补 | review 落盘前/后、order 提交前/后、submission_started 前/后、远端已完成但回执前、outcome 后/continue 前硬杀；恢复不丢任务、不重做未知操作、不重置预算 |
| ALI-05 / 085,086 | graphs/test_candidate_evolution.py | 假编码 provider 产生真实隔离 patch，可信 runner 执行至少一个会先失败后通过的测试；r1 fail→r2 pass→版本证明；不是 stub 全部 gate=pass |
| ALI-06 / 085,086,088 | integration/test_candidate_isolation.py | 独立隔离后端：尝试写原工作树/读秘密 canary/联生产地址/篡改 trusted suite；全部拒绝，合法候选编辑和受控测试能执行 |
| ALI-07 / 086,087 | persistence/test_revision_activation.py（版本 CAS）；PromotionService 完整硬杀矩阵待补 | gates 检查后并发换候选/撤销授权/改 policy/两个候选竞争；事务内阻断 TOCTOU，仅一个后继谱系；目标 plan/contract/auth 改变导致旧证明失效，新授权撤销不能回退旧授权发布 |
| ALI-08 / 087 | e2e/test_agent_evolution.py | 原图有在途操作；新 bundle ready 才切换；旧任务原版本 drain，新 run 原子创建并使用冻结目标 plan/contract/auth，旧 run 不改引用；故障回退保留证据，未闭合共享域不能执行新测量 |
| ALI-09 / 081,088 | integration/test_host_agent_bridge.py | 实际配置的宿主/模型 provider：派发、任务查询、context 隔离、token 上界、取消及响应丢失对账；无环境 blocked，不阻塞应用内 provider 的独立验收 |

所有 L3 用例使用真实子进程退出/硬杀与可控 barrier，不以可捕获异常或随机 sleep 代替。外部操作无幂等/无查询能力时，预期是 unknown+隔离+不重发；不能把测试要求写成无法兑现的全局 exactly-once。

### ATG-04. 可信结果、基线和执行命令

测试 runner 的执行命令、必需测试清单和结果收集器取自受信版本；候选无法修改它们。候选自带新增测试单独运行，不替换基线。至少固定 Python/依赖锁、base commit、candidate tree、suite、policy、测试环境与 runner bundle；日志和 JUnit 在候选写权限之外保存，控制服务校验 digest 和 producer。候选子进程退出、超时、报告缺失均生成 fail/blocked，不能丢失该次尝试。

当前已有固定门禁入口，必需 nodeids 固化在 config/gates/lifecycle-*.json；缺环境在运行子进程前 blocked：

~~~bash
openspec validate sandbox-evaluation-workflow --strict --no-interactive
python -m pytest tests/orchestration/unit -q --strict-markers --junitxml=artifacts/gates/unit.xml
python -m pytest tests/orchestration/graphs tests/benchmark/integration -q -m "not postgres and not live and not linux" --junitxml=artifacts/gates/graphs.xml
python -m sandbox_graph lifecycle gates run --profile persistent --output artifacts/gates/persistent-current
python -m sandbox_graph lifecycle gates run --profile isolation --output artifacts/gates/isolation-current
python -m sandbox_graph lifecycle gates run --profile release --output artifacts/gates/release-current
~~~

G3 的测试数据库只从专用 SANDBOX_GRAPH_TEST_POSTGRES_DSN 引用获取，测试命名空间每例隔离；缺 DSN 不连接其他默认数据库。G4 用可声明候选隔离能力的测试后端；G6 用专门验收环境，不能默认向生产集群发请求。lifecycle gates 命令已接管固定 profile 的 argv，并把 collect nodeids、退出码、JUnit 与 GateManifest 对账；不拼 shell 命令、不接受模型提供的 arbitrary pytest args。

普通开发全库 pytest 允许保留已有环境相关 skip，但输出仅叫“离线回归结果”；生产 profile 必需的 G3/G4/G6 不得因此算通过。全库回归在影响广泛的 runtime/tasks/worker/persistence 变更时运行；仅文档变更不额外发起产品测试。候选失败后按最小反例迭代，最终晋级前运行该候选必需完整集合。

### ATG-05. 门禁求值及失败策略

1. 冻结候选身份和可信影响计算结果；required_gates、required_test_ids、允许 N/A 项在测试之前确定。
2. 运行各 gate 并追加结果；失败是 fail、环境缺失是 blocked，二者不能用 skip 包装成 pass。
3. 比对实际收集集合：缺失/重复身份、意外跳过、未执行参数化变体、日志不可读均拒绝该必需 gate。
4. 可信 runner 发布 GateManifest；candidate_revision、base、scope、suite、policy、environment、producer 和有效期逐一核验。哈希证明内容身份，不单独证明执行来源。
5. fail 可把最小失败证据回传 coder，产生新 revision；blocked 只等待缺失环境/必要决定，不通过改测试绕过；预算耗尽保存完整候选历史。
6. G5/G6.pre 使用同摘要门禁，G6.post 另绑 activation/run 身份；G5 结果不自依赖，激活前也不等待 G6.post；，任何新的代码/依赖/图改动使相关证明失效。成功的最后一次修订可晋级；不得要求成功后还有额外剩余修订次数。

建议 GateManifest 最少字段：
~~~text
schema_version, candidate_ref, candidate_hash, base_commit,
graph_hash, dependency_lock_hash, trusted_suite_hash, policy_hash,
target_plan_ref/hash, target_contract_ref/hash, target_auth_ref/revision,
required_gates, required_test_ids, collected_test_ids,
results[gate_id, outcome, case_results, environment_ref, logs_ref, junit_ref],
producer_identity, worker_bundle_ref, observed_at, expires_at
~~~

本地开发查询可展示 partial，但 promotion 只能消费与指定发布 profile 完全匹配的 pass 集合。N/A 的原因引用可信规则；人工批准不把 failed test 改写成 passed。

### ATG-06. 变异、追溯与交付

必须验证以下变异反例能被对应测试抓到：
- 去掉 work fence 或把 attempt 读取移到事务外（ALU-08、ALI-03）。
- 将 unknown usage 按 0 计或 context hash 变化就重置额度（ALU-05）。
- 原图清理 fixture 后直接 resume_from=N05（ALU-10、ALI-02）。
- repair failed 永远终止、candidate test failed 永远终止，或无限重试同一失败（ALU-06、15、ALI-01、05）。
- 将 performance fail 自动重新采样（ALU-11、ALI-02）。
- 只看 pytest exit=0、不验证收集集，或接受自写 gate 报告（ALU-17、18、ALI-06）。
- G5 只在事务外检查、worker bundle 不匹配仍激活（ALU-19、ALI-07/08）。

| 规格/设计 | 单元/组合 | 任务组 |
|---|---|---|
| WF-04/39；ALD-02–04 上下文与委派 | ALU-01–03、12/13；ALI-09 | 42、44 |
| WF-06/07/35/39；ALD-04/05 原子派发及预算 | ALU-04–09；ALI-03/04 | 42、43 |
| WF-21/34/39；ALD-05/06 恢复与收口 | ALU-06、10/11、20；ALI-01/02 | 45 |
| WF-36/39；ALD-07 候选代码循环 | ALU-14–16；ALI-05/06 | 46 |
| WF-39；ALD-08 测试证明与门禁 | ALU-16–18；ALI-05/06 | 47 |
| WF-36/39；ALD-08/09 晋级与版本运行 | ALU-19/20；ALI-07/08 | 48、49 |

交付必须逐项标 planned/implemented_unverified/locally_verified/backend_verified/adapter_verified，并附实际命令、代码版本、fixture/环境、nodeids、结果和证据路径。主任务原有勾选不等于本增量任一 gate 通过。本增量已开始实现；仅完成整条验收的任务勾选，其他保持未勾选。

## Risks / Trade-offs

- 门禁太少会把半实现当生产能力 → 固定 required set，按风险/影响选取；G3 不被 unit 代替。
- 门禁太多拖慢候选迭代 → 迭代时跑最小反例，晋级时跑完整必需集；兼容证明复用有 hash/期限约束。
- 编码 agent 同时改变被测代码和测试 → 单独可信基线套件与独立 reviewer；候选不可修改门禁服务。
- 宿主未提供 API → 应用内 provider 与宿主 bridge 分开验收，缺能力明确 blocked。

### ATG-07. 交接新增租约门禁 ALU-21

目标 tests/orchestration/unit/test_remedy_leases.py：从真实 TaskBackend.claim 结果调用 RemedyJobHandler，不预取得 thread lease；夹具产生独立 UUID。断言正常动作执行且释放租约，job epoch 与 thread epoch 不同仍使用真实 thread token；其他 owner 已持有 thread 时拒绝；续租失败取消阻塞动作；完成前过期或被接管时不写 outcome/continue；取消/异常仍释放自身租约且不误释放新 owner。既有 test_remediation.py 改为符合合同的调用，保留失败证据和次数的断言。

质量缺口另参数化 execution succeeded + NodeReview.reject、NodeResult.review_status=rejected，断言两者均创建监督事件但 receipt 不变；性能 fail 不是质量缺口本身。


### T6 返工门禁子交付（2026-10-23）

`tests/orchestration/graphs/test_delivery_rework.py` 的11项专项测试使用真实 Worker、
StateGraph、NodeExecutor、ReviewService，以及明确的合成 runner。覆盖 N07/N09 正例、
缺授权、错误/模糊来源、同名 group 跨 run 隔离、恢复子预算、归档、新版本解决和迟到重放。
本地固定门禁278项通过，无 skip；证据为
`artifacts/gates/measurement-generation/20261023T151506Z-ee60f986`。
尚不代表真实 PostgreSQL、产品沙箱现场或45.4全部跨 scope cancel/supersede验收。

### ATG-08. 取消、drain、版本替换的详细门禁代码设计

对应 ALD-11、WF-39 和 tasks 45.4。下面的函数名和新文件是拟实现清单，不声称已存在。此前 `test_lifecycle_stop.py` 是部分草稿；已有测试可重用，必须绑定到下列行为 ID。

2026-10-24 已落地的 R1–R3 回归位于 `tests/orchestration/unit/test_stop_regressions.py`，共25项，无 skip：ST-03 父 run/group 汇总、其他 run 排除和父级不接管；ST-06/07 现场/provider 排空自然完成、原期限、显式取消优先、无恢复派发；ST-08 上下文构建前/构建中截止与原请求幂等；ST-12 停止预留及 I/O 前重验；ST-13 旧版本处置与原后继证明、缺失/错误证明拒绝。受控 Runner/provider 通过真实应用服务、TaskBackend、Worker 和原预算执行；不是 PostgreSQL 或真实沙箱验收。证据见 [R1–R3 修复](../../../docs/verification/agent-lifecycle-runtime/stop-fixes-20261024.md)。这些用例仅覆盖对应行为的一部分，不代表下方整行目标或 ATG-09 已完成。

#### 1. 测试基座先满足合同

拟在 `tests/orchestration/fixtures/lifecycle_stop.py` 提供 `StoppedRunHarness`：

~~~python
class StoppedRunHarness:
    identity: StopFixtureIdentity  # run/plan/graph/group/work/lifecycle/request/thread
    repository: Repository
    worker: Worker
    effects: ExternalEffectLedger  # 独立记录 submit/query/cancel/children 状态

    def assert_ready(self): ...
    async def advance_until(self, predicate, *, max_steps): ...
    def stop_status(self, *, request_ref, lifecycle_id): ...
    def snapshot_evidence(self): ...
~~~

`assert_ready` 在行为测试前检查 artifact scope 格式、实际 handler 的构造参数、run/plan/gate/generation 一致、真实租约获取、原 failure receipt/activation 归属、provider capability 与 budget fixture。除专门验证缺字段的负例外，不使用手工残缺 LifecycleRecord 证明真实调用链。

`advance_until` 使用真实 Worker 和受控时钟，每步记录 job/outbox/phase 变化；达到明确谓词即退出。无可执行任务且谓词未满足或超过 max_steps 必须失败并输出原目标状态/待办，不把“队列暂时空”算成功。只有测试显式要求才推进时钟，禁止随机 sleep。等待场景的谓词是准确 waiting 状态及原资源仍存在，不是等到最终 closed。

所有断言通过 request_ref+lifecycle_id 读目标；不使用 scan()[0] 选状态。独立效果账本和字面预期是 oracle，不能调用生产 inventory/transition 推导预期。控制协调任务和原业务子任务使用不同 ID。共享 scope、晚到回执和多生命周期是基础夹具的正常配置。

#### 2. 精确测试矩阵

路径相对 `tests/orchestration/`；每个参数变体都登记为 required nodeid。

| ID | 文件 / 拟议函数 | 触发及独立断言 |
|---|---|---|
| ST-01 | unit/test_lifecycle_stop.py::test_stop_command_replay_and_conflict | 同请求两次仅一命令/每目标一个 job；异内容 Conflict；预算不变 |
| ST-02 | unit/test_lifecycle_inventory.py::test_typed_ownership_matrix | run/group/work/node × 同名其他 run / 缺 operation / 矛盾身份；foreign 排除，ambiguous 阻塞，不执行删除 |
| ST-03 | unit/test_lifecycle_inventory.py::test_parent_includes_child_provider | provider 绑定子 lifecycle；父 stop 和 archive 均等待；独立外部账本证明子任务仍活跃 |
| ST-04 | unit/test_lifecycle_stop.py::test_cancel_accepted_is_not_closed | cancel accepted=True、children_stopped=False；精确原目标 waiting，submit 恰好一次；终结回执后才 closed |
| ST-05 | unit/test_lifecycle_stop.py::test_unknown_without_original_identity | unknown source、账本为空或原 ID 缺失；needs_input/隔离；绝不从空列表生成关闭证明 |
| ST-06 | unit/test_lifecycle_stop.py::test_cancel_and_drain_differ | 截止前 submission_started × cancel/drain；cancel 请求停止，drain 原期限内只观察；仅 permit 无 start 不放行新提交 |
| ST-07 | unit/test_lifecycle_stop.py::test_drain_deadline_preserves_reason_and_budget | FakeClock 到原 deadline；转安全停止，保留 supersede/drain 原因；不刷新任何预算 |
| ST-08 | unit/test_lifecycle_stop.py::test_stop_admission_matrix | repair/role/recovery/generation/diagnosis 五入口及执行前二次检查；cutoff 后新派发=0；原回执和安全处置仍可提交 |
| ST-09 | unit/test_revision_drain.py::test_versioned_safety_jobs_keep_original_binding | cleanup/reconcile 及 measurement-v1 前缀 × 两 policy；保留原 job、bundle、operation/grant；伪造前缀不能获得授权 |
| ST-10 | unit/test_lifecycle_stop.py::test_late_failure_attaches_once | stop 前/后 failure outbox 两种顺序；精确原命令挂接一次；新版本不受旧 stop 影响 |
| ST-11 | unit/test_lifecycle_stop.py::test_live_owner_and_expired_owner | 活 owner 不抢占；过期通过真实 lease 接管；旧 epoch 不能发布 proof/关闭状态 |
| ST-12 | unit/test_lifecycle_stop.py::test_legacy_cleanup_does_not_bypass_grant | 同组 modern+legacy 两目标；modern 只由原 reconciler；legacy 无权限保持缺口；授权存在才单独清理 |
| ST-13 | unit/test_lifecycle_closure.py::test_stop_proof_and_phase_are_not_interchangeable | terminal phase 但有 provider/sampler/resource/lease/stop pending；clean archive 拒绝；partial 只能按明确隔离处置合同 |
| ST-14 | unit/test_lifecycle_closure.py::test_late_effect_invalidates_stop_proof | proof 后写入迟到 resource/receipt；处置 revision 改变，旧 proof 拒绝；原 proof 内容不变 |
| ST-15 | graphs/test_lifecycle_stop.py::test_real_worker_stop_chain | 真实 graph/NodeExecutor → failure → lifecycle → RunService.cancel → 原 reconciler → N07/N09/N10/交付/closure；业务继续=0，收尾顺序与产物可验 |
| ST-16 | graphs/test_lifecycle_stop.py::test_supersede_preserves_old_effect_owner | 两版本共享域；新 run 能建立但域未释放前不得测量；旧 operation 只用旧 adapter；别的无关组可继续 |
| ST-17 | graphs/test_lifecycle_stop.py::test_scope_node_matrix | work 与 E/C、R/G、A/F 的实际来源绑定；只证明取消接线，不据此声明 45.5 修复动作已完成 |
| ST-18 | persistence/test_lifecycle_stop_transactions.py::test_cutoff_races_submission | 真实 PG 两连接 barrier；start/cutoff 两种提交顺序；截止后准入拒绝，截止前潜在在途可查询，绝不丢原身份 |
| ST-19 | persistence/test_lifecycle_stop_transactions.py::test_proof_races_late_effect | 两连接竞争 seal/迟到证据，及双 worker 接管；CAS/fence 只允许与当前事实一致的证明消费 |
| ST-20 | recovery/test_lifecycle_stop_recovery.py::test_crash_cutpoints | T1 提交前/后、T2 入队/ACK、T4 调用后回执前、T5 事实后、T6 提交前/后硬杀；任务不丢、原 ID 不重提、费用不重扣 |

ST-04 定位子项已完成：原失败由多生命周期夹具中的首行读取导致。现按原 request/lifecycle 精确定位，参数化 running/cancelled 父状态且 children_stopped=False，两者均断言 waiting/无关闭证明；终结回执后断言 cancelled/closed，submit/stop 各一次且不派恢复。187项相关测试通过；完整 fixture 与其它 ST 条目仍按任务逐项实施。

#### 3. 固定门禁入口（已有实现，覆盖按固定 profile 核对）

已提供 `scripts/check_lifecycle_stop.py`，沿用本地 gate 的产物目录惯例，但补完整收集核对；不把现有 `check_measurement_generation.py` 改名伪装成生产门禁。

| Profile | 必需测试 | 环境与结论 |
|---|---|---|
| stop-local | ST-01–17 全部变体 + 当前 measurement-generation 固定回归文件清单 + 受影响 agent-team/reconciliation/repair 测试 | 本地离线；不证明 PostgreSQL 或真实 provider |
| stop-persistent | ST-18–20 全部切点 + 既有预算/派发持久竞态 | 专用 SANDBOX_GRAPH_TEST_POSTGRES_DSN；缺失则 blocked，不能退到 memory |

CLI 为 `python scripts/check_lifecycle_stop.py --profile stop-local`；真实环境与覆盖限制按实际输出核对。参数只接受登记的 profile，禁止追加任意 pytest args/测试路径过滤。测试清单由维护者在受信配置中登记，不能在同次执行中把实际 collect 结果自动当作预期集。

执行器拟拆为以下纯函数/过程，便于对 runner 本身做单元测试：

~~~python
load_profile(profile_id) -> GateProfile
freeze_inputs(root, profile) -> InputManifest
collect_cases(profile, input_manifest) -> CollectionReceipt
validate_collection(required_ids, collected_ids) -> CollectionDecision
execute_cases(profile, input_manifest, output_dir) -> ExecutionReceipt
evaluate_cases(required_ids, collection, execution) -> GateDecision
publish_evidence(input_manifest, collection, execution, decision) -> LocalGateReport
~~~

执行前冻结 git HEAD、dirty diff 摘要、未跟踪源码/测试清单及内容摘要、Python/锁文件、profile 和 suite 摘要；仅记 HEAD 无法描述当前脏工作树。执行后重算输入，执行中变化则本次结果失效。单独输出 collect nodeids、逐阶段 pytest report、JUnit、stdout/stderr、退出码和超时状态；required nodeid 必须唯一收集且 setup/call/teardown 均成功。缺参数变体、零收集、重复、skip/xfail/xpass、collection error、teardown error、报告丢失或摘要变化都不能 pass。JUnit testcase 数只能辅助校对，不能替代 nodeid 集合。

入口用 argv 数组及超时，不拼 shell；清理 PYTEST_ADDOPTS 等过滤输入、固定配置和明确插件集。stdout/log 中保存实际 argv 和环境摘要，不输出 DSN/秘密。进程超时/异常也写失败证据，不丢本次尝试。退出码约定 0=pass、1=fail、2=blocked；OpenSpec 校验独立记录，不能顶替代码测试。

拟新增 `tests/orchestration/unit/test_lifecycle_stop_gate.py` 验证：缺 required case、参数遗漏、重复报告、call pass 但 teardown fail、skip/xfail、零测试 exit0、JUnit 缺失、超时、源文件执行中改变、脏树摘要变化、环境缺失、合法完整集合。这些测试注入小型受控进程结果，不能递归执行全套 gate；另有一个真实小型 pytest 子进程合同用例核验收集与执行报告格式。

#### 4. 实施顺序与交付条件

顺序固定为：夹具+ST-04 定位 → ST-01/02/03/05 归属模型 → ST-06–12 准入与控制 → ST-13/14 收口 → ST-15–17 真实图组合 → stop-local → ST-18–20 持久门禁。每步只补使目标反例通过的实现，再跑相关回归；完整本地集合通过且无新增行为变更后不反复重跑。

不可接受的弱化包括：删除取消测试、把 waiting 改为 closed、以更长 sleep 掩盖调度顺序、放宽归属、清空资源表、把缺 DSN 改 skip、只检查退出码。指定反例必须捕获：删 admission check（ST-08）、漏子 provider（ST-03/04）、去 fence（ST-11/19）、复用过期 proof（ST-14）、总清理绕过授权（ST-12）。

离线通过只能记录 locally_verified；持久环境阻塞单独记录，相关任务保持未完成。provider 定位修正后的187项相关测试及重跑278项测量/T6门禁已通过；两集合有重叠，不相加为独立用例总数。stop-local runner 后续已交付并记录514项历史门禁；当前新增代码必须另跑更新后的固定集合，不能沿用历史结果。

### ATG-09. 生产可信门禁服务的代码边界（tasks 47，真实后端另验收）

本地 runner 与候选晋级不是同一信任等级。现有本地脚本在开发工作树执行 pytest，不能签发用于自动晋级的可信 GateManifest。

| 拟议文件 / 接口 | 输入与输出 | 不可绕过的检查 |
|---|---|---|
| services/gate_policy.py::plan(candidate_ref, profile_ref) | 冻结候选、真实 diff、可信影响规则 → GatePlan | 最高风险、完整 required IDs、N/A 规则理由；候选不能自选必需集 |
| services/gates.py::request_in / record_result_in / evaluate_in | request_id+GatePlan → gate jobs → GateManifest | 请求幂等、来源鉴权、候选/目标计划/授权/环境/bundle 摘要和有效期 |
| runtime/gate_worker.py::execute(job) | 受信 runner 配置与只读 suite、冻结候选 → 观察产物 | 真实隔离；候选无可信输出目录写权；控制端保存结果和 producer 身份 |
| services/evolution.py 的候选修订入口 | failed manifest+最小证据 → 新 revision | r1 失败保留；r2 改动重新冻结和验证，模型自报通过无效 |
| PromotionService / RevisionService（ALD-08） | 同候选 GateManifest+review+授权+ready | 同一激活事务重验；任何必需 fail/blocked 拒绝 |

GatePlan、GateManifest 继续使用 domain/evolution.py 的严格身份合同，新增执行记录不复制另一套候选模型。可信控制端运行黑盒协议验收或隔离外观察；把可信 pytest 文件放在候选进程中执行本身不能防止恶意候选篡改解释器/结果，必须在 G4 证实测试执行与报告来源隔离，未具备时禁止自动晋级。

门禁服务自身测试仍映射 ALU-16–19 / ALI-05–08：集合缩水拒绝、伪造 producer 拒绝、r1 fail→真实 r2 pass、同名不同摘要拒绝、授权撤销竞态拒绝、缺 ready worker 不关闭旧 gate。先交付 stop-local 的确定性收集核验，再复用其纯求值规则到 tasks 47；不能用 stop-local 的绿灯把 tasks 47 勾选。

### ATG-10. 后续固定反例与实际文件

| 行为 | 实际测试文件 | 证据边界 |
| --- | --- | --- |
| 受信 suite root 权限、缺 nodeid、skip、候选自报、候选附加测试分离 | tests/orchestration/unit/test_controller_pytest.py | 实际 controller pytest 子进程；隔离输出为受控协议 |
| Git 导出体积、路径/CAS、冻结恢复 | tests/orchestration/unit/test_candidate_workspace.py | 实际本地 Git/文件操作 |
| rootfs 校验后重验启动权限、原进程取消 | tests/orchestration/unit/test_candidate_isolation.py | 原语/受控后端，不登记 backend_verified |
| rollout 关闭新工作而保留安全收尾 | tests/orchestration/unit/test_lifecycle_rollout.py、test_candidate_safety.py | 本地应用服务，PG 竞争另验收 |
| 回退新 run、等待 worker 持久化/请求冲突 | tests/orchestration/unit/test_release_rollback.py | 本地实际状态服务，真实发布验收另列 |

每次 profile 维护通过 scripts/freeze_lifecycle_profiles.py 显式更新 required nodeids；执行器不以当次 collect 自动替代预期集合。缺专用 PG/隔离/产品合同的 profile 在子进程启动前 blocked。

### ATG-10 可信套件装配与固定变异

`sandbox_graph/testing/suite_catalog.py` 和 `scripts/prepare_trusted_gates.py` 实现完整六 gate 的外部控制端套件装配、真实 collect、required nodeid 精确比对、源码/候选/输出隔离和 GateCase 摘要派生。G5 保持独立评审，G6.post 保持实际后继运行的账本观察。装配结果 prepared/unverified 不能授权晋级；部署所需黑盒断言与隔离命令不得由候选替换。

`testing/critical_mutations.py` 的十类变异分别绑定原 fence、预算、fixture 重建、有限循环、修复失败继续、候选失败继续、性能失败不自动重采、自签 gate、worker 缺失及零收集的既有 baseline nodeid。脚本只在临时副本改变源码，不修改主树；记录实际阶段回执及测试摘要。任何收集/导入/进程错误均不能冒充抓住变异。

### ATG-11. saver 真实租约锁竞争顺序

`tests/orchestration/persistence/test_fenced_saver.py::test_actual_saver_and_takeover_serialize_on_same_lease_row` 参数化 checkpoint/pending writes × 同步/异步 × writer/takeover 先持锁共 8 项。独立 SQL 连接用 `pg_blocking_pids` 证明后者实际等待同一租约行，先写后接管允许原写提交，先接管后写拒绝旧 epoch；再查 SQL 行数与 epoch。没有 DSN 时 skip，不能把收集成功当事务正确性证明。
