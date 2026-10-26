# 原 ID 有限安全对账

状态：本地 MemoryRepository 定向验证，等待独立复审。属于原 43.5 / 45.4 子交付，不代表策略 A/B、T6、多 scope、真实 PostgreSQL 或真实产品适配器验收。

`LifecycleReconciliationService` 与 `LifecycleReconciliationHandler` 消费原 `repair_reconciliation_required`、`provider_reconciliation_required`，通过真实 `Worker` / `TaskBackend` 的 `reconcile` job 执行。未配置时保留原 outbox 和明确的依赖 blocker。它不 submit 模型或现场操作，不重建 role、order、lifecycle 或 lineage。

## 明确装配与权限

```python
from sandbox_graph.services.lifecycle_reconciliation import (
    LifecycleReconciliationService, ReconciliationConfig,
)

config = ReconciliationConfig(schema_version=1, cost_unit=deployment_cost_unit,
    query_cost=maximum_query_cost, stop_cost=maximum_stop_cost,
    cleanup_cost=maximum_cleanup_cost, backoff_seconds=observation_backoff_seconds)
reconciliation = LifecycleReconciliationService(repository, config=config,
    authorizations={original_order_or_team_request: independent_authorization_ref})
reconciliation.authorize(independent_authorization_ref,
    operation_kind='site', target_ref=original_order_ref,
    principal=authenticated_operator, expires_at=absolute_safety_authorization_deadline,
    permissions=('query', 'stop', 'cleanup'))
handlers = build_job_handlers(..., operation_service=actual_operation_service,
    agent_provider=actual_scope_bound_provider, reconciliation=reconciliation)
worker = Worker(repository, handlers, policy=explicit_worker_policy)
```

变量必须来自实际受信部署合同；省略号表示已有装配参数。三个费用都是明确、严格正整数，没有默认零费用、价格推导或币种换算。它们是集成层承诺的单次安全调用最大费用，包括该 provider 调用所需的有限 capability 核验；当前协议不报告安全调用可信用量，因此按整个最大费用结算。单位必须与原 lifecycle / lineage 和 policy 相同。

`authorize` 是已经认证的部署/操作员边界使用的内部 Python 接口，不是未认证的外部 API。授权冻结实际原 source、order/outcome/isolation 或 team contract/envelope、run/scope/版本/revision、原 operation 全集和原双层安全限额。操作员明确列出 query/stop/cleanup 权限；旧 repair 权限不会自动成为安全清理权限。provider 授权只允许 query/stop。撤销、过期、身份变化仍拒绝。授权不会扩额或延长原 safety deadline；费用上限、调用次数和 policy 限额均须满足。

配置映射优先按原 order/team request ID 选择授权，也可为单目标 lifecycle 绑定授权引用。所有现场调用经过实际 OperationService，provider 调用经过实际 AgentBridge。调用前与提交远端事实前重验真实 job、thread、lifecycle、共享 work owner、授权和预留。普通 dispatch gate 关闭、supersede 或业务期限已到，不会取消已经明确授予的有限安全处置权限；它也不会授予普通修复、测量、模型或恢复权限。

## 持久有限性与重放

每个原分域目标只有一个 `lifecycle_reconciliations` 会话。不同 outbox ID、换 worker 或换 job 不创建新目标或新预算。每轮 query 在原双层安全预算中原子预约 `query_calls=1` 和明确费用，同时保存 `reconciliation_calls` 原 ID 意图。停止与清理各有独立类别和费用；stop 按原 operation、cleanup 按原目标的实际资源集合最多发出一次。原意图可能已经提交时，即使进程在网络调用前后中断，也不重发停止/清理。

已知本地 authoritative terminal 不会再花一次查询额度。运行中状态和取消 accepted 保留未知，持久化 `round`、`next_due_at` 与唯一后继 outbox；观察按明确 backoff 继续，调用仍逐笔检查原剩余额度。每轮控制 job 最多两次实际 claim；不是新的业务 child job，也不重建业务配额。额度/期限耗尽停止远程派发并保存具体 blocker。最后一次额度内取得完整关闭证据可以成功，不额外要求“还有下一次额度”。

Worker 终结 watchdog 会结算已经预留但未拿到回执的安全调用，保留 unknown 和原 ID，标记 `safety_job_stopped_unknown`；不自动创建无限替代 job。查询、原操作状态、清理记录已经真实提交的，可以重读本地原记录；unknown 清理不能因新 job、重复 outbox 或改变资源集合而再次执行。

现场与 provider 的成功 job result 现在与关闭事实在同一事务保存。原 claim 到期后接管只返回原成功结果，不重写不可变关闭记录、不使 closed 降为 blocked。对于旧版本已提交 closed 但缺 job result 的窗口，只能从经过验证的原 resolution / provider terminal fact 重建相同结果；缺证明则拒绝，不能仅凭 state=closed 绕过验证。

## 现场关闭与追加后验

现场消费者要求原 order/request/execution/outcome/isolation、原 source receipt 与 raw 证据摘要精确匹配。当前 work 版本与原 operation inventory 必须相等，资源必须归属这些原 operation。关闭还要求：每个原 operation authoritative terminal；真实资源全部关闭；原 OperationService collection、完整事件序列和精确 artifact 集；可信 work_runtime 的 N07/N09/N10 收尾、关闭 admission、attempt_closed，以及可信 sampler stopped / not_started 事实。

