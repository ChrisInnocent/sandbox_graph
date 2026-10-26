# 工作图的可信恢复计划

任务 45.2 实现确定性恢复编译及实际 Driver/StateGraph 入口检查。本文描述代码接口，不代表真实后端、产品适配或新测量授权流程已经验收。

`RecoveryService.compile_recovery(lifecycle_id, receipt_refs=None, *, token)` 及事务内版本 `compile_recovery_in` 要求实际 lifecycle owner 和共享 work owner。编译器消费最近一次后验通过且效果确定的现代修复结果及冻结 order；调用方不能选择恢复节点，也不能替换冻结前置结果。Driver 和工作图均拒绝调用方提供的 `resume_from`。

编译器写入不可变的 `recovery_plans`、`recovery_contexts`，保留原始 input_refs、attempt、失败及修复回执、receipt 摘要、输入证据身份、测量绑定、资源账本快照和明确路由。`recovery_order_plans` 仅是索引，不能据此覆盖原计划。同样的事实在凭证有效期内重复编译得到同一计划。

图摘要覆盖实际工作图的节点、成功/失败边、入口、恢复路由及测量边界，并纳入已注册的图定义；构图器直接使用这份路由合同。合同摘要必须来自 `contracts[contract_ref]` 内容或部署时的 `measurement_bindings[work_id].measurement_contract_hash`。缺少内容时阻塞，不能仅对引用字符串计算摘要冒充版本证明。

| 失败节点 | 恢复路由 | 保留的约束 |
|---|---|---|
| N00 | N00、N01、N02、N03，再进入允许的原测量或封存尾段 | 合同变化需要另一个计划；原失败不变 |
| N01、N02、N03 | N01、N02、N03，再进入允许的原测量或封存尾段 | 保守地重验准备和前检；缺失、销毁、未知、过期或变化的 fixture 不允许复用 |
| N04、N05、N06 | N07、N08、N09、N10 | 中断与不足样本归属原 generation；源测量节点仍未验收 |
| N07 | N07、N08、N09、N10 | 封存原证据，不新增采样 |
| N08 | N07、N08、N09、N10 | 要求原 N07 的有效原始证据；本 attempt 重新封存并保持测量来源 |
| N09 | N07、N09、N10 | 本 attempt 先封存再清理；不能返回测量 |
| N10 | N07、N09、N10 | 本 attempt 重新封存、清理并交付，不增加样本 |

每个恢复 attempt 必须实际产生属于当前 activation 的 N07、N09、N10 回执。不能把上一 attempt 的回执重新标记成当前收尾事实；Driver 和处置服务继续严格核验三者的来源。因此 N08/N09/N10 恢复也包含必要的封存、清理与交付节点。

准备失败后，只有不存在原测量回执、observation、测量操作、未知操作身份或性能失败，才允许首次进入 N04–N06。测量可能已经开始时，这些节点从恢复路由中排除。计划不分配新 generation、不重置请求总量 Q、不结算其他修复，也不修改预算。另行测量须经授权创建独立 work；本服务不发出这种授权。

凭证有效期受已验收修复的真实处置凭证、lifecycle/order deadline 和授权期限约束，不制造默认 TTL，也不延长过期证据。过期后需要新授权和对账证据。启动时在新 owner 下重新生产并验证处置凭证；前一 owner 的凭证不能直接作为新 owner 的许可。

`Activation.recovery_plan_ref` 是专门的控制字段。GraphJobHandler 从 payload 提取它后再恢复原 input_refs，业务输入不会混入控制参数。Driver 复核 scope/run/plan/graph、work revision、合同与测量绑定、输入版本、回执来源、资源快照/时效、当前派发 generation 和授权，并原子绑定计划到唯一 activation/thread。未知或隔离效果、过期证明在任何节点执行前阻止启动。

