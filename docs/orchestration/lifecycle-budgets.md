# 生命周期与 lineage 配额

`LifecycleBudgetService` 提供持久预算账本，不执行模型、现场动作或任务派发。调用方把预算预留与生命周期变化、order、job、outbox 放在同一个已完成 fence/授权校验的 Repository 事务中；完成结果和结算也应一起提交。`*_in(tx, ...)` 都是静态方法，不开启嵌套事务；实例的 `initialize/reserve/settle/adjust/snapshot` 是独立事务包装。

## 明确配置与身份

普通额度必须完整提供七项正整数：`repair_attempts`、`diagnosis_calls`、`candidate_revisions`、`tokens`、`child_jobs`、`observations`、`cost`。`diagnosis_calls` 计模型请求，观察原 operation 消耗 `observations`，不能再算一次修复。不同维度独立累计；一个请求可同时消耗候选和子 job 等多个维度。`cost` 是部署合同明确配置的最小费用单位整数，`cost_unit` 指明单位，例如测试夹具的 `microUSD`；服务不从 token 数推算价格，也不为缺少金额合同的旧 policy 生成货币上限。

安全额度完整提供 `stop_calls`、`query_calls`、`cleanup_calls`、`cost`，另有有限绝对 `safety_deadline`，不得早于普通 `deadline`。普通额度不足或普通期限到达后，可以在安全期限内申请停止、查询和清理；安全额度本身耗尽后拒绝新申请。该服务报告拒绝，生命周期服务负责隔离证明、保留未知效果以及进入 blocked。

`initialize_in(tx, lifecycle_id=..., lineage_budget_ref=..., limits=..., deadline=..., safety_limits=..., safety_deadline=..., cost_unit=...)` 建立双层账本。首次创建 lineage 时可另行明确提供 `lineage_limits`、`lineage_deadline`、`lineage_safety_limits`、`lineage_safety_deadline`；省略时首次使用本 lifecycle 的配置。此后省略表示继承现有 lineage，显式提供则断言其**初始**配置相等。已存在 lifecycle 只能使用原配置和原 lineage 重放初始化，否则拒绝。

新 lifecycle 可以配置不同的局部额度或更晚期限，但其任何申请仍必须同时满足原 lineage 的剩余额度和原期限。context hash、模型、policy、候选 revision 和后继 run 都不是预算主键，不能刷新共享额度。后继生命周期必须由可信生命周期服务绑定原 `lineage_budget_ref`，不能接受模型自报的新 lineage 作为重置入口。

## 原子预留与结算

```python
with repository.transaction() as tx:
    tx.assert_fence(token)
    # 先验证当前 work、版本、动作能力、授权和 dispatch gate。
    reservation = LifecycleBudgetService.reserve_in(
        tx, request_id=request_id, lifecycle_id=lifecycle_id,
        input_ref=input_manifest_ref,
        amounts={'repair_attempts': 1, 'child_jobs': 1},
        metadata={'run_ref': run_ref, 'policy_ref': policy_ref},
    )
    # 同一 tx 保存 order、job、outbox、请求回执。
```

预留先校验两层所有维度和期限，再写任何一层；外层事务异常会一起回滚。当前 Repository 在 PostgreSQL 使用事务级 advisory lock，并对读取账本加行锁；MemoryRepository 用互斥锁模拟原子性。服务不依赖先在另一事务读取 attempts 的结果。达到额度的最后一次申请仍可执行和成功，只拒绝下一次新申请。

模型调用必须在发起前同时预留 `diagnosis_calls`、最大 `tokens` 和最大 `cost`，并提供 `provider_enforces_maximum=True`。该字段是受信集成层校验 provider capability 后的断言，不是模型输出或未验证的公网请求字段。输入/输出上限、实际计价和 provider 的硬性执行由适配器负责；没有可执行上限的 provider 不得进入自动循环。应用内模型网关也须调用这条路径，独立账本存在并不等于已接通所有 provider。

`reserve_in` 将 `request_id`、lifecycle、amounts、输入引用、metadata、安全类别与 provider 上限断言绑定内容摘要。相同 request ID 和相同内容返回原 reservation，不另扣额度；同 ID 异内容抛出 `Conflict`。换新 context/model/policy/run/revision 后可以使用新请求身份，但继续扣相同 lifecycle/lineage 的余额。reservation 重放只返回已有事实，不授予重新提交外部效果的权限；provider 的响应丢失必须继续按原 request ID/operation 对账。

`settle_in(tx, request_id=..., actual_tokens=..., actual_cost=...)` 对两层同时释放预留并写入实际消耗。缺失的 usage 维度按对应最大预留全部消耗，`usage_status='unknown'`；已知维度只退未使用差额。实际值超出可强制最大值拒绝结算并保留预留，要求适配器/处置层处理合同违例。请求、动作、候选、job 和观察次数一直计入消耗，不随 usage 退款。只有受信服务已验证“未提交”证明后才可传 `proven_not_submitted=True` 释放 token/费用；此时请求次数仍保留。结算允许在期限到达后保存事实，重复同结算返回原回执，异内容拒绝。

安全请求必须传 `safety=True`，`operation_kind` 只能为 `stop/query/cleanup`，amounts 只能含其对应的 `stop_calls/query_calls/cleanup_calls` 和可选费用。不能用安全额度创建诊断、候选、修复、子 job 或测量。安全结算只更新安全账本。

## 授权追加与审计

受信授权服务建立不可变 `budget_authorizations`，键为授权引用，body 必须明确包含：

```python
{
    'target_kind': 'lifecycle',  # 或 lineage
    'target_ref': 'life:123',
    'additions': {'repair_attempts': 1},
    'cost_unit': 'microUSD',
    'expires_at': 1900000000.0,
    'revoked': False,
    'safety': False,
}
```

`adjust_in(tx, adjustment_id=..., target_kind=..., target_ref=..., additions=..., authorization_ref=..., safety=False)` 核验精确目标、增量、单位、有效期和安全类别。每个授权只能使用一次；同 adjustment ID 同内容重放返回原回执，异内容拒绝。增量必须为正整数，不能把布尔值当成 1。追加保存 `budget_adjustments` 和 `budget_authorization_uses`，只增加当前上限，不清零消耗、不更改初始配置或期限。两层额度需分别获得授权；局部追加不会自动扩大 lineage。过期预算不能仅靠增加次数重新开放，需要上层明确的后续运行/处置授权设计。

`snapshot_in(tx, lifecycle_id)` 返回两层身份、`original` 初始合同、当前 limits、reserved、spent/consumed，以及独立安全账目。初始合同、reservation、settlement、adjustment 和授权使用记录可分别追溯。不能直接改写预算行实现“恢复默认值”。

## 验证边界

`tests/orchestration/unit/test_lifecycle_budgets.py` 使用现有 MemoryRepository，验证 ALU-04/05 对预算服务的局部要求，包括最后一次成功、跨配置身份累计、未知 usage、不同 lifecycle 共用 lineage 的并发竞争、事务回滚、授权追加和独立安全 envelope。它不代表真实 PostgreSQL、实际模型计费或 ALI-03 的端到端派发集成已经验收。provider 调用、动作派发、结果提交与此账本的事务接入必须另行验证。
