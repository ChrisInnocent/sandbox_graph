# 有限负载、完成观察与日志证据

本轮实现 `sandbox_bench/load.py`、`completion.py`、`logs.py`；验证对象为测试器，不是实际沙箱性能。算法使用 Python 异步任务，测试使用独立 `tests/fixtures/benchmark/load_clock.py`，不通过真实 sleep 猜测时序。黄金值来自 BCT-02 手算表。

## 调度与持久边界

`await LoadEngine(clock=..., sleep=...).run(schedule, operation, journal=...)` 接受 `LoadSchedule` 与 `async operation(request_index) -> OperationResult`。operation 必须包含提交至所选完成终点的完整过程。单独返回 HTTP 201 不符合 STATE_READY 合同。

journal 是强制依赖，协议为异步 `reserve(index, child_weight)`、`submission_started(token)`、`record_terminal(token, result)`。reserve 必须原子持久消费 child 预算及一个 parent 额度，submission_started 必须完成 ownership/fencing 检查及提交意图持久化。调度器从不重试这些调用或外部 operation；失败不退回已分配预算。文件账本的崩溃/并发证据由 runner/journal 测试负责，本组脚本 journal 只验证调度边界。

WINDOW 即时补槽；BATCH 支持 B>C 的批内限流、余数批及尾批无 gap；OPEN_LOOP 接受有限非递减相对到达时间序列，明确 drop 或有限 FIFO queue。各模式都消费有限请求数，不补发至成功数 Q。所有 clone 方法的 child_weights 按 child 占用并发，K>C、child_budget 不足或 parent_budget 不足在调用前拒绝；不拆分请求。逐 child 提前释放通过可选 `progress_operation(index, child_terminal)` 接口完成：回调 `child_terminal(child_index, result, completed_at)` 要求 journal.record_child_terminal 持久成功，然后只释放该 child 的一个槽并唤醒调度。重复、重叠、越界 child 事件拒绝；父收尾只释放 remaining weight，不双重释放或重复计 unknown。旧 operation(index) 接口仍保持父调用终结时整体释放的兼容行为。

客户端超时释放占用，但保留 `unresolved_external`。unknown/timeout 未携带数量时保守计完整 child 权重；达到 unknown_limit 停止新提交。上限 0 表示首个 unknown 即停止，正常请求仍能运行。已在途请求可能随后增加未知数，不能把该上限误解为远端绝对并发上限。全局 deadline 停止新增，已运行调用按有限 operation_timeout drain。终态落盘另有同长截止，失败立即停止新增；operation 和 journal 实现须支持协作取消，底层同步 I/O 须提供自身 deadline。

事件包括 scheduled、admitted、submit、queued、dropped、child_completed、completed、gap、stop；child_completed 带 child_index。`active_slots` 是保守保留的准入槽（含待持久化终态）；`completed_at` 是到达所选终点的客户端时间，`completed` 事件本身的 `at` 是持久化处理后的记录时刻。计算服务完成分布应使用 `completed_at`，不能使用落盘时刻。`refill_gap` 包含前次完成至新提交的准入/持久化开销；generator_lag 保存计划至实际提交延迟。OPEN_LOOP 的 `target_rate_achieved` 使用严格零延迟条件，未配置容差时不声称实际目标率达成；该字段是发生器诊断，不能直接归因于产品。

## 完成观察

`CompletionObserver(process_epoch=..., clock=..., sleep=...).observe(spec, submitted_at=..., process_epoch=..., accepted=..., inspect=..., execute=..., server_terminal=...)` 支持 API_COMPLETE、STATE_READY、EXEC_COMPLETE、SERVER_TERMINAL。inspect 必须是只读 API；接口本身不能证明调用方没有传入会 auto-resume 的函数。

状态观察从首次只读 poll 开始，同一 cadence 复用所有产品；unknown 不映射为 running，失败读取保留未知状态。轮询/执行等待受从提交开始计算的 deadline 约束，截止时刻不作为成功。成功客户端 latency 只在同一 epoch 计算，失败/超时/跨 epoch 为 null；服务端 duration 与采集到达时间分列。`validate()` 是独立有限后验，不改变不可变 CompletionResult。`record_late()` 生成独立迟到事实，不回填成功分位数。新 ScenarioExecutor 已显式接入本观察器；旧 measure 路径迁移由主代理负责。submitted_epoch 与观察 process_epoch 分列，跨进程不拼接单调时钟时延。

## Kuasar 日志

TerminalLogParser v1 支持源代码 `orchestrator/internal/orch/orch.go::runLaunch` 发出的 JSON slog 和文本 slog。匹配完整消息及 sid/run_id/kind/profile；JSON duration 只接受整数 ns，文本接受有明确 Go 单位的精确 duration。保留 raw_record/hash、parser_version、服务端 time 和 observed_at。

只有成功记录且存在匹配同一操作、非零、带时区和 evidence_ref 的 AcceptedAtEvidence 才给内部 duration。源代码在缺 AcceptedAt 时会用当前时间替代，因此仅见正 duration 不足以证明有效起点。重复记录返回 duplicate/null，冲突重复使 parser.records 中该语义记录永久 invalid；消费者应以最终 records 集合封存统计，不能在未结束去重前累计不可撤回的成功值。该证据类型是来源引用，真实可信度仍由采集和环境证明承担。没有为 Cube/AgentENV 伪造同类内部字段。

## 验证与任务边界

本组最终专项命令为 `.venv/bin/python -m pytest -q tests/benchmark/scenarios/test_load.py tests/benchmark/scenarios/test_completion.py tests/benchmark/unit/test_logs.py tests/benchmark/scenarios/test_scenarios.py tests/benchmark/scenarios/test_scenario_density.py tests/benchmark/unit/test_contract_models.py tests/benchmark/unit/test_wiring.py tests/benchmark/unit/test_fixture_lifecycle.py tests/benchmark/unit/test_source_wiring.py`。2026-10-21 最后一次结果为 **129 passed、25 subtests passed（17.85s）**；另含 Session/B08 records 的六文件集成为 **91 passed、6 subtests passed（10.13s）**。全仓最终结果由主代理统一验证，历史中间运行不作为最终验收证据。

覆盖精确 WINDOW `[0,0,1,3,3]`、BATCH `[0,0,5,5,9]` 及 wall=10、B>C/失败预算、drop/queue、未知上限、原子权重拒绝、提交许可拒绝、迟到/epoch/慢 poll、四种完成终点、日志关联和冲突反例。16.4、18.2、18.3 可由本组离线证据勾选；16.3 的旧路径替换及 18.1 的 BTC-019 三切点强杀恢复已有主代理实现和测试，整条勾选由其统一核对。未运行真实产品性能验收。

逐 child 新增验证：C=3、两个 K=2 父请求分别于 t=0/t=1 提交，父0至 t=3 才终结；最大占用仍为3。持久 child receipt 延迟会推迟补槽；重复 child 被拒绝；unknown child 立即停止新增且父收尾不重复计数；BATCH 在批内允许逐child补槽，但批间 gap 仍等待整批父请求终结。
