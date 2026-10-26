# Provider 取消测试定位修复（2026-10-24）

范围：45.4-A / ST-04 的未决失败。工作树为 `.worktrees/agent-lifecycle`。本次修正测试身份定位并增强反例，没有为了让测试通过而改变生产取消状态或关闭条件。

## 根因和修正

原复现命令：

~~~bash
python -m pytest tests/orchestration/unit/test_lifecycle_stop.py::test_cancel_waits_for_original_provider_children_without_resubmission -q --tb=short -o addopts=--import-mode=importlib
~~~

首次复现：`1 failed in 1.01s`。原断言读取 `tx.scan('lifecycle_stop_status')[0]`，得到 `closed`，预期 `waiting_dependency`。

`test_agent_team.prepared` 调用 `test_lifecycle_context.context_service`，它从 N02/N09 两个 receipt 创建两个 lifecycle。provider 绑定其中的 N02；无 provider 的另一目标可以完成停止。首行读取没有与测试的 provider/lifecycle 绑定，错误地把另一目标的状态用于断言。

现按 `request_id='cancel:provider-stop'` 与原 `record.key.lifecycle_id` 查找唯一 stop request，并在全过程使用其 stop_ref。精确定位后原测试通过，没有修改 waiting 预期。

新增 `remote_state=running/cancelled` 两种参数，均令 `children_stopped=False`。两种情况下分别验证：

- provider 取消 accepted 后，原 stop target 仍 waiting，remaining.providers 含原 request。
- 生命周期尚未 cancelled，原目标没有关闭证明。
- 原 provider 的终结回执确认 children_stopped 后，原生命周期 cancelled、stop target closed。
- 全程 submit/stop 各一次；原生命周期来源证据不变；没有 repair order 或 repair recovery 派发。

这些测试通过真实 Worker、RunService、持久内存账本与 LifecycleReconciliationHandler；provider 是受控边界，不能据此声明真实宿主 provider 已验收。

## 验证结果

~~~bash
python -m pytest \
  tests/orchestration/unit/test_lifecycle_stop.py \
  tests/orchestration/unit/test_lifecycle_reconciliation.py \
  tests/orchestration/unit/test_agent_team.py \
  tests/orchestration/unit/test_repair_execution.py \
  tests/orchestration/unit/test_revision_drain.py \
  tests/orchestration/unit/test_revision_rollback.py \
  tests/orchestration/unit/test_worker.py \
  -q --tb=short -o addopts=--import-mode=importlib \
  --junitxml=artifacts/gates/lifecycle-stop-20261024/related.xml
python scripts/check_measurement_generation.py
~~~

| 验证 | 实际结果 | 本地原始产物 |
|---|---|---|
| 取消/对账/团队/修复/版本/Worker 相关集合 | 187 passed，0 failures/errors/skips，9.59s | `artifacts/gates/lifecycle-stop-20261024/related.xml`、`summary.json` |
| 测量代际/T6 固定门禁 | 278 passed，0 skips，32.82s；Gate PASS | `artifacts/gates/measurement-generation/20261023T161611Z-8c9ef4e9/` 下 summary.json、junit.xml、pytest.log |

产物目录使用 UTC 时间，文档日期使用 Asia/Shanghai。两集合存在重叠，不相加为独立用例总数。

已验证的 `test_lifecycle_stop.py` SHA-256：`993864b7f9a7d9b084d0205655eba7fb6dc12bec9018b2921fdf92608c983813`。

本次已解决特定失败；45.4-A 的统一夹具、45.4 其余归属/事务/收口矩阵、完整 stop-local runner 和真实 PostgreSQL 验收仍按 tasks 记录，不能由本结果勾选整个45.4。
