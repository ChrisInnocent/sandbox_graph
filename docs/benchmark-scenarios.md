# 基础场景与资源 cohort 执行入口

本轮 `ScenarioExecutor` 与 `DensityController` 实际执行有限阶段和观察流程；它们不把源码审计、手工 fixture 或受控服务通过升级为真实产品能力。默认 adapter 仍需所在环境的版本、规格、placement、权限和实际协议验证。

## 场景执行

`ScenarioExecutor(case, adapter, on_owned=async_callback, ...)` 提供 `setup(targets=..., fixture=...)`、无 I/O 的 `preflight(index)`、`execute(index)`、批后 `validate()` 和有限 `cleanup()`。execute 只能在 runner 的持久请求许可下调用。setup 不偷偷创建准备实例；外部目标需要 fixture evidence_ref、匹配 environment_ref 与唯一 sandbox_id，准备动作必须由独立预算和 receipt 管理。fixture 引用是追溯输入，不等于本模块已经核验其真实性。

| 方法 | 本轮执行路径 | 关键边界 |
| --- | --- | --- |
| B01.TEMPLATE / SNAPSHOT | create → 所选 CompletionObserver 终点 | STATE_READY 不隐式 exec；创建身份先交所有权 sink |
| B02.FULL | build 注册/触发/ready → 使用持久 source_id create → 完成 | 每个 request 独立 FULL，构建计入总时延；builder callback 带 request_index；不改共享 adapter 的 source |
| B02.PREPARED | 从已准备来源 create → 完成 | 来源准备证据由输入 fixture/environment 提供 |
| B03.PAUSE | pause → 只读 paused 观察 | STATE_READY 的目标是 paused；API_COMPLETE 仅代表所声明的 API 边界 |
| B04.MEMORY / DISK | resume → running → 独立内存/磁盘挑战 | 缺 challenge_ref 或 fixture_validator 在副作用前阻塞；挑战失败不抹掉状态完成时延 |
| B05.NATIVE | 一次 fork → 逐 child 结果/完成 → 必要源恢复 | 部分失败保留；不补成功；Kuasar native profile 明确 unsupported |
| B05.FULL | capture/export → K 个持久 child_create 阶段 → 必要源恢复 | 源恢复属于完整父耗时；child 时延从自身提交算起 |
| B05.PREPARED | 已核验 snapshot → K 个持久 child_create 阶段 | 不重复 capture；不把临时注册 ID 当持久 source |
| B06.RUNNING / PAUSED | 一次 DELETE → 只读不存在 | 仅404可证明该观察的逻辑不存在；401/网络错误不作不存在；物理资源释放仍 unverified |
| B07.FIRST / STEADY / OUTPUT / ERROR | 完整执行流 → exit/output 断言 | FIRST 以持久公共 authority 按 environment/sandbox/generation 原子消费；输出 byte count/hash 与 exit 记录；预期非零退出单列，不计成功命令吞吐 |
| B08.LOCAL / PUBLISHED | capture/export → 本地 API 或明确发布观察 → 独立恢复断言 | 需要实际触页证据；PUBLISHED 不用 ACK 代替 published 状态；restore 后验不回填完成时间 |

同步 adapter 放入 `asyncio.to_thread`，其 I/O 必须自行具备有限 deadline；取消 Python 等待不证明远端副作用已停止。异常响应不自动重试，未知结果交 runner 对账。`on_owned` 对外只交身份与环境信息，不持久化 handle.private 中的 token；执行器内存保留原 handle 供后续执行。cleanup 只删除本执行器创建的 sandbox，外部准备目标不会被顺带删除；source 清理保留 residual，等待引用计数与共享关系检查，不声称物理清理完成。

`stage_journal(event)` 是 B05 扩展阶段的强制异步协议。event 包含 request_index、stage、child_index 及 phase=intent/receipt；intent 回调必须在返回前完成持久 fencing、授权及阶段 HTTP 预算消费，receipt 保存返回身份。它不能只是无约束日志函数。阶段一旦开始不重放；后续 child 丢失响应时，已经完成的 sibling 及其身份保留，未提交 child 标 unsent，未知 child 单独计数。跨进程重入要由同一持久 key 拒绝或进入只读对账。builder 的同步 on_progress 同样要求持久处理，不提供空回调默认许可。