工作图恢复入口要求 Driver 的当前 owner 启动记录，并从冻结回执重建 result_refs 和状态。重验节点的新回执替换已失效输出；未受影响回执保持原引用。被跳过的失败测量回执保留原 data_status 和 performance_status。每个恢复节点检查当前所有权和版本；启动后派发关闭时，普通节点停止，但仍允许有界 N07/N09/N10 封存、清理和交付。

`graph_result.recovery_acceptance` 记录源节点/失败回执、修复回执、测量是否禁止及本次源节点回执。图内只记录 `execution_review_accepted`，并保持 `accepted=False`；Driver 写入当前 attempt 的 N07/N09/N10 收尾事实后，才调用确定性的节点专属后验。N01 复核实际本人操作的权威终态；N07 要求 valid 数据、原始产物与无中断/缺序的实际 collection 一致；N09 要求 cleanup=complete，并与当前 owner 下的新处置凭证、资源及操作集合一致。缺证据、insufficient 数据、unknown/incomplete 清理或残留资源均不能通过。验收结果同时写入返回值和持久 activation 记录，并给出后验/处置引用和明确原因。

N01 恢复额外冻结已验收修复回执中的原 operation 引用，并核验它们属于修复执行前 `repair_executions.operation_refs` inventory；同时冻结其 work/run/request 身份摘要。重新进入 N01 只能返回这组原引用的非空子集；同 work 的新建 replacement 即使已经终态，也不能替代原操作对账。图在 N01 回执落库后立即检查，失败时直接进入封存/清理，不继续 N02–N06；最终节点验收再次执行同一检查。

当前 attempt 的新 N07 收尾回执和已有 N08 分析依赖分别保存。当恢复计划已有 valid 原 N07 raw 时，`recovery_contexts.analysis_dependency` 冻结该原回执、内容摘要及 raw artifact 身份。N08 执行前核验原依赖未变，当前 N07 回执确实属于本 activation、执行和 review 通过、数据 valid，且 raw 身份集合与冻结原始数据相同。不同 raw、缺失或无效封存会阻止调用 N08，然后继续 N09/N10；相同 raw 可正常重算，当前 N07 仍用于严格收尾。

`graph_result.recovery_dependency_failures` 记录节点到原因的映射，例如 `N01: original_operation_mismatch`、`N08: analysis_raw_mismatch`。N01 的实际成功执行回执保持原事实，依赖拒绝另行记录；未调用的 N08 记录 blocked，不伪造执行回执。上层调度须将这些拒绝与 `recovery_acceptance.accepted=False` 一并消费，不能仅凭收尾闭合推进 fulfilled。

尚无可用节点专属验收合同的源节点返回 `node_acceptance_contract_unavailable`，不能用通用 review 成功冒充验收。此状态与收尾已闭合是独立事实。N10 成功不能让未重新验收的 N04–N06 变成 fulfilled；上层调度也不能从交付推断性能通过。

历史 remedy_orders 保留。旧 handler 仅处理已登记的对账/封存/清理动作并记录事实；对已登记 work 获取同一共享互斥锁，拒绝与原图同时执行。它不再按下一编号节点派发 work；继续恢复必须使用生命周期合同。

本地测试使用显式 MemoryRepository、受控业务动作、真实现代修复 worker、Driver、StateGraph 和 NodeExecutor。PostgreSQL 崩溃恢复、真实 fixture/runner、完整监督调度与分层收口仍单独验收。N02/N03/N04–N06/N08/N10 的具体修复动作是否可用由动作目录决定，恢复路由测试不使尚未实现的动作自动可用。

## 明确授权的新测量代际

`RecoveryPlan` 仍不授予重开测量的权限。实际 `NodeExecutor` 的连续性守卫会拒绝同 work 的新测量 attempt，但保留原 operation 查询和 N07/N09/N10 收尾。独立的 `MeasurementGenerationService.compile/authorize/request` 才能建立新 run/work；其严格 envelope、旧计划摘要兼容、预算继承和真实派发边界见 [measurement-generations.md](measurement-generations.md)。该服务不改变既有 recovery route，也不让有效 performance fail 自动触发重测。
