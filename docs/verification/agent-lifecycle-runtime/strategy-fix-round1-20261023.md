# 规则与诊断选路：后验拒绝修复验证

2026-10-23，工作树 `.worktrees/agent-lifecycle`。44.5 的规则／诊断选路已通过限定范围规格和代码质量复审，tasks 已勾选；完整45.1仍待 N02 原始失败组合验收。

后续进展：N02组合已另行补齐并通过复审，见[独立记录](n02-loop-20261023.md)；本页仍只记录此前7文件修复快照。

修复独立审查发现的边界：A 执行成功但独立后验拒绝时，通过可信 `postcheck_rejected` 证据选择 B。证据绑定真实前一动作、后验、执行回执摘要、处置和 outcome，unknown 不构成换策略许可；原执行回执和共享预算保持不变。

真实 StateGraph/Worker 受控文件效果测试覆盖 A→B→依赖重验→源 N01 验收，含执行成功且 error=None 的 A、最后一次预算内 B 成功、其他组继续和原始数据不变。另有9种缺失／错配／未知后验证据反例。生产目录仍仅 N01/N07/N09，不以测试注入动作宣称产品新增能力。

最终受影响回归命令：

```bash
python -m pytest tests/orchestration/unit/test_repair_catalog.py tests/orchestration/unit/test_repair_strategy.py tests/orchestration/unit/test_repair_execution.py tests/orchestration/unit/test_lifecycle_models.py tests/orchestration/unit/test_lifecycle_dispatch.py tests/orchestration/unit/test_agent_team.py tests/orchestration/unit/test_recovery_plan.py tests/orchestration/unit/test_lifecycle_reconciliation.py tests/orchestration/graphs/test_work_remediation.py tests/orchestration/graphs/test_work_recovery.py tests/orchestration/graphs/test_work_strategy.py -q --tb=short
```

结果：438 passed，31.92秒，无 skips，退出码0。修复包含7份代码／测试／监督文档，提交复审前及复审时摘要一致。复审确认原缺陷已修复，未发现本次差异引入的重要问题，没有重复执行同一测试组合。

原始证据：[测试输出](strategy-fix-round1-20261023.log)、[7文件摘要](strategy-fix-round1-20261023-sourcehashes.json)。此前[1765项编排回归](strategy-interim-20261023.md)只证明修复前快照，不与本次438项相加。真实 PostgreSQL/provider/产品适配和候选隔离未由本报告验收。
