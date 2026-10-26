# 停止、迟到事实与归档实施记录（2026-10-24）

本轮按既有 ALD-11 / ATG-08 继续实现；未使用 Superpowers，也未委派审计。

已实现：

- `LifecycleInventory` 对 operation、job、provider、resource、observation、资源租约、工作租约、repair/candidate isolation 做统一 typed scope 归属；冲突身份阻塞，不获得清理权限。停止和归档均调用此清单。
- PostgreSQL UnitOfWork 的所有相关效果 writer 在原事务内推进 disposition revision；旧停止证明和 archive 保留，当前状态失效并挂起原目标。内存测试事务使用同一钩子，仍不算数据库验收。
- 新生命周期在写入时挂接原停止命令；失败消息晚于取消到达时，记录失败生命周期并接入停止，不派发诊断。迁移历史命令采用有界显式批次。
- outbox 只路由停止任务。TaskBackend/Worker 运行有截止时间和退避上限的 stop coordinator；只有不可变原目标绑定正确的任务能够跨派发截止运行，也只有这些协调任务被效果清单排除。
- 实际 source thread 租约保护控制转移和证明提交；终态生命周期也需要新租约。证明、阶段及 closed 投影同事务；归档重验当前处置版本、清单和持久生产者记录。
- 封住新 generation、新 recovery，以及仅预留 permit 尚未提交的旧版本请求入口；已开始操作继续原 ID 对账。
- `StopCommand/StopTarget/StopStatus/StopProof` 为版本化合同；命令冻结截止 generation 与处置版本。StopStatus 为兼容投影，历史记录不静默冒充新证明。

固定本地门禁 `python scripts/check_lifecycle_stop.py --profile stop-local` 已通过 **514 个登记 nodeid**，收集集合与预期一致、setup/call/teardown 全部成功，JUnit 及执行前后输入摘要匹配。证据：`artifacts/gates/stop-local/20261023T181118Z-5234063c/`。UTC 时间与本地 2026-10-24 对应。此脚本只发布本地开发证据，不签发生产晋级 GateManifest。

真实 StateGraph 停止测试覆盖 N01/N04/N05/N07/N09 故障、原 N07→N09→N10 顺序、失败后停止、迟到生命周期和无新修复。既有 provider/drain/多 scope 反例继续纳入固定门禁。

持久门禁登记 **14 个用例**：4 个 PostgreSQL 双连接提交顺序竞态，以及 10 个独立进程硬杀切点（命令、入队/ACK、证明事务、外部受理后丢回执、迟到事实）。代码已提供；本机未提供 `SANDBOX_GRAPH_TEST_POSTGRES_DSN`，执行 `stop-persistent` 返回退出码 2 / blocked，证据 `artifacts/gates/stop-persistent/20261023T181351Z-d4283957/`。不将未运行切点记为通过。

一次中间全回归为 2640 passed / 4 failed / 20 skipped；4 个失败来自历史 fixture 将外部 run 绑定到本 run 唯一 work。已保留冲突负例，并改用可合法复用的 group label 验证外部任务排除；相关 99 项及最终上述 514 项通过。完整回归将在其余实现收束后重新运行，不能把中间结果记为全绿。

任务 45.4 整项仍保留未勾选：完整 ST-16 多版本共享现场组合、全部停止边界映射与真实 PG 验收还需收束。后续非 work 修复、候选编码、生产可信门禁、晋级和产品环境 adapter 不因本门禁通过而自动变为完成。
