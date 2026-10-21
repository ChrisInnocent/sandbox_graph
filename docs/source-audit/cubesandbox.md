# CubeSandbox 源码审计与统一测试适配

审计对象：`CubeSandbox`，HEAD `b25e416e990e6ba0641b4609974c53c61ee568f0`；审计时 `git status --short` 为空，origin 为 `https://github.com/TencentCloud/CubeSandbox.git`。已读仓库 `AGENTS.md`。本次只审查本地源码，未运行远端测试、未读取凭据、未修改上游源码；下文不是实际性能结果。行号对应上述 HEAD。

## 创建完成语义

2026-10-21 设计修正：用户要求创建可以按状态／完成事件计时，命令验证可在停表后或独立用例执行。下文“主指标到 exec”是早期建议，不是当前强制合同；最新定义见 [场景节点设计](../../openspec/changes/sandbox-evaluation-workflow/test-nodes.md)。源码事实及原官方脚本计时边界仍然保留。

节点级分工：R1/T0 的版本、引用和字段核验可脚本化，probe／init／状态、克隆副作用等新语义仍需模型审计；E1/E2/C1–C3 的角色探测、已定方案配置和报告脚本化；N00–N10 的请求、状态轮询、列表净增长、资源 PSS/CPU、每秒成功创建吞吐、断言与清理目标脚本化。原 `env_reset` 不可直接复用为本次资源清理，原时延及列表节奏必须带方法标签。缺适配、净底噪减项或预算未定保持待处理，不能由模型代测。详见 [逐节点职责表](../../openspec/changes/sandbox-evaluation-workflow/node-automation.md) 和 [节点内部图](../../openspec/changes/sandbox-evaluation-workflow/node-workflows.md)；原脚本存在不等于统一节点已验收。

创建链路为同步调用：

