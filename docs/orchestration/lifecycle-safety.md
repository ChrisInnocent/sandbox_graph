# Lifecycle 安全边界（43.4）

`services/lifecycle_safety.py` 提供共享 work 执行互斥和 ResourceDispositionReceipt 的生产/复核。它依赖 Repository 同一事务中的 fence 和记录锁，不进行外部调用。服务核心与合成 MemoryRepository 测试不代表运行时已经接入，也不代表 PostgreSQL 并发或真实 adapter 已验证。

## 事务内 API

```python
LifecycleSafetyService.acquire_work_in(tx, scope, token, purpose='execution')
LifecycleSafetyService.assert_work_in(tx, scope, token)
LifecycleSafetyService.release_work_in(tx, scope, token)
ref = LifecycleSafetyService.produce_disposition_in(tx, scope, token, ttl=30)
receipt = LifecycleSafetyService.verify_disposition_in(tx, ref, scope, token)
```

实例方法 `acquire_work`、`assert_work`、`release_work`、`produce_disposition`、`verify_disposition` 自动打开事务。调用者必须持有真实 `LeaseToken`；owner 的 thread_id、整数 owner_epoch、lease_id 都必须与实际线程租约和共享 work 记录一致，客户端 token 的 lease_until 不作为有效性依据。

`work_execution_leases[scope]` 绑定 owner 三元组以及 scope、run_ref、plan_ref、graph_ref、contract_ref。不同线程即使各自有有效租约，也不能同时持有同一 work。work 状态更新引起的 revision 变化不丢失执行锁；改变 run/plan/graph/contract 身份则拒绝继续执行。原图、收尾、修复、恢复的所有写入点和外部副作用前置事务都应调用相同断言。

旧 owner 失效后可接管执行锁，但不会删除或代改原 operation 和资源。旧 owner 的预留冲突域强制转为 quarantine；原 operation_refs、未知 operation_refs 和 residual_resources 保留于 `work_safety_quarantines[scope]`。释放执行锁也不会删除资源预留；有未知 operation 或未收口资源时隔离后再交还执行权。

## 运行时事实生产合同

受信任运行时先落盘 N07/N09/N10 实际节点回执，再在最终 work 状态写入之后发布 `work_safety_facts[scope]`：

```json
{
  "schema_version": 1,
  "scope": "work/a",
  "run_ref": "run/a",
  "plan_ref": "plan/a",
  "graph_ref": "graph/a",
  "contract_ref": "contract/a",
  "work_revision": 7,
  "producer": "work_runtime",
  "producer_version": "1",
  "stopped": true,
  "admission_status": "closed",
  "sampler_status": "not_started",
  "attempt_closed": true,
  "activation_id": "activation/a",
  "attempt_receipt_ref": "receipt/N10",
  "receipt_refs": {
    "N07": "receipt/N07",
    "N09": "receipt/N09",
    "N10": "receipt/N10"
  }
}
```

`work_revision` 是 `work_instances` 记录的实际存储 revision。facts 身份、producer/version、schema_version 必须精确匹配。布尔量本身不能建立闭合事实：服务独立读取三份节点 receipt，核对 node/run/scope/plan/graph/activation，且结果 execution_status 必须已终结。N10 必须与 attempt_receipt_ref 相同。缺任何停止、采样、attempt 事实或实际回执时，不放行现场副作用。

采样从实际 `observations` 重新读取：任何 running 会话都会覆盖 facts 中的停止声明；未知状态保持 unknown。无实际 sampler 行时才使用受信任运行时的 stopped/not_started 记录。生产者必须基于真实停止/未启动结果发布这些状态，不能从取消 accepted 推导已停止。

## 凭证来源、时效与未知资源

生产过程读取本 scope 的 operations、resources、resource_leases、observations、work_safety_facts 及其实际 receipts。operation 只有 phase=terminal 且 status.authoritative=true、state 属于 completed/failed/cancelled 时，才确认效果已知。资源未 closed，或 operation 报告的 resource_refs 没有本 scope 的资源记录时，均保留 residual。未知效果先隔离冲突域，再对隔离后的账本生成凭证快照；不会换 operation ID 或分配修复 attempt。

`resource_disposition_receipts[ref]` 是不可变 envelope：schema_version、完整 identity、producer owner 三元组、snapshot_hash、严格 ResourceDispositionReceipt JSON。ref 为 `disposition:` 加 envelope 的 SHA-256。凭证内 producer=`lifecycle_safety`、producer_version=`1`；observed_at/expires_at 使用 Repository wall clock，ttl 必须是有限正数。

每次 `verify_disposition_in` 都在同一事务中再次确认线程 fence、共享执行 owner、scope、完整身份、producer、内容摘要和有效期，并重新读取完整账本快照。实际事实改变、换 owner、过期、未来观察时间或内容不一致均拒绝。默认 require_closed=true，还要求 stopped、admission closed、sampler stopped/not_started、无残留且无隔离域。现存 quarantine 必须通过原有授权对账/回收路径解除，不能靠新凭证覆盖。

不存在 resources_disposed 参数。生产调用返回 receipt_ref，调用者不能自带布尔值替代证据，也不能通过 require_closed=false 获得现场执行许可。

## 受限离线诊断

未知处置下可用 `offline_diagnostic_capabilities(ref, scope, token, evidence_refs=...)`。它仍复核当前凭证和 owner，但不要求现场已经闭合；每份证据须存在于受信任 `frozen_lifecycle_evidence`，绑定同 scope/run、frozen=true、非空 artifact_refs 和 SHA-256 content_hash。仅返回 `('read_frozen_evidence',)`，不包含停止、重建、清理、测量或其他现场能力。封存服务负责发布内容真实且不可变的证据记录。

## 验证边界

`tests/orchestration/unit/test_lifecycle_safety.py` 使用显式合成 MemoryRepository，覆盖两线程互斥、租约失效接管/隔离、旧 owner/伪造 identity 拒绝、时效、变更后证明失效、真实 sampler 覆盖、缺闭合事实、原 operation 保留、未知资源和受限封存证据读取。真实 PostgreSQL 双 worker 与故障恢复、外部操作 adapter、完整 runtime 接入另行验证；该模块通过不意味着任务 43.4 的集成验收完成。

## 原图接线与无资源域隔离

Driver 已对已登记 work 获取本共享锁、在新 attempt 开始使旧收尾事实失效，并基于实际 N07/N09/N10 和 observation ledger 生成事实。Worker 仅给同 activation 的已关闭事实同步完成元数据 revision。`repair_isolations` 即使没有资源域或 operation ID 也会使 closed proof 无效，并阻止 ordinary execution/recovery；离线诊断和受控清理/对账仍可获取 owner。隔离不能由模型自行删除，解除机制仍待安全对账接线。详见 [持久记录](lifecycle-persistence.md)。
