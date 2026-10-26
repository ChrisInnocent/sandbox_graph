# 端到端检查 R1–R3 修复（2026-10-24）

本轮落实用户“修复”要求，范围是 [端到端检查](e2e-review-20261024.md) 已复现的三个停止缺陷，以及入口文档的进度不一致。实现位于 `.worktrees/agent-lifecycle` / `feat/agent-lifecycle`，未提交、合并或推送；主工作区的其他改动保持原样。未启动审阅子 agent。

## 结果与实现

| 问题 | 修复 | 验证的实际行为 |
| --- | --- | --- |
| R1：drain 被当成 cancel | 停止控制区分处置策略；原请求/生命周期取较早期限；现场和 provider reconciler 在预留及 I/O 前重验；现场收尾禁止旧版 retry/recovery；激活回执冻结前任版本，处置及后继证明齐全后 version_drained → superseded | 原期限前只有 query；自然完成后关闭原子任务/资源；到期记录 deadline 并停止原操作；显式取消优先；缺失或错误后继证明保持等待 |
| R2：取消后仍可入队/预留 | request_role 在上下文构建前及实际入队事务重验 stop，同请求重放先读原件 | 构建前及构建中发生取消均无新 agent_team task/job、预算不变；已有同内容请求只返回原引用 |
| R3：父 scope 漏计子 provider | 从 envelope.lifecycle_id 查原 run、类型化 scope 和 work 组成员；父级只汇总/等待 | run/group 都计入原 work 的未知请求，排除其他 run 的同名 scope；父级不继承子 operation_refs、预算或处置授权 |

原安全查询/停止/清理授权与双层预算继续有效，不增加额度、延长期限或重发未知提交；不足时保持阻塞和隔离。历史原始 NodeResult、unknown repair outcome 均不改写。新增 `version_drained` 是受信服务的旧版本处置事件，不能替代候选门禁或晋级授权。

涉及实现：`services/agent_team.py`、`services/lifecycle_stop.py`、`services/revisions.py`、`services/lifecycle_reconciliation.py`、`runtime/lifecycle_reconciliation.py`、`domain/agent_lifecycle.py`。新增正式回归 `tests/orchestration/unit/test_stop_regressions.py`，不是仅运行临时探针。

## TDD 和门禁证据

先加8项用例：**7 failed、1 passed**。失败分别是两处停止后入队、两种父 scope 漏计、三种 drain 被写入 explicit_cancel；已有请求重放通过。入队/归属修复后，5项对应回归通过。再加入现场操作、父级不接管及新终结事件用例，出现预期失败后实现 drain 路径；原 provider 的 deadline 比 lifecycle 更早的断言继续失败，修正为读取实际原请求期限后通过，没有放宽断言。

实际执行的相关回归：

~~~bash
python -m pytest tests/orchestration/unit/test_stop_regressions.py \
  tests/orchestration/unit/test_lifecycle_stop.py \
  tests/orchestration/unit/test_lifecycle_reconciliation.py \
  tests/orchestration/unit/test_agent_team.py \
  tests/orchestration/unit/test_lifecycle_state.py -q --tb=short
~~~

该阶段 **819 passed，11.23秒**。随后补充停止预留/I/O 竞争、同图计划替换、后继证据缺失/错误及终结事件反例，专项最终 **25 passed，无跳过**；实际 Worker/TaskBackend 与应用服务连接受控 Runner/provider，无真实沙箱或模型网络调用。

最终命令与原始产物：

~~~bash
python -m pytest tests/orchestration/unit/test_stop_regressions.py -q --tb=short \
  --junitxml=artifacts/reviews/stop-fixes-20261024/targeted.xml
python -m pytest tests -q -rs --maxfail=5 -m 'not live and not postgres and not linux' \
  --junitxml=artifacts/reviews/stop-fixes-20261024/offline.xml
openspec validate --all --strict --no-interactive
git diff --check
~~~

离线全量结果：**2639 passed、20 skipped、110 subtests passed，366.84秒，退出码0**。20项跳过为18项 PostgreSQL、1项真实 Linux procfs、1项真实双环境组 E2E；双组验收函数仍有实现缺口，不能只归因于缺环境。专项 JUnit 对应25 passed，4.14秒。七份被修改源代码/测试在摘要记录后至全量结束均未改变。

OpenSpec 三个 change 严格校验通过；差异格式检查通过。`artifacts/reviews/stop-fixes-20261024/` 保存 targeted/offline 日志、JUnit 及七份被修改源代码/测试的 SHA-256，最终汇总写入 `summary.json`。

## 任务与交付边界

proposal、WF-39 spec、ALD-11、ATG-08、tasks、README、progress 和能力对照已同步。45.4-B/C 记部分完成，45.4 整项不勾选，主 change 仍 **160/209**。历史报告保留当时结果并链接本次修复，不覆盖旧证据。

仍未完成：所有 ledger 的统一归属/矛盾与 legacy 处理、停止调度扫描改造、迟到效果与处置版本完整接线、所有 scope 和持久化故障门禁；真实产品适配及双组 E2E、候选编码/可信 gate/晋级，以及主区集成。这些既有代码缺口和环境验收，不因本轮离线通过变成已交付。

缺前任绑定的历史 revision activation 不能被新逻辑猜测为有效后继证明，会明确等待；本轮没有执行历史数据迁移。真实 PostgreSQL、三产品沙箱、实际 provider 及隔离部署不在本轮通过声明内。