- [CubeAPI service](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L287) 等待 Master 返回，再生成 Sandbox 响应。
- [Master CreateSandbox](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/sandbox_run.go#L163) 调用 `createCtx.Wait()`，在 L170 等待完成或上下文超时。
- [Cubelet Create](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/Cubelet/services/cubebox/service.go#L462) 同步执行 `engine.Create`。
- [容器创建](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/Cubelet/services/cubebox/cube_container_create.go#L423) 启动容器，L452 执行 probe，L466 执行 create-time envd init，错误向上传递。

这证明 API 不是仅接受异步创建任务，但不足以证明任意模板成功响应后都能执行用户命令。[probe.go L89](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/Cubelet/services/cubebox/probe.go#L89) 仅在配置 probe 时探测；L145–146 无配置直接通过。L195–208 没有创建时环境变量时跳过 envd init。官方 perf 模板明确设置 `--probe 49999`，见 [cubebench.sh L1374](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1374)，它仍不是客户端真实命令 round-trip。

统一 harness 建议保留 `t_submit`、`t_api_done`、`t_exec_done`，主指标为同一受控命令成功返回且 exit code=0 的 `t_exec_done-t_submit`；分列 API 延迟及额外可执行验证时间。异步产品由适配器等待，Cube 使用原生同步 create 后验证，不重复创建。

## B01–B10 覆盖与缺口

| 项目 | 实际入口与证据 | 适配缺口 |
|---|---|---|
| B01 模板/快照创建 | [cubebench.sh L833](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L833) section 3.2 使用 cube-bench；c/n 为 1/20、10/200、20/300、50/500。section 4.3 在 [L1005](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1005) 使用 `bench_create_concurrency.py`；[该脚本 L43](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_create_concurrency.py#L43) 预制快照，L50–64 计整批创建墙钟。 | Go 单请求到响应头，Python 到 SDK 完整返回；wall/concurrency 为摊销成本，不是请求延迟。需统一冷热与 exec-ready 终点。 |
| B02 镜像创建 | Master [请求类型 L57](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/types/types.go#L57) 支持 Containers；[util.go L471](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/util.go#L471) 转换资源、image、probe。公开 CubeAPI 支持镜像→模板的异步 build，详见后文。perf [L750–758](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L750) 在测量前构建模板并等 ready。 | 原 perf 没有计入镜像构建成本。公开 API 的 `build→ready→create→exec` 应命名为镜像端到端创建，并拆分 build/start；不能冒充原始镜像直接启动。 |
| B03 暂停 | section 4.6 [L1049](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1049)；[bench_pause_resume_concurrency.py L99](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_pause_resume_concurrency.py#L99) 计整批 pause。SDK [sandbox.py L570](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L570) 默认 wait=true，L590–599 POST 后轮询 paused。 | 默认 1 秒轮询进入延迟；应记录单操作样本与暂停完成终点。 |
| B04 恢复 | 同脚本 [L110](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_pause_resume_concurrency.py#L110)；SDK [L618](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L618) POST resume 后检查响应。 | 没有恢复后 exec 验证，与暂停状态轮询终点不对称。 |
| B05 克隆 | section 4.5 [L1034](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1034)；[bench_clone_concurrency.py L53](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_clone_concurrency.py#L53) 计 `src.clone(n,c)`；SDK [L930](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L930) 先 snapshot，再并发 create。 | 完整 clone 含快照生成；应与已有快照 fan-out 分列。SDK clone 失败会清理成功兄弟实例（L973–986）。 |
| B06 删除 | [cube-bench runner L65](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/runner.go#L65) create-delete；[CubeAPI service L313](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L313) 给 Master 传 sync=true。 | 现有创建/删除错误共用一个 Err，缺独立删除压测；需分列逻辑删除、不可查询、资源释放终点。 |
| B07 命令 | 功能用例 [test_run.py L36](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/e2e/sdk_compat/cases/commands/test_run.py#L36) 检查 stdout/stderr/exit；L91 检查超时。 | 未见 tests/perf 独立命令延迟/吞吐入口。需统一命令、输出量、连接冷热。 |
| B08 快照 | section 4.1/4.2 [L950](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L950)；[bench_snapshot_concurrency.py L42](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_snapshot_concurrency.py#L42) 预建实例后计 snapshot；[bench_snapshot_dirty.py L86](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_snapshot_dirty.py#L86) 写 `/dev/shm/dirty`，计快照并 warm restore 后再计创建。 | wall/n 非请求延迟；dirty bytes 依赖本地日志，应明确缺测。需恢复后状态内容验证。 |
| B09 底噪 | 密度 [L1792](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1792) 只有单次主机 used-memory baseline。 | 没有完整空宿主/服务空闲/单 sandbox 的稳定窗口 CPU、RSS/PSS、IO 观测。 |
| B10 密度 | [cubebench.sh L1766](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1766) 实际扩容和内存摊销。 | 非 exec-ready 计数，无稳定窗口、资源反推密度、CPU/PSI/IO/OOM 曲线。 |

其他仓库自带功能测试包括 `tests/e2e/sdk_compat/cases/lifecycle/test_create.py`、`test_pause_resume.py`、`test_rollback_clone.py`、`test_fork.py`，以及 SDK Go/Python/Node 测试。cube-bench 有 `main_test.go`、`runner_test.go`；`tests/perf/cubebench_test.sh` 当前主要验证 CPU 拓扑报告，不能据此声称实际密度已验证。本次未执行这些测试。

## 现有计时、warmup 和清理陷阱

- [runner.go L30–42](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/runner.go#L30)：起表、构造请求、`client.Do`、停表；L49–63 才读取 body、验状态、反序列化。创建指标到 HTTP 响应头，不含完整响应，也没有 exec 验证。
- 删除 [L67–77](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/runner.go#L67) 同样计到响应头；错误写入同一个 `IterResult.Err`。因此成功创建但删除失败的样本也被排除。[main.go L271–279](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/main.go#L271) 只统计 Err 为空的样本；L315–317 吞吐为成功迭代数/总墙钟秒数。
- **实际 warmup 已排除总计时**：[main.go L420](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/main.go#L420) 先 warmup，L424 才起总表。`cubebench.sh` L839 的“warmup计入总时间”注释已过时。create-only warmup 的实例确实不会自动删除（runner L66 只在 create-delete 删除）。
- [env_reset L1066](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1066) 全集群删除再等待 count=0；[L1093–1109](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1093) 所有实例 ID 按50个一组、4并发删除，**未按 run_id 隔离**。
- 正常结束 [L818](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L818) 调 env_reset；EXIT trap [L728](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L728) 仅清临时工作目录。异常退出不保证实例清理；L823–826 明确仅提示人工处理模板/snapshot残留。
- 部分 Python 性能脚本清理不在 finally 中，如 `bench_create_concurrency.py` L50–64、`bench_pause_resume_concurrency.py` L96–121，异常可能跳过清理。

## B10 的准确算法、公式与限制

来自 [cubebench.sh L1766–1948](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1766)：

1. BATCH=100、并发50、max_batches=200、默认 max_total=2000（环境变量 `CUBE_BENCH_MVM_MAX`）；min_growth=max(1,BATCH/10)=10，stall_limit=2（L1767–1786）。
2. 先取得 used-memory baseline（L1792）。每批运行 `cube-bench -c 50 -n 100 -m create-only`，失败不立即结束；expected 累加100，sleep 2 秒（L1808–1814）。
3. 从 `cubemaster list --all --size 1000` 取得 actual（L1735、L1817）；随后读取主机 used memory。
4. `gained=actual-prev_actual`（L1839）。连续两批 gained<10 停止；任一批 gained>=10 将 stall 清零（L1871–1888）。
5. 若从未创建过任何实例而连续停滞，返回 setup/quota failure，不输出真实“容量0”（L1875–1881）。
6. actual>=max_total 时停止并标结果为下界；200批或读取 count/memory 失败时标提前停止（L1821–1835、L1894–1906、L1943）。

actual 是 CLI [list.go L327–331](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/cmd/cubemastercli/commands/cubebox/list.go#L327) 的 `len(rsp.Data)`，没有筛 Running，也不执行命令。准确表述是**列举存量净增长**，不是本批 API 成功数或 exec-ready 容量。旧实例退出/过期也会降低 gained。默认2000上限在整批完成后检查，检查实际存量而不是累计发起请求数，因此不是“最多发起2000次”，也不是不会超调的硬上限。平台还可能由配额、调度、TTL产生，不自动证明物理RAM极限。

主机内存 [L1702](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1702) 用 `free -m` used，失败回退 `/proc/meminfo` 的 `(MemTotal-MemAvailable)/1024`，没有 Available 时再减可回收 cache（L1707–1718）。报告公式（L1925–1934）：

`amortized_memory_i = (used_memory_i - baseline_memory) / listed_count_i`

单位 MiB；差值负或count=0显示“—”。这是内存摊销，不是资源反推密度。

单机门禁 [L910–946](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L910) 要求本机 Cubelet 配置存在、CubeOps登记节点总数恰为1；并非只数健康节点。提示文字说 drain 其他节点，但代码对 total>1 仍跳过。配额检查 [L1533](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1533) 读配额并提示，没有容量归因和实时内存停止阈值。

统一方案：同时保留兼容口径和 exec-ready 口径，记录每批 attempts、API成功、listed、running、exec-ready、旧实例损失、资源样本、stop_reason。实测取稳定窗口内 ready 存量。配额/超时/安全上限停止报告受限结果或“≥N”，不能称最大物理密度。资源反推使用0实例底噪及多个N的稳定窗口估算边际内存/CPU/FD/PID/磁盘，`D_mem=floor((memory_budget-baseline-reserve)/marginal_memory)`，`D_est=min(各有效资源上界)`，与实测单独报告。

## 公开 API 与 SDK 适配契约

以下路径相对 CubeAPI base URL；[routes.rs L94](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/routes.rs#L94) 注册实例路由，L129生命周期，L151快照，L168 fork，L174模板。鉴权使用配置提供的凭据，日志不得保存 token。

| 操作 | 请求 | 成功响应及完成处理 | 代码 |
|---|---|---|---|
| 创建 | POST `/sandboxes`，`{"templateID":"…","metadata":{"run_id":"…"},"timeout":-1}`；可加 `distributionScope`、`backend` 等 | 201 Sandbox：sandboxID/templateID/clientID/envdVersion/domain，可能含trafficAccessToken；保持token仅在内存。返回后exec验证。 | [service L162](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L162)、[models L433](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L433) |
| 查询详情 | GET `/sandboxes/{id}` | 200，state/metadata/cpuMilli/memoryMB/clientID等。state为running/paused/pausing/unknown；unknown包含创建中、已退出及无法读取。 | [models L657](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L657)、[state映射 L1303](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L1303) |
| 暂停 | POST `/sandboxes/{id}/pause`，不需body | 204空响应。SDK默认再轮询 GET详情直到state=paused，默认30秒、1秒间隔。 | [handler L349](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L349)、[SDK L570](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L570) |
| 恢复 | POST `/sandboxes/{id}/resume`，`{}`或`{"timeout":-1}` | 201 Sandbox。该endpoint为deprecated；SDK resume丢弃响应对象。 | [handler L389](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L389)、[SDK L618](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L618) |
| connect/自动恢复 | POST `/sandboxes/{id}/connect`，`{}`或timeout正值/-1 | 200 Sandbox；SDK返回新对象。timeout=0非法。恢复压测可用此接口，但需确认此前确为paused，返回后exec。 | [handler L439](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L439)、[models L711](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L711) |
| 快照 | POST `/sandboxes/{id}/snapshots`，`{}`或`{"name":"…","backend":"xfs"}` | 201 `{snapshotID,names,backend?,remoteStatus?}`。服务只接受snapshot.status=READY后返回；S3远端同步状态另列。 | [handler L33](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/snapshots.rs#L33)、[service L331](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/snapshots.rs#L331) |
| 快照列表 | GET `/snapshots?sandboxID=…&limit=100&nextToken=…` | 200数组；响应头 `x-next-token` 给下一页。字段snapshotID/status/originSandboxID/remoteStatus等。 | [handler L66](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/snapshots.rs#L66)、[models L747](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L747) |
| 快照删除 | DELETE `/templates/{snapshotID}` | 204空响应，snapshot分支带`x-operation-id`头；没有独立DELETE `/snapshots/{id}`。逻辑退休完成时仍可能因runtime引用保留物理数据。 | [templates handler L268](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/templates.rs#L268)、[service L112](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/snapshots.rs#L112) |
| SDK clone | `source.clone(n,concurrency)` | 没有对应 `/clone`；先snapshot，再POST `/sandboxes`，失败清兄弟，最后一个clone通过同SDK kill后删临时snapshot。 | [SDK L930](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L930) |
| 服务端fork | POST `/sandboxes/{id}/fork`，`{"count":N,"timeout":-1}` | 201数组，每项恰为`{"sandbox":…}`或`{"error":{"code":…,"message":…}}`；count 1..100。逐项判断，HTTP201不代表全部成功。 | [models L573](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L573)、[handler L257](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L257) |
| 删除实例 | DELETE `/sandboxes/{id}` | 204；Master请求sync=true。 | [service L313](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L313) |

创建 metadata 被映射为 Master labels，只有 `host-mount` 特殊转 annotation，见 [service L199](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L199)。harness 应显式保存资源清单；SDK clone 内部 `Sandbox.create(template=snap_id,config=cfg)` 不再传新metadata，见 SDK L934，不能依赖SDK clone自动带本次run标签。需要按原始snapshot+create自行注入metadata，或按返回ID追踪清理。

快照列表的backend query虽在模型中声明，但[handler L78–85](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/snapshots.rs#L78)没有传给service；若按后端筛选，需检查返回项backend后在客户端过滤。与实例列表不同，快照列表nextToken确实传递且返回下一页header。

### run metadata、分页与节点归属

可调用 `GET /v2/sandboxes`，query 参数通过HTTP客户端编码为：

```text
metadata = run_id=RUN_ID&suite=unified
state = running
limit = 100
```

`metadata` 是一个完整字符串参数，其内部 `&` 必须编码；[filter_by_metadata L1246](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L1246) 按`&`分割、`=`分隔并AND匹配。不支持复杂表达式。v1 GET `/sandboxes`只接metadata、不接state，固定传limit=200；[handlers L36–64](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L36)。Python `Sandbox.list/list_v2` 当前不暴露这些query参数（[SDK L451–483](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L451)），harness需直接HTTP调用。

**不要假设实例列表有可用cursor分页。** [models L1013](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L1013) 虽声明nextToken，[handler L110–125](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/sandboxes.rs#L110) 完全未使用它，也不返回cursor。[service L98–104](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L98) 固定Master start_idx=0、size=max(1,limit)。Master [sandbox_list.go L75–98](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/sandbox_list.go#L75) 把size用于**节点分页**，每节点返回实例列表，因此单节点2000实例不会因limit=100只返回100实例；超过limit个节点则可能不完整。harness不要对nextToken做虚假的分页循环；密度应单机固定归属并与已知ID逐个核对，跨大集群需Master inventory适配或修复公共API。

**节点ID字段存在语义陷阱。** GET详情 `clientID=d.host_id`（[service L146](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L146)），列表 `clientID=s.host_id`（[L1201](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L1201)），可将这两类响应的clientID适配为node_id；**创建响应**却把Master `request_id`传作clientID（[L303–309](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L303)），不能使用创建响应clientID判归属。公开列表不单列hostIP；需要IP映射时接CubeOps/受控节点inventory。

### 公开镜像构建：请求、轮询与B02

公开API可以完成镜像→模板→可执行实例：

1. POST `/templates`，最小body `{"image":"registry/image@sha256:…"}`；统一规格示例可加 `{"name":"bench-RUN","cpu":2000,"memory":2048,"writableLayerSize":"1G","exposedPorts":[49999,49983],"probePort":49999,"probePath":"/health","nodes":["NODE_ID"],"with_cube_ca":false}`。
2. HTTP202返回 `{jobID,templateID,status,phase,progress,errorMessage?}`。必须使用服务端返回的templateID；请求中的templateID被忽略。见 [handler L151](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/templates.rs#L151)、[model L1573](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L1573)、[job model L1678](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/models/mod.rs#L1678)。
3. GET `/templates/{templateID}/builds/{jobID}/status`，200 `{buildID,templateID,status,progress,message}`。**轮询此endpoint的状态是`building`、`ready`、`error`**，不是原始job的PENDING/RUNNING/READY/FAILED：Master [template_commit.go L224–244](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/httpservice/cube/template_commit.go#L224) 明确映射；CubeAPI [service L222–239](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/templates.rs#L222) 原样转发。遇ready继续；error失败；building等待；必须总deadline。
4. ready后 POST `/sandboxes` 使用返回templateID，注入run metadata和统一TTL，等待返回再exec验证。单列build耗时、实例start耗时和完整镜像端到端耗时。

资源单位有重要陷阱：公开 `cpu` 是**millicores**，`memory`是MiB；[service L574–580](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/templates.rs#L574) 把cpu格式化为`{v}m`。Python `Template.build(cpu_count=...)` 虽名为cpu_count，仍直接写payload["cpu"]（[_template.py L312–315](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/_template.py#L312)），不能传2却宣称2vCPU。PythonSDK拒绝dockerfile/start_cmd（L285–288）；可使用command/args。probePort省略但有exposedPorts时默认取第一个端口做HTTP `/health`（[service L551–570](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/templates.rs#L551)），通用镜像不一定提供此服务，必须显式匹配工作负载。

API路由超时当前为普通30秒、pause/resume/connect120秒、snapshot/create/template-delete240秒、fork1560秒（[routes.rs L26–39](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/routes.rs#L26)）。harness需单列client超时、server预算和生命周期TTL；不要把失败超时样本删除或归入成功延迟。所有上述适配建议尚未执行验证。