本地进程内 sampler 已有 [版本化生产与可信消费合同](sampler-lifecycle.md)：实际 BenchSession N04/N07 → BenchGateway collection/producer 核验 → OperationService 同事务入账 → ObservationService.closed_in。真实 loop、在途 capture、尾样本和 close 完成的 proof 可以支持本 order 的安全关闭；缺失 proof 的 observation 保留 `sampler_stop_adapter_receipt_missing`。ObservationService.stop 仍只修改账本/序列，原 operation terminal 和 caller stopped 布尔值不能补物理停止证明。通用远端 host/process 适配器与跨进程未核验产物恢复能力尚未实现，不能归为仅缺环境。无 observation 且真实 work_runtime 明确 not_started 的现场仍可正常关闭。

关闭事务先核验上述事实，保存不可变 `repair_isolation_resolutions` 及完整冻结 material/摘要、producer、真实 job/owner 提交链，然后才释放本 scope / run 的资源 reservation 并广播可用域。原 `repair_isolations` 和 unknown `repair_outcomes` 均保留。共享 isolation 读取器验证精确原件绑定和可信提交链；其他 isolation 不会被顺带清除。历史关闭不因后续合法 work revision、新资源或旧授权过期而复活；新未知操作仍由当前 disposition/inventory 阻止不安全执行。

独立重跑原动作后验后，追加 `repair_reconciliation_outcomes`；只有 accepted 且业务期限仍有效时发布 `repair_recovery_required`。失败沿原有限 retry/blocked 路由。明确取消只有完成真实收尾后才可终结为 cancelled；额度/超期保持 blocked 语义，未知收尾不伪造终结。

RecoveryService 只通过统一可信 resolution 读取追加 accepted 事实，重验原 receipt/raw、原 operation 集合、动作后验和当前 disposition，使用本次对账的新 disposition 期限。原业务 authority、work revision、gate 和版本继续独立核验；安全授权不能替代恢复权限。原件仍为 unknown，缺失/伪造 resolution 或撤销业务权限均不能编译 plan。

## Provider 分域

AgentTeamService 的窄 `safety_target_in` 只认可已终结原 role job 的 waiting_provider investigator、原 contract/envelope/request ID、原 lineage 和已结算模型预留；coder/reviewer 的工具依赖 blocked 不进入该入口。新安全 job 沿原 request ID lookup 或原 handle status/cancel，Bridge 的提交 guard 防止旧 owner 保存迟到终结事实。没有 submit/collect/proposal 入口，也不重新开放普通团队模型网络权限。

waiting_provider intent 可能先于原 Worker 的 TaskBackend.finish 可见。此时安全消费者保留原 intent 并记录 `waiting_dependency / original_provider_job_not_finished`，不创建会话、抢 owner 或发送安全调用，也不改变原授权。原 job 完成后重放仍须精确独立安全授权。错误的原目标、版本或 schema 只阻塞该 outbox 项，不让权限异常终止共享 pump；其他可投递项继续处理。

只有实际 terminal 且 children_stopped 才追加 `agent_team_reconciliations`、将原 task 标为 reconciled 并释放其原 lineage 并发槽。accepted cancel 保留 waiting_provider。provider 不触碰现场 isolation、资源、sampler 或恢复计划。原 unknown 模型 settlement 的满额消耗保留；迟到 usage 仅追加事实，没有会计规则时不退款、不以不同 usage 覆盖历史。

## 故障恢复入口

| blocker | 保留事实与处置 |
| --- | --- |
| 未装配 / 独立授权缺失 | 原 outbox 未 delivery，配置实际服务并登记精确安全授权后续办 |
| operation_binding_missing | 不虚构原 ID，保留原阶段/隔离；需可信原操作归属恢复合同 |
| original_operation_collection_or_event_chain_missing | 原 ID 和 raw 保留；需实际原 operation 采集记录，不能空集验收 |
| sampler_stop_adapter_receipt_missing | 保留隔离；使用本地实际 producer 的原 collection 核验；远端能力或原 owner 证据缺失仍 blocked |
| cleanup_response_unknown | 原 Runner.cleanup 没有 request identity/lookup 接口；保留 unknown 意图/隔离，绝不重发。未来须有可信原清理查询/外部证据恢复合同，本批没有自动解锁接口 |
| safety_budget_or_policy_exhausted / safety_deadline_exhausted | 停止远程派发；原双层预算、原 ID、隔离和授权引用保留，不自动重开 lifecycle |
| safety_job_stopped_unknown | watchdog 按原最大费用结算；明确续办只重读/查询原身份 |

可信操作员可调用 `reconciliation.resume(session_ref, authorization_ref=exact_new_authorization_ref)`。不更换授权时可省略引用。该入口只恢复已 blocked 原会话，保存追加审计，保留轮次、原调用和原双预算；不能重发 unknown cleanup，不能补缺失原 ID，也不能延长 deadline。已关闭会话只返回原事实。解除的同目标 blocker/error 另存历史；其他目标 blocker 保留。

本地验证使用实际 Worker/TaskBackend/Lifecycle/RepairOutcome/OperationService/AgentBridge 与受控 Runner/provider，不证明 PostgreSQL 多进程崩溃、实际远端命令、真实计费或跨主机远端 sampler 停止能力。