FIRST 必须注入 `first_command_reserver(environment_ref, sandbox_id, generation)`；缺 authority 或 generation 在 command 前阻塞。回调失败不发命令且不自动重试，跨 work/执行器共用 authority 才能保证真实 FIRST。资格持久化时间不计入命令服务时延。

源恢复在扩展及 native clone 的 finally 中执行，包括 capture/fork/child 异常和父任务取消；`source_resume` intent/receipt 带 `compensation=True`，恢复使用独立 cleanup_timeout 窗口，最终状态另以 observed 阶段持久化（持久化也有独立有限截止）。恢复失败不会覆盖 child 成功记录；返回 source_recovery 和 source_residual，取消后仍可查阶段账本。此补偿不授权重发未知 child 或 capture。

后验与性能分列。batch_after 在 `validate()` 中执行；stream_after、恢复挑战和 clone 隔离验证标 `validation_interference`。父/child 完成字段在验证之前冻结；LoadEngine 的 callback 返回与保留槽释放可晚于该时间，统计须使用明确的 completion.observed_at。没有 clone fixture_validator 时，native API/状态成功不代表私有内存/文件隔离已证明。`execute_with_progress(index, sink)` 为每次父调用绑定独立 sink，native 与扩展 clone 都在 child 完成后、后验前通知 LoadEngine；child 持久终态写入后即可提前补槽。

## B09 / B10 控制器

`DensityController(case, cohort_inspector=..., resource_snapshot=..., pause_cohort=...).run(executor.create_member, journal=...)` 直接执行专门工作流。cohort inspector 返回不可变 `CohortSnapshot(ids, ready_ids, node, environment_ref, scope_ref, complete, paused_ids=())`。resource provider 返回有 evidence_ref/domain 的原始观测，可附 MemAvailable samples、既有采样窗口与比较指标。这些 provider 是真实采集接入点；模块不生成资源数字、不将缺失 PSS/CPU 填零，也不声称一次 snapshot 已满足稳态窗口。

- GROWTH_LIST 使用列表存量净变化，保留新/丢失 ID；GROWTH_READY 使用新增健康 ID 集合，并另记 ready_net_growth 和 lost_ready_ids。两者分别执行自己的停止规则，不从同一轨迹冒充两轮测量。增长恢复会清零连续 stall。LIST 原版的 created_any 依据曾有 listed；健康从未成立时 normalized_stop_reason 单列 setup_failure。
- 每批共享原始有限总请求预算，OffsetJournal 保留全局 request_index；max_batches、max_instances、global deadline、未知上限均停止新增。停止原因是预算/观测结果，不称最大物理容量。每批结束保留真实 cohort 和资源观测。
- FIXED_REQUESTS 运行明确 WINDOW/BATCH，OPEN_LOOP 适用配置也不会静默换闭环。创建完成数与最终存活 IDs 分开。旧实例退出会减少最终 cohort，不减少历史完成数。
- PROJECTION 先拒绝非零实例基线，按新建 ID 与最终 ready 的交集确定 healthy 分母；统一模型调用共享 metrics.capacity 函数。部分健康、内存域变化和缺输入不能产生有效容量估计。兼容 awk 视图必须由 provider 提供已锁定的真实参考证据，否则 unavailable。
- SLO_VERIFY 只读取已有 cohort，不创建实例。每个指标需带实际 value、比较方向（<= 或 >=）和 evidence_ref；缺任一项为 not_evaluated。创建吞吐为 not_applicable。
- B09.PLATFORM 在零测试实例条件下采资源窗口；IDLE/PAUSED 先采平台基线，再有限准备 cohort 并观察。PAUSED 还要求独立持久授权的 pause_cohort 回调及最终 paused_ids 证明；API ACK 或无响应不充当暂停。资源卸载是否可记零由真实采集证据判断。

## 验证和剩余接入

