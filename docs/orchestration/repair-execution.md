# 现代修复任务执行接线

状态：`locally_verified`（MemoryRepository 定向单元测试）。这是 43.5 / 45.1 的子交付，不代表整个恢复闭环或生产适配验收完成。

`NodeRemediationService.request_remediation` 创建的 `repair_orders` 通过 `payload.repair_order_ref/lifecycle_id` 进入 `RemedyJobHandler` 的独立现代分支。历史 `remedy_ref` 不转换为现代授权。

现代 worker 领取真实 TaskBackend claim，另取得真实 thread token，接管 lifecycle，并持有统一 `work_execution_leases`。job epoch 与 thread epoch 独立核验；动作前、NodeExecutor 收据落盘前和 outcome 提交前，均核验当前 job/thread/lifecycle/work owner、冻结 work revision、授权 revision/权限/有效期、当前 dispatch gate 和 generation、冻结输入及前序 result_refs。接管者重新调用 LifecycleSafetyService 从当前账本产生并绑定处置证明；旧 owner 的 proof 不能充当当前许可。

实际动作经过版本化 RepairActionCatalog 的固定 NodeExecutorRoute。输入清单的 input_refs 与原 source 的 result_refs 原样透传；原失败 receipt 不改写。允许的最后一次已预留尝试可以通过后验，不要求剩余一次额外额度。失败按生命周期和 lineage 的剩余额度、策略上限及同失败计数选择 diagnosing、有限 backoff 或 blocked。申请后新增的无进展事实在执行前再次检查。

独立后验读取可信账本，不消费 `result.output` 中自报的证明：

- N01：当前 scope/run 的原 operation inventory，真实 terminal 且 authoritative 的 RunnerStatus，以及正确归属的 NodeReview；新建 replacement operation 不能冒充原 operation。
- N07：属于该 work/run 的 operation、OperationService 写入的 collection、完整 runner_events 序列及精确 artifact 集；缺 collection、缺事件、interrupted、错 scope 或错 artifact 均拒绝。
- N09：当前 owner 从真实安全账本重建的关闭处置证明，已停采/关闭 admission、无残留/隔离，以及与结果精确相等的 operation/resource inventory。

`repair_executions` 的 submission_started 标记与 lifecycle claimed 事件在同一事务提交。标记后缺失精确 NodeExecutor receipt 时不再执行动作；若已有 receipt，则只重读原收据并重验后验。未知结果保留真实原 operation refs、隔离事实和 reconciliation-required outbox。完全缺 operation binding 时保持 remediating，记录 operation_binding_missing，禁止伪造 operation 或 owner_lost 事件。现代 worker 拒绝已有 repair_isolations 的 work 上新的现场动作。

`repair_outcomes`、预算 settlement、后验对应 lifecycle transition 与后继 outbox 在同一事务提交。提交失败后可重读原 receipt，不重做动作；已提交 outcome 可幂等返回。未知 usage 按原预留计费规则结算。worker 最终取消本地子任务，并只释放自己仍持有的 work/thread lease。外层 Worker 的 job_seconds 先到期时，即使其已把同一 job 记为 TimeoutError 失败，仍存活且未被替换的 job/thread/lifecycle/work owner 可以在取消后提交仅含未知事实的 outcome/settlement/隔离/对账 intent；这条通道不能执行动作或发布成功，其他终态或旧 owner 均拒绝。

成功只进入 reentering 并保存 `repair_recovery_required`，不按节点编号派发下一节点。消费端必须编译及验证 RecoveryPlan，再安排原图重新进入。

## 子交付及验证边界

原修复执行子交付只产生以下持久 intent，其自身不提供消费 scheduler：`repair_recovery_required`、`repair_retry_required`、`repair_reconciliation_required`、`repair_blocked`。通用 Worker.pump_outbox 当前若没有另行注册消费者，会将这些 intent 记录为 unregistered，保留未投递；需明确接入后续的恢复/诊断/定时/安全对账服务；可选安全消费的当前能力与未完成项见下文。

现代 worker 本身不持有 RunnerGateway，不调用远端 reconcile。后续新增的可选 [原 ID 有限安全消费者](lifecycle-reconciliation.md) 通过既有 OperationService、独立安全授权和原双预算查询/停止/清理原操作，禁止 replacement。没有 operation binding 或真实收尾证据时继续 bounded blocker。它以不可变 resolution 表达本 order 的已验证关闭，不删除 repair_isolations 或改写 unknown outcome；公共 Driver/安全 admission 统一读取经过验证的 resolution。存在 observation 但缺真实远端 sampler 停止合同仍保留隔离。

后验信任应用注册的 NodeService factory、OperationService 与 work_runtime 安全事实生产者。操作账本中的 intent/submission_started 可以尚无 status，这类原 operation 直接保守进入未知效果的原子 outcome/settlement/隔离/对账路径，不要求虚构远端状态。这里验证这些生产者的持久事实及归属，没有新增 runner 的签名/远端来源认证，也没有将正常 NodeService factory 改成逐方法的 capability facade。N07 核对收集清单和序列，未重新读取每个外部 artifact 的字节及内容摘要。N09 依赖已有完整 attempt N07/N09/N10 收尾事实，不会凭单次 cleanup exit=0 推导完整 attempt 已关闭；当前 request 路径要求现场处置已闭合，因此有残留或未知效果的现场先走安全处置而非普通 repair。

MemoryRepository 测试不证明 PostgreSQL 并发、子进程硬杀、真实产品 adapter 或部署环境安全；ALI-04/G3 与真实 RecoveryPlan/StateGraph 闭环仍须独立验收。