2026-10-21 本组九个专项文件共 **129 passed、25 subtests passed（17.85s）**，完整命令见 benchmark-load.md。另含 Session/B08 records 的六文件集成为 **91 passed、6 subtests passed**。测试使用独立 ManualClock 与脚本化服务事件，包含原生 partial fork、扩展/native fork 响应丢失与取消后的源恢复、父/child 计时分离、FIRST 公共资格、原始输出字节哈希、AgentENV codec 证明、发布 ACK 未完成、B08 独立恢复预算与不重发、source pin/归属/清理、新旧 seal 冲突、401 删除、净增长、ready 替换、stall、投影 18/14、零基线、SLO 无创建和 IDLE/PAUSED 序列。验证的是实现可执行边界，不是真实候选产品。

配置式 guest proof、publication collector、独立恢复预算、完整动态 inventory、PSS/CPU 和本进程 N06 稳态窗口已接入，实际命令和输入结构见 benchmark-wiring.md。真实环境仍需提供 source/规格/placement、触页 baseline、guest socket、collector 和 Linux 访问权限。B02 共享 build K profile、远端命令任务未知对账以及 snapshot/image 来源删除仍是实现缺口，不归因于缺少部署。B05 逐 child 提前释放已有独立时序与 FileJournal 集成测试。E01–E06 继续要求专门已选择 workload 和补偿协议，本模块不会默认施加外部负载/故障。

## 任务状态与证据归属

- 16.4、18.2、18.3：本组离线算法、黄金时序及反例可勾选。16.3 四终点与独立后验已实现，旧路径替换由主代理验证；18.1 WINDOW、权重预算、逐 child 提前释放及 FileJournal 已测，BTC-019 三切点强杀/复入测试由主代理实现并通过。16.3/18.1 可结合其证据整条验收。
- 19.2：独立 FULL/PREPARED 已实现，共享 build K profile 未完成，不建议全项勾选。19.3 核心状态流程与挑战接口完成，真实进程连续性证明仍需环境证据。19.4 只有逻辑删除与归属保护，物理释放未验收。19.5 FIRST 公共资格及原始输出 bytes/hash 断言已有测试，远端命令任务身份/超时对账仍未完整提供。
- 20.1：三种 clone、阶段持久化、部分结果、源补偿、逐 child 槽释放及单宿主共享引用清理接线已实现；snapshot/image 删除仍缺已审计适配，不建议全条勾选。20.2：捕获/发布观察、独立恢复预算/intent/receipt、已知资源清理及未知隔离已接线；产物大小、完整来源副作用与物理清理仍需补齐。
- 21.1：基线/cohort/暂停序列及资源 provider 接口完成，实际资源服务窗口由采集组验收。21.2：独立增长状态机、集合差集、旧实例损失、不完整列表、stall 重置及兼容偏差已测试，跨组 BTC-057 需主代理核对。21.3 共享公式集成已测，公式/awk 证据属统计组。21.4 固定请求与 SLO 算法已测，完整三产品矩阵不在本组证据范围。

以上没有把真实候选产品性能、数据库 fencing 或物理清理标为已验收；本组不修改 tasks.md。

## 创建成功率与成功创建数量（RT-36）

创建场景（B01/B02）单机请求密度固定，必须统计：**发起 / 接纳 / 所选终点完成** 三类计数与速率，分列不得混同。

| 字段 | 含义 | 来源 |
|---|---|---|
| `quality.n_attempts` | 本轮发起的请求总数（分母） | `metrics/quality.py` |
| `quality.api_success_count` / `accepted_rate` | **接纳**成功数（API 已受理，非完成） | 行 `api_status='accepted'` |
| `quality.completed_success_count` / `completed_success_rate` | **完成**成功数（到达所选终点） | 行 `status='success'` |
| `quality.unknown_count` / `failure_statuses` / `failure_reasons` | 未知与失败分类 | 同上 |
| `quality.final_cohort_count` | **实际存活实例数**（密度/cohort 场景；与累计创建数分开） | 需要 cohort 合同 |
| `throughput.count` / `buckets[].rate_per_s` | 整轮成功创建数 + **1 秒窗口成功创建速率曲线** | `metrics/throughput.py` |
| `throughput.late_operations` / `outside_operations` | 迟到完成 / 窗口外完成，不回填分位 | 同上 |

边界：失败消耗请求预算、**不补发到成功数**；`api_status='rejected'`（`reason=api_rejected`）表示 API 接纳前被拒；`not_sent`（如资源观测不可用）不计入发起成功；缺失一律 `unavailable`，不填零。
