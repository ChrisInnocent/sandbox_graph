# 三沙箱 B01–B10 对齐与方法交叉移植

2026-10-20。本文件是基于本地源码的测试合同与实施方案，**没有执行三套环境的真实性能测试，没有容量或延迟实测结论**。功能代码存在、原仓库测试存在、统一适配已实现、真实环境已验证是四个不同状态。本文不将任一前者写成后者。

2026-10-21 设计更新：当前以 [主图](../../openspec/changes/sandbox-evaluation-workflow/design.md) 和 [场景节点合同](../../openspec/changes/sandbox-evaluation-workflow/test-nodes.md) 为准，[align-sandbox-benchmarks/tasks.md](../../openspec/changes/align-sandbox-benchmarks/tasks.md) 的提前实施已暂停。本文件保留源码证据和早期方案，不替代任务完成证据。逐产品接口及限制见 [Cube审计](cubesandbox.md)、[AgentENV审计](agentenv.md)、[Kuasar审计](kuasar.md)。

用户已修正创建完成判据：**不强制 exec 作为唯一主指标**。可用经核验的状态或完成事件，客户端状态观察耗时与服务端内部耗时分别报告，命令可作停表后验证或独立指标。Kuasar 新补查的 `sandbox launch terminal`、`starting_total_duration` 起点及 `startedAt` 限制见节点合同第 3.2 节。下文保留的 exec 方法只是其中一种。密度新增固定总请求数的滑窗／分批并发对比及 PSS/CPU 联合采集，底噪 PSS 的 Guest OS 扣除边界待确认。

## 节点级自动化与实施边界

以下为目标职责，不是实现完成声明；每个节点的内部 Mermaid 和实现状态统一见 [内部图](../../openspec/changes/sandbox-evaluation-workflow/node-workflows.md) 与 [职责表](../../openspec/changes/sandbox-evaluation-workflow/node-automation.md)。

| 工作节点 | 可脚本化 | 未脚本化或待验证 |
|---|---|---|
| R1/T0 | 仓库版本、文件／行引用、方法字段和矩阵覆盖核验 | 调用链语义、官方方法等价性与新接口分析由模型参与 |
| E1/E2/C1–C3 | 类型／角色检查、实际生效值、已有确定方案配置、独立复检与报告 | 新部署适配、最大／无约束目标、必要凭据或范围待明确 |
| N00–N03 | 合同、外部任务对账、fixture、真实协议／功能预检和复位 | 产品路径未实现或未测不能由模型补成成功 |
| N04–N06、各 B/E 方法 | 发压、状态／日志观察、各组件 PSS/CPU、每秒创建吞吐、逐项断言和后验 | 新资源归属／Guest OS 减项语义需证据；参数选择未定交人工 |
| N07–N10/T6/T8 | 原始证据、局部统计、所有权清理、交付及组复核 | 未知异常解释；已有根目录脚本只为 draft |
| A1–A7/F1–F4 | 统一计算、比较、Excel、事实／引用校验、归档 | 模型解释与有证据建议；业务取舍按需人工 |

特别补充：密度采集的每秒创建数使用唯一成功完成事件，不把接纳或最终存活数替代；分批等待的零值与事件缺测分开。所有官方脚本仅为上游已存在方法，统一执行器仍待实现／验证。本文件后续代码目录方案为历史草案，不能据此恢复暂停任务。

## 1. 来源与版本

| 候选 | 审计本地提交 | 来源证据与范围 |
| --- | --- | --- |
| CubeSandbox | `b25e416e990e6ba0641b4609974c53c61ee568f0` | origin脱敏为TencentCloud/CubeSandbox；[创建链](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/sandbox_run.go#L163)、[原perf](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1766) |
| AgentENV | `49684233698d40dd800c1d0edba15ab30a745eeb` | origin脱敏为kvcache-ai/AgentENV；[同步创建](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L419)、[冷启API](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L666) |
| Kuasar聚合仓 | `ba218fc5f20973621277484b9144a8e838ffe840` | origin脱敏为kuasar-sandbox/kuasar-sandbox；[原密度](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L597) |
| Kuasar orchestrator | `c14da165ecc14de0e5c0afbfba2e1b737d64ee7c` | [异步接纳](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/orch.go#L338)、[public API](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L461) |
| Kuasar connector | `8fc4b6a266ff551b2747a83f43ce1c8a717ef187` | [网络配置E2E](https://github.com/kuasar-sandbox/connector/blob/8fc4b6a266ff551b2747a83f43ce1c8a717ef187/test/e2e/cases/network.provision.sh) |
| Kuasar sandboxer | `3f4978c39499c98acd5e8a7b32d450bb66ea4db9` | [底层生命周期E2E](https://github.com/kuasar-sandbox/sandboxer/blob/3f4978c39499c98acd5e8a7b32d450bb66ea4db9/test/e2e/cases/sandbox.lifecycle.sh#L141) |

审计时这些仓库工作树clean，未fetch，故不声称等同最新官方main。部署测试还必须记录实际二进制、envd、runtime/kernel、镜像/模板digest及配置；仅提供Git SHA不足以证明运行的是这些字节。`kuasar-components`不是独立组件仓库，不能使用它继承的外层Git HEAD作组件版本。

## 2. 共同创建完成事件与时间

每次操作使用同一个单调时钟和总deadline。并发数表示允许多少操作同时进行，与“调用返回时已经可执行”的同步合同分开。

| 事件 | 定义 | 输出指标 |
| --- | --- | --- |
| `t_submit` | 首次且唯一的创建POST发送前 | 全链路起点 |
| `t_headers` | HTTP响应状态及headers已读到 | `api_headers=t_headers-t_submit` |
| `t_body` | 完整body读完、JSON及必需ID校验完成 | `api_complete=t_body-t_submit` |
| `t_state` | 观察到该ID达到running；保留查询时间和原始状态 | `observed_running=t_state-t_submit`；是采样上界，不是内部精确转态时刻 |
| `t_exec_submit` | 受控无副作用标记命令发送前 | 额外探针链路起点 |
| `t_exec_done` | HTTP/协议无错误、完整end事件、exit=0、stdout包含本次唯一nonce | 可选“创建至首次命令完成” `t_exec_done-t_submit`，不强制作为纯创建耗时 |

同时保留每次state/exec探针的开始、结束、结果及重试间隔。探针重试不重置总表，不重复POST创建；客户端timeout可能已有外部副作用，必须按metadata/ID对账，不能当作“未创建”。未收到ID的迟到创建单列为未知资源风险，不纳入成功。

- Cube原 [runner.go L30–63](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/runner.go#L30) 在`client.Do`返回时停表，body尚未读完；只能对齐`t_headers`。其 [Master](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/sandbox/sandbox_run.go#L163) 同步等待，但[无probe时直接通过](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/Cubelet/services/cubebox/probe.go#L145)，仍需exec。
- AgentENV [service.rs L419](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L419) 承诺返回时ready，启动路径先wait_for_ready再Running；[冷启](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L676) 也为同步请求。统一测试仍独立验证exec。
- Kuasar [HTTP201](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L567) 仅durable acceptance；[runtime readiness+envd init+commit](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/sandbox_task_prepare.go#L265) 后才running。公共GET detail轮询，不能拿内部DB helper充当公共接口。

所有产品报告API成功、状态成功、exec成功三个计数。吞吐分别报告成功操作数/整批wall、并发与offered rate；`batch_wall/N`只叫摊销时间，不能叫单请求延迟。延迟分位只针对明确成功终点，同时显示全部失败/超时及其耗时，避免幸存者偏差。

## 3. 方法保留与交叉移植矩阵

“保留”表示保存原口径及原标签来源；“移植”表示用目标产品真实接口实现相同实验方法，不照抄全局清理、错误吞没或假ready。以下移植是合同/待实现项，不表示已完成代码或实际运行。

| 方法与原证据 | Cube | AgentENV | Kuasar | 共同限制 |
| --- | --- | --- | --- | --- |
| HTTP响应头/完整响应计时：[Cube runner](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/cube-bench/runner.go#L30) | 保留原头部计时，增加完整体/state/exec | 移植全部事件 | 移植全部事件，201与ready分开 | 不删原指标，统一主指标用exec |
| 预制快照创建并发梯度：[Cube batch create](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_create_concurrency.py#L43) | 保留整批wall与每请求 | snapshot模板→create | e2b-snp→create | 冷热缓存、预制成本分开 |
| pause/resume整批墙钟：[Cube脚本](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_pause_resume_concurrency.py#L99) | 保留 | 移植原生pause/resume | 移植pause/connect | 再增加每请求与业务恢复验证 |
| 完整clone与预制快照fan-out：[Cube SDK clone](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/sdk/python/cubesandbox/sandbox.py#L930) | snapshot+create与native fork都保留 | native fork与snapshot+create都保留 | pause+export-template+create | 完整路径计捕获；源副作用独立 |
| dirty-memory快照：[Cube dirty脚本](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/examples/snapshot-rollback-clone/bench_snapshot_dirty.py#L86) | 保留 | 移植capture+publish | 移植pause捕获与export发布 | 相同touch/dirty字节，检查内容，不能只分配不触页 |
| backend pause/hot-cold restore：[Agent benchmark](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/benchmarks/benches/snapshot_benchmark.rs#L233) | 保留为待设计的底层对照 | 保留backend层原方法 | 保留底层snapshot/restore方法 | 与HTTP端到端分层；“cold restore”不是OCI冷启动 |
| manifest冷/热恢复至TICK：[Kuasar脚本](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/sandbox-perf-manifest.sh#L615) | 迁移“冷/热后首业务输出”方法，保留后端差异 | 同左 | 保留原低层TICK与新HTTP exec | 缓存清理需要真实措施；sleep不是cold证据 |
| 固定N idle/cycles/pareto资源窗口：[Kuasar workload](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L10) | 移植相同guest负载 | 移植相同guest负载 | 保留并补业务完成 | 配置zone不是物理成本；峰值采样与负载周期对齐 |
| 逐批净增长、两批<10：[Cube密度](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1766) | 保留兼容净增长及新增/存量ready | 交叉移植 | 交叉移植 | 旧实例损失、TTL、限额与失败分类 |
| 固定N增量资源反推：[Kuasar公式](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L597) | 交叉移植原口径+统一口径 | 同左 | 保留原公式，增加统一公式 | 模型估算，不能冒充实测饱和 |
| 业务完成/输出hash/无重启：[Kuasar Agent校验器](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/agent-density/README.md#L21) | 可移植验证合同，需另建launcher | 同左 | 原checker仅验外部BMS证据 | 原3/16并发数字不是跨产品基线，也非portable launcher |

保留产品特有指标，如manifest读取、UBLK共享、XFS或远端快照状态、气球/资源控制器统计；它们置于解释性扩展列，不硬凑为共有字段。缺测标unknown，不填0。

## 4. B01–B10 场景合同

全场景公共输入：产品/部署版本、单节点或集群拓扑、明确节点归属、source digest、CPU/内存/磁盘及**生效证据**、TTL、缓存条件、重复数/并发、负载、客户端与服务端deadline、资源采样域、清理预算。原生是已有公开服务动作；组合是多个动作构成一个目标场景，不表示底层能力不存在。

| 场景 | 输入与功能断言 | 计时及副作用 | 各候选原生/组合路径 |
| --- | --- | --- | --- |
| B01 模板/快照创建 | 固定模板；新ID；文件基线hash正确；exec nonce/exit；实例互不污染；核实实际规格 | 模板准备在本场景前另记；t_submit至t_exec_done；分冷/热、逐个/并发 | 三者POST模板create；Kuasar e2b-snp，Agent资源继承模板 |
| B02 镜像完整创建 | 固定OCI digest和规格；guest内容/应用版本、网络与exec；模板/构建job与实例均可追踪 | 从首次镜像相关准备动作起到exec；拆pull/prepare/build/publish/start；记录缓存与残留artifact | Agent原生cold；Cube镜像→异步模板build→ready→create→exec；Kuasar镜像→合法Image artifact准备/发布→e2b-img→create→exec |
| B03 暂停 | 活跃进程计数器、文件/内存标记；暂停后state=paused，不能因探针自动唤醒；检查进程停止推进 | API/paused/资源释放分别计时；记录业务停止窗口、snapshot写入 | Cube/Agent原生pause；Kuasar默认memory=true捕获+pause，禁止与仅freeze混为等价 |
| B04 恢复 | 前置确认paused；相同ID；文件hash；memory恢复必须证明原进程及内存连续；冷恢复单独profile | request至running与exec；业务不可用窗口另记；TTL续租计入说明 | Cube connect或resume；Agent resume/connect；Kuasar connect(memory模式显式) |
| B05 克隆 | count、并发、源状态；每child新ID；初始内容相同；child独立写不影响源/兄弟；逐项失败/部分成功 | 完整clone计捕获+发布+fan-out；预制快照fan-out另列；源暂停/运行/恢复成本列明 | Cube native fork、SDK snapshot+create；Agent native fork、snapshot+create；Kuasar pause+export keepSource+create，恢复源是额外动作 |
| B06 删除 | 删除前ID存在；返回后查询/列表一致；guest不可执行；资源释放与本次清理清单核对 | API返回、逻辑不可访问、物理释放三终点；共享artifact保留不算泄漏 | 三者DELETE；只删本次拥有ID，不能全局reset |
| B07 命令 | 相同用户/cwd/env、命令长度及输出字节数；stdout/stderr/exit；异常退出与超时是预期断言 | 请求至完整协议end；连接冷/热、首条/稳态分开；timeout副作用对账 | Cube SDK/envd；Agent已证proto codec、JSON须smoke；Kuasar envd Connect JSON或单列native exec |
| B08 快照 | dirty工作集触页；进程内存和文件标记；返回artifact可由新实例恢复；源保留/运行状态核对 | 捕获、持久化/发布、远端同步、恢复验收分别计时；不能以snapshotID存在判可恢复 | Cube原生snapshot并区分remoteStatus；Agent原生capture+publish保持源运行；Kuasar pause(memory=true)+export，源paused，需另connect恢复 |
| B09 底噪 | 零测试实例、必要服务开启的基线；单idle实例增量；连续稳定窗口；列出后台任务/缓存 | CPU、内存、IO、网络、FD/PID、PSI可获得项；采样器自身成本；不将清理后样本当idle运行 | 三者共同新增采样合同；已有节点指标只能在相同域验证后使用 |
| B10 密度 | 固定规格、idle或明确活跃负载；本批新增与存量exec健康核验；OOM/业务完整性/清理 | 同时输出增长实测与资源估算两个方法；稳定窗口、停止理由、限额、存量损失 | 将Cube增长方法和Kuasar增量反推法交叉移植到三者；不以配置额度当实际占用 |

关键来源：

- Cube [模板build请求与job](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/handlers/templates.rs#L151)、[building/ready/error状态映射](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/pkg/service/httpservice/cube/template_commit.go#L224)、[资源cpu转millicores](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/templates.rs#L574)。2 vCPU应传cpu=2000；完整B02不能只计预制模板create。
- Agent [cold schema](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L743)、[模板资源继承](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L525)、[snapshot发布](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1841)、[fork逐项结果](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1521)。B01不得向未定义的字段传资源期待覆盖。
- Kuasar [template ref校验](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/types.go#L284)、[resource输入](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/docs/node.md#L878)、[pause请求](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L756)、[export结构](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L1226)。`e2b-img-...`后缀是artifact ref，不是Docker镜像名。没有准备路径耗时的测量只能称“已准备Image artifact启动”，不能称完整B02。

快照是否包含内存、是否包含应用持久卷、共享存储位置与远端复制承诺应成为场景参数。三者提供的不同副作用和完成承诺保留为子方法；不要强行将更短的本地捕获与完整远端发布排名。

## 5. 密度的原口径与统一口径

### 5.1 Cube原增长与内存摊销

[cubebench.sh L1766–1948](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1766) 默认批100、并发50、最大200批、max_total=2000；门槛 `max(1,BATCH/10)=10`，连续2批停止。每批create-only后sleep2秒并列举存量：

```text
gained_i = listed_count_i - listed_count_(i-1)
stall_i = stall_(i-1)+1 if gained_i < 10 else 0
stop if stall_i >= 2
amortized_memory_i = (used_memory_i - baseline_memory) / listed_count_i
```

`listed_count`来源为CLI返回列表长度，不筛running、不exec：[list.go L327–331](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeMaster/cmd/cubemastercli/commands/cubebox/list.go#L327)。因此**原规则是存量净增长<10，不是本批新增成功<10**。新增成功20而旧实例丢失15时，净增长5，会触发stall；两个数必须分别保存。

达到max_total是在整批后检查存量，并非请求硬上限。达到实验上限只形成下界；从未成功则是setup/quota failure，不输出“物理容量0”。free-used及MemAvailable回退口径见 [L1702–1718](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/tests/perf/cubebench.sh#L1702)，其摊销式不是容量反推。

### 5.2 Kuasar原反推

[density-perf.sh L597–604](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L597)：

```text
A0 = daemon启动后、首实例前的MemAvailable MiB
Amin = 5秒采样窗口内最小MemAvailable
Nrequested = 配置N（非ready数）
d_original = decimal_round((A0-Amin)/Nrequested, 1)
original_projected = floor(Amin/d_original), 仅d_original>0.5时输出
```

报告 [L642](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L642) 称projected ceiling，但该式近似剩余可增加量，未加既有N、未扣预留。控制器HOST_RES_MEM等配置不进入公式。未ready仍除以requested、5秒漏峰、非线性共享页都会影响估算。停止实例后取得的post_per_sb属于回收阶段，不能作为存活idle成本：[L573–601](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L573)。

原值保留在独立compatibility字段中，完整保存requested分母、舍入、阈值、窗口与标签错误解释，不把修正后的新值声称为原脚本结果。

### 5.3 统一资源域与公式

固定同一节点、同一采样域；零测试实例但服务全开，得到可用内存基线A0；N个全部验证健康的测试实例保持存活，取运行窗口Amin；预先指定同单位预留R。计算中保留浮点精度，不先四舍五入成本：

```text
delta_mem = A0 - Amin
cost_mem = delta_mem / Nhealthy
estimated_total_mem = max(0, floor((A0 - R) / cost_mem))
estimated_additional_mem = max(0, estimated_total_mem - Nhealthy)
```

前提为N>0、delta>0、A0>R、采样完整、全部目标实例ready且身份/归属可核实。否则统一估算invalid并注明原因，不能用requested补分母。若估计总量小于已运行N，报告预算冲突，而不是掩盖冲突只显示additional=0。该单点式是线性预测，不是实测最大值；下一阶段在多个N稳定窗口用`Δresource/ΔNhealthy`拟合边际成本并报告残差/区间。

更通用的已用资源式为 `floor((budget-baseline_used-reserve)/marginal_cost)`。内存budget/基线可用式只能在同一可回收内存口径下转换。CPU预算单位是核或核秒/秒；磁盘、FD、PID等各用自身预算及成本。只有实际测量的有效维度才参与 `estimated_total=min(valid_resource_bounds)`；未测CPU不能虚构CPU容量。

**采样域不能混合：** Agent节点 `memoryUsedBytes`可能是cgroup.current，也可能是MemTotal−MemAvailable，CPU又来自宿主/proc/stat：[host.rs L118](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/observability/host.rs#L118)。Cube宿主free-used、Kuasar宿主MemAvailable、容器cgroup指标不能直接放同一公式。优先三者相同只读host sampler，并单列服务/VMM cgroup；不得用allocatedMemoryBytes、capacity.memory替代物理增量。

### 5.4 统一增长的两个独立结果

同一批至少保存 attempts、api_accepted、new_running、new_exec_ok、listed_owned、healthy_owned、old_lost、late_unknown、资源时间序列。

- `cube_growth_compat`：按本次拥有实例的**列举存量净增长**连续两批<10停止。隔离run后与原全局列表不是完全字节等价，须写明域变化。
- `new_ready_growth`：若选本批新增exec成功<10作为停止规则，明确另命名，不声称复现Cube原算法。

两种规则均复验旧实例。主实测结果为指定稳定窗口内、相同节点上同时健康实例的观测值；未尝试更高规模写“已验证N”，触及实验上限写“至少N”。限额/限流/TTL/路由/资源不足分别归因；两次停滞不是物理极限的证明。

Cube详情/list的clientID可映射host_id，但create响应clientID是request_id：[service.rs L146](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L146)、[L303](https://github.com/TencentCloud/CubeSandbox/blob/b25e416e990e6ba0641b4609974c53c61ee568f0/CubeAPI/src/services/sandboxes.rs#L303)。Agent无公共nodeID，需受控节点URL逐个查或Gateway debug header：[helpers.sh L514](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/lib/helpers.sh#L514)。Kuasar单机直连或受控固定placement亦须实证；无归属证据不能发布单机密度。

## 6. B03–B08 下一步代码方案与状态

依据现有 [design.md](../../openspec/changes/align-sandbox-benchmarks/design.md)，统一层约定为根目录`sandbox_bench/`；当前分工为`transport.py`、`adapters.py`、`measure.py`、`density.py`、`observe.py`、`__main__.py`。根CLI近期范围是`create`、`density-growth`、`density-projection`，共用nonce exec，不能据此声称B01–B10全部覆盖。以下建议新增`lifecycle.py`与`sources.py`，**二者尚待实现**；表中是下一步代码方案，不是已实现或已验证的场景清单。本文件生成时未执行这些场景，真实环境也尚未提供。实施者应以实际提交、对应合同测试及运行记录更新状态，不能由本表勾选完成。

| 场景 | 计划代码位置及职责 | 必需合同测试 |
| --- | --- | --- |
| B03 | `sandbox_bench/adapters.py`映射pause及状态；建议新增`lifecycle.py`建立marker、等待paused，复用`observe.py`采样释放；避免数据面自动wake探针 | 204但未paused、已paused409、deadline、内存捕获与disk-only差异 |
| B04 | `adapters.py`映射resume/connect和更新访问令牌；`lifecycle.py`确认paused前置、内存/文件连续性及exec终点，复用`measure.py`命令验证 | 200/201但starting、错误token、memory丢失、冷恢复不能误判内存恢复 |
| B05 | `adapters.py`native fork及snapshot/export组合；`lifecycle.py`拆分capture/publish/fan-out及源恢复；`__main__.py`登记所有部分成功ID/artifact | 201数组部分失败、重复ID、源副作用、兄弟写隔离、失败仍清理所有已知拥有资源 |
| B06 | `adapters.py`delete与详情/list对账；`lifecycle.py`逻辑删除及资源释放事件，复用`observe.py`；`__main__.py`cleanup journal | 204后仍可查询、未知ID迟到、删除错误与创建结果分离、禁止全局清空 |
| B07 | 复用`transport.py`Connect framing/流错误/超时、`adapters.py`寻址与`measure.py`nonce exec；`lifecycle.py`新增独立命令冷暖连接/吞吐场景 | 截断帧、HTTP200协议error、无end、非零exit、wrong nonce、Agent JSON兼容预检失败不能自动判成功 |
| B08 | `adapters.py`Cube/Agent snapshot、Kuasar pause+export；`lifecycle.py`dirty工作集、artifact发布状态、恢复内容及源状态；`__main__.py`artifact ledger | snapshot仅本地ready、远端未同步、result与snapshotID字段差异、keepSource默认false风险、恢复内容错误 |

B02建议新增`sandbox_bench/sources.py`管理镜像准备、Kuasar准备步骤provider及产物追踪，配合`adapters.py`模板build/job状态与`measure.py`完整路径起点。B09在`observe.py`基础上增加多稳定窗口与独立场景，不可从`density.py`的单次baseline推导全部底噪。现有创建/密度原语即使通过本地合同测试，也不等于这里所有B场景已经实现。

只有数据面协议、认证、功能smoke成功后才进行性能迭代。Agent当前源码直接证明Connect proto，JSON codec仍需现场验证；Kuasar本地envd JSON fixture加代理转发代码证明可设计该链，但本审计未实测公共代理组合。若codec不兼容，应实现已证实协议或明确阻塞，不能静默替换为health-only。

## 7. 阻塞真实环境测试的输入

以下值未从本地源码自动获得，缺失会阻止对应真实场景；凭据仅通过安全环境变量/外部注入，不写结果或文档：

1. 三环境API base URL、TLS/CA策略、代理base/domain/端口、API鉴权环境变量名和访问权限；单机/集群模式与节点URL/inventory。没有endpoint不能发起smoke。
2. 部署实际组件、envd/runtime/kernel版本及镜像digest；Cube backend、Agent KVM/PVM与OSS/POSIX、Kuasarartifact/store配置；没有这些不能形成可追溯比较。
3. B01合法template/snapshot ID、来源digest及规格；Agent模板继承资源，需先准备符合规格模板。Cube按millicores/MiB，Kuasar按resource JSON；所有候选需生效核验。
4. B02固定OCI镜像digest、registry访问、模板build/publish权限和存储目的地；Cube job状态可达；Kuasar合法Image artifact准备/发布命令或provider、源到artifact的证据和计时起点。缺少准备仅能运行已准备模板/产物启动子场景。
5. B03–B08 checkpoint/快照后端与权限、是否要求远端持久性、源可暂停策略、目标恢复节点/同节点约束、artifact清理/保留政策；不能擅自删除共享模板。
6. 单机密度目标node_id、固定placement及归属验证方式；宿主/容器资源域、只读本地或SSH采集argv与权限、内存/CPU/磁盘预算、预留R、安全上限和明确停止策略。缺少宿主采样不能给资源反推；缺少归属不能给单机密度。
7. TTL及续租约定、总运行/每操作deadline、重复/并发/批大小、max_batches/max_instances、观察稳定窗口、采样间隔、随机种子、idle/active工作集、cold/warm缓存措施。timeout=0不可统一当无限；Agent默认短TTL尤其须显式配置。
8. 唯一run标签、输出目录、资源ledger、迟到创建对账能力及清理deadline；授权测试规模须具体。不能复用Cube全局env_reset清空其他资源。

这些是执行依赖，不是要求重复批准已授权的源码整理或本地合同验证。在输入齐备且实现完成前，报告应保留“尚未执行/待适配/缺输入”，不生成模拟性能数字或将本地mock测试称为真实沙箱结果。

## 8. 模板与快照的语义审计：来源产物与启动路径

2026-10-21 补查，纯源码审计，未运行任何沙箱、构建或性能测试。

**问题**：三产品都把创建参数叫 `templateID`／“模板”，但从“模板”创建与从“快照”创建是否等价？能否合并为同一个实验方法？

**结论（先行）**：**不等价，不能合并为一个无条件的创建方法**。“模板”在三个产品里是三件不同的东西；决定实验身份的不是“模板”这个名字，而是**来源产物 kind** 与**解析出的启动路径**。三者都把“从模板创建”落进同一个 create 入口，但该入口内部可能是镜像冷启、portable sandbox 冷启或快照内存恢复，其前置生产动作、规格来源、源副作用和完成语义都不同。

### 8.1 Kuasar：模板是“带 kind 的自描述引用”，模板 ⊇ {image, sandbox, snapshot}

- `kuasar-components/orchestrator/internal/types/types.go` L43–48：`Kind` ∈ {`img`（cold boot from a portable image artifact）, `sbx`（cold boot from a portable sandbox）, `snp`（restore from a snapshot manifest）}。
- 同文件 L135–140：`LaunchMode` ∈ {`image`, `cold`, `memory`}。
- L203–210 `LaunchModeForTemplate`：`img→image`、`sbx→cold`、`snp→memory`。即同一句“从模板创建”会因 kind 不同走到三条启动路径。
- L247–299 `TemplateID` = `<profile>-<kind>-<base64url(canonical-portable-ref)>`；file ref 按 kind 校验后缀 `.image/.bundle`(img)、`.sandbox/.bundle`(sbx)、`.snapshot/.bundle`(snp)。
- L54–58 捕获产物 `CaptureKind` ∈ {`snapshot`, `sandbox`}；L64–68 `ResumeSourceKind` ∈ {`snapshot`, `sandbox`}；L180–197 `ResolveLaunchMode`：snapshot 默认 `memory`（可显式 `cold`），sandbox 只允许 `cold`。
- 构建模板是独立异步链：公共 builder `POST /v3/templates`（注册 name/cpu/memory）→ `POST /v2/templates/{tid}/builds/{bid}`（fromImage，节点侧 pull+flatten）→ `GET …/status`（building/ready/error），见 `internal/api/api.go` L384–391；ready 产出**持久** `e2b-img` templateID（`internal/orch/build.go` L976–992）。
- 含义：Kuasar 的 “template” 是**引用/绑定**，不是产物类型。方法定义若不写明 kind，就说不清测的是镜像冷启、portable sandbox 冷启还是内存恢复。

### 8.2 CubeSandbox：模板命名空间是“镜像构建模板 ∪ 快照”的并集

- 模板：`POST /templates` body.image 必填（`CubeAPI/src/services/templates.rs` L128–133）→ `CreateTemplateFromImageReq`（L141，`source_image_ref = body.image` L150）→ CubeMaster build job，状态 building/ready/error，auto `tpl-` id。
- 快照：`POST /sandboxes/{sandboxID}/snapshots`（`CubeAPI/src/services/snapshots.rs` L35）从**运行中的 sandbox** 捕获 → `snapshotID`；`ensure_snapshot_ready`（L66）要求 READY。
- 同一命名空间：快照删除走 `DELETE /templates/{templateID}`，源码注释明写 “when templateID is a snapshot id”（snapshots.rs L112）。
- 创建：`POST /sandboxes` 只收 `template_id`（`CubeAPI/src/services/sandboxes.rs` L266、L1188）；CubeMaster 把它解析为模板**或**快照，错误信息统一是 “template/snapshot not found”（sandboxes.rs L1006–1019，`openapi.yml` L79）。
- 含义：Cube 的 create 入口是同一个，但**模板由 image 构建、快照由运行实例捕获**，产物来源与制作阶段完全不同；`templateID` 这个名字本身不能区分二者。

### 8.3 AgentENV：API 的 “templateID” 就是快照 ID；Image 冷启是另一条路

- 创建（warm）：`POST /sandboxes` 取 `body.template_id` → `snapshot_manager.load_runnable(&body.template_id)`（`AgentENV/src/api/impls/sandbox.rs` L1016–1024，`SandboxStageTimer::new("create_warm")`）→ `SandboxLaunchSource::Snapshot`（L1102）。
- 模型映射：`template_id: m.snapshot_id`（sandbox.rs L341/L361/L425）——对外文档说的 “template”，存储里就是 snapshot id。
- 冷启（Image）：`SandboxLaunchSource::Image { image_ref, overlaybd_config_path, resources, … }`（sandbox.rs L771；`src/orchestrator/types.rs` L13–25）。
- 规格来源分叉：snapshot 路径 `resources: *snapshot.resources()` 继承快照（`src/orchestrator/service.rs` L525）；image 路径 `resources.unwrap_or_else(default_fresh_sandbox_resources)`（L559）。
- 跨环境约束：snapshot 路径校验 `committed.virtualization_mode != configured_mode` 即报 `VirtualizationModeMismatch`（service.rs L486–490）——快照禁止跨虚拟化模式（KVM/PVM）。
- 构建模板 = 跑一个 `template_builder` sandbox（service.rs L436 `create_template_builder`，`template_builder=true`）再 publish 快照：`snapshot_manager.publish_captured(SnapshotPublishSource::Sandbox { source_sandbox_id })`（sandbox.rs L1848），产出新的 `SnapshotId`——即下一次 create 的 template_id。
- 含义：AgentENV 里“模板”与“快照”在 API 层**同名同物**；真正与它并列的是 Image 冷启。

### 8.4 逐维度对照：为什么不可合并为同一实验方法

| 维度 | 从镜像／构建模板 | 从快照创建 |
|---|---|---|
| 来源产物 | OCI image → 派生 image artifact（Cube `POST /templates` build job；Kuasar builder 产 `e2b-img`；AgentENV image 冷启） | 运行实例捕获的 snapshot manifest／portable sandbox（Cube snapshotID；Kuasar `snp`/`sbx`；AgentENV snapshotID） |
| 启动路径 | 冷启动（`LaunchImage`；AgentENV `for_create_fresh`） | 快照恢复（内存 `LaunchMemory` 或磁盘冷恢复 `LaunchCold`） |
| 前置生产动作 | build／convert／publish 异步 job（building→ready） | capture／pause／export／publish（可能暂停或占用源实例） |
| 前置成本归属 | 一次性构建成本；共享构建与独立创建是两个条件 | 一次性捕获／发布成本；源副作用需单列 |
| 规格来源 | 请求显式规格或默认规格（AgentENV `default_fresh_sandbox_resources`） | 继承快照规格（AgentENV `*snapshot.resources()`；Kuasar 恢复的 capacity 构成约束） |
| 源副作用 | 无运行源 | capture 可能暂停源；Cube/AgentENV 保持源运行，Kuasar pause 默认 memory=true |
| 跨环境约束 | 镜像 digest 可移植 | AgentENV 快照禁止跨虚拟化模式（KVM/PVM） |
| 可测完成点 | build ready + create + running | snapshot ready + create + running／restore |

### 8.5 对 B01 的方法拆分结论

- **保留并强化两个独立 method 身份**：`B01.TEMPLATE` 与 `B01.SNAPSHOT` **不能合并**。现行 `sandbox_bench/registry.py`、`sandbox_graph/catalog/registry.py` 的 METHODS 列表与 [test-nodes.md §4](../../openspec/changes/sandbox-evaluation-workflow/test-nodes.md) 已经是两行，方向正确，本次审计确认其必要。
- **`B01.TEMPLATE` 必须再钉死 `source_kind`**：否则跨产品不可比——Kuasar 的模板可以是 `img`/`sbx`/`snp`，Cube 的模板只对应 image 构建产物，AgentENV 的 “template” 实为 snapshot。已登记为要求记录来源产物 kind 与解析出的启动路径（`runtime-evaluation` RT-16／RT-24／RT-40），而不是只写 “TEMPLATE”。
- **`B01.SNAPSHOT` 内部要再分磁盘／内存**：已登记为 `B01.SNAPSHOT.DISK` / `B01.SNAPSHOT.MEMORY`，对齐 B04 命名粒度，分别对应 Kuasar `sbx`(cold)/`snp`(memory)、Cube cold/memory（见 `test-nodes.md` §4 与 tasks 27.3）。
- **制作成本不计入 B01**（`test-nodes.md` §4 已定），构建与捕获分别由 `B02.FULL`（image→artifact build）与 `B08`/`B05.FULL`（capture/publish）承接。
- **“构建模板／构建快照”是产品里的独立异步阶段**（Kuasar builder job、Cube template build job、AgentENV builder sandbox + publish）。本轮已把它登记为独立场景目录 `B00.TEMPLATE_BUILD` / `B00.SNAPSHOT_BUILD`（`runtime-evaluation` RT-41、`test-nodes.md` §4、`benchmark-design.md` BCD-05/BCD-07、tasks 组 27），构建成本只计一次且不与 `B02.FULL`、`B08` 重复计税。

**登记落点（本轮）**：需求 `RT-16`／`RT-24`／`RT-40`／`RT-41` 与 `RT-02` B01 行；设计 `design.md` §3 `M0`/`M1` 与 §4 B00/B01 行；节点 `test-nodes.md` §4 B00–B01 与 §9 官方方法行；执行 `benchmark-design.md` BCD-05/BCD-07；任务 `tasks.md` 组 27（代码、三产品脚本与独立单元／契约测试）。

## 9. 创建端点的可及性、同步/异步与就绪信号

2026-10-21 补查，纯源码审计，未运行任何沙箱、构建或性能测试。

**问题**：Kuasar 的 router `/sandboxes` 与数据面 node API 是同步还是异步？口径如何落到测试？若部署没有管理面（ingress/router）角色，能否用只存在于数据面的入口？能否像 CLI 一样给创建注入 `--ready-fd`，由测试进程直接取创建成功时刻？

**结论（先行）**：

1. **router `/sandboxes` 同步（阻塞到 running）**；**数据面 node API 异步（返回 `starting`）**；**node-ctl `run-sandbox` 是底层异步执行体**。三者是不同层，不能混为一个“创建完成”口径。
2. 请求 router 需部署里存在**管理面/ingress 角色**；仅有数据面时，可远程访问的创建入口只有数据面节点的 node API（异步，须轮询到 running）。
3. **`--ready-fd` 机制已经存在**，但它是 node 内部 fd／UDS 链，**不能经 HTTP 注入**；它标记的是 `sandbox-ctl` 的 guest runtime ready，**早于** node 的 `StateRunning`。若使用，必须单列为白盒端点，不得与黑盒状态就绪混算。

### 9.1 Kuasar 三层：router 同步、node API 异步、node-ctl 异步执行体

**router `POST /sandboxes` —— 同步到 running**

- `kuasar-components/orchestrator/internal/router/router.go` L339 `handleCreate` → L360 `routeLinkReserve(...)` → L378 `w.WriteHeader(http.StatusCreated)`（路由分派见同文件 L268／L302）。
- registry 侧 `internal/registry/route_link.go` L188 `serveReserve` → `internal/registry/reserve_operation.go` L63 `ReserveSandbox` → `internal/registry/registry.go` L469 `reserveCreate`。
- `reserveCreate` 内：`startReserve(...)`（registry.go L631；注释 L629 *"It does not wait — the channel reader signals completion when the node reports…"*）只负责选点并下发创建命令；随后 `wctx, cancel := context.WithTimeout(ctx, r.parkTimeout)`（registry.go L543；`parkTimeout` 默认 **30s**，见 `New` L197–210）并调用 **`r.waitReserveCall(wctx, call)`**（registry.go L1485）park 住等。
- `waitReserveCall` 循环直到 `reserveReadyResult`（registry.go L1524）返回 ready；该函数**只有 `rec.State == StateReady` 才返回 ready**。
- `rec.State` 变 `StateReady` 只在 `applyRoute`（registry.go L1140–1141）收到 node 上报的 **`StateRunning`** 时发生；`applyRoute` 对 `StateStarting` 显式忽略（registry.go L1111–1115，注释 *"Only running/paused are terminal route facts."*）。
- `applyLiveRoute` 在 `rec.State == StateReady` 时调用 `r.finish(...)`（registry.go L1288–1292；`finish` 定义于 L1412）关闭 `call.done`，park 结束，router 才回 201。
- **含义**：router 的 201 返回时沙箱已经是 `running`；若 30s 内未到 running，回滚预留并返回错误。router 侧**无需再轮询**。

**node API `POST /sandboxes` —— 异步（返回 `starting`）**

- `internal/api/api.go` L532 `create` → L567 `a.core.Create(...)` → L572 `writeJSON(w, 201, a.sandboxResp(sb))`。
- `internal/orch/create.go` L19 `Create` → `acceptSandboxCreate` 建 `State: types.StateStarting`（create.go L196）→ `o.acceptFreshLaunch(...)`（orch.go L282）→ 直接 `return cloneSandbox(accepted)`，**不等 running**。
- 状态机 `internal/types/types.go` L220–224：`StateStarting="starting"` / `StateRunning="running"` / …。
- 异步佐证：`api.go` L213–215 `ErrSandboxStarting = errors.New("sandbox starting")`；pause 在 running 前返回 **409**（api.go L803–804）；到 running 由后台 launch worker 完成（`CommitStartingRunning` / 置 `StateRunning`，orch.go L696、L712–747）。

**node-ctl `run-sandbox` —— 底层异步执行体**

- `cmd/node-ctl/run_sandbox.go` 头注释：*"runSandbox is the ExecStart of `sandbox-runner@<run-id>.service`: it waits until the run-id is assigned a sandbox id, fetches that sandbox's LaunchSpec over the config-socket, starts sandbox-ctl as its direct child, waits once, and reports the bounded child result before exiting."*
- 即每个沙箱对应一个 systemd unit 进程，是真正把沙箱从 `starting` 推进到 `running` 的 worker。

### 9.2 router 角色的可及性：控制面/ingress 与数据面分离

- `internal/clustercfg/config.go` 为每个角色定义独立配置：L394 `RegistryConfig`（registry member 的 unified control-plane）、L708 `RouterConfig`（注释 *"the router role's config … e2b-compatible unified ingress (cluster-router.md)"*，上游=registry、下游=e2b client ingress）、L809 `PlacerConfig`。
- 数据面节点运行 `cmd/node-ctl` 的 `conductor`／`proxy`（`cmd/node-ctl/main.go` L61–63）。
- **含义**：只有部署包含 router/ingress 角色，才谈得上请求 router；仅有数据面时，唯一入口是数据面节点的 node API。测试必须把实际拓扑作为显式条件记录。

### 9.3 `--ready-fd` 就绪信号链（存在，但不可经 HTTP 注入）

fd 由 node 自身产生并沿进程链传递：

1. orchestrator 建 readiness pipe／socket，`internal/sandboxproc/start.go` L18 `Start(cmd, vmmCgroup, ready)`：`cmd.ExtraFiles += ready`，追加 `--ready-fd=<fd>`（start.go L45）。
2. `cmd/node-ctl/taskrun.go` L346 `startSandboxChildAndReport(...)` → L353 `sandboxproc.Start(cmd, vmmCgroup, ready)`，把 fd 继续传给 `sandbox-ctl`。
3. `sandbox-ctl` 的 `readinessFDWriter`（`sandboxer/cmd/sandbox-ctl/readiness.go`）在就绪时写 `control_ready\n` 再写 `ready\n` 随后 close。
4. 读取端是 node 自己的 orchestrator：`internal/configsock/readiness.go`（`ReadReadiness`，协议封闭为 `control_ready → ready → EOF`，L14–18、L30–51）；`ready.sock` 路径为 `SandboxRunDir(runRoot, sandboxID)/ready.sock`（L21–26，注释 *"shared by the orchestrator and node-ctl … deliberately not part of LaunchSpec"*）。

**结论**：

- fd 是进程本地资源，无法放进 HTTP 请求；因此“给 node API 注入 `--ready-fd`”在远端 HTTP 场景**不成立**。
- `ready` 事件是 **`sandbox-ctl` 的 guest runtime 里程碑**，早于 node 的 `StateRunning`（后者还需 `CommitStartingRunning`、proxy route barrier、envd 探测）。用它当“创建成功时刻”会**系统性偏早**。
- 该信号当前只由 node 内部消费，未对外暴露。

### 9.4 三种取得“创建成功时刻”的手段

| 手段 | 能否注入 fd | 适用面 | 端点归属 | 可比较性 |
|---|---|---|---|---|
| ① router 同步调用 | 不适用 | 有管理面/ingress 角色 | `API_COMPLETE` ≈ `STATE_READY`（同一时刻） | 黑盒，可与 Cube/AgentENV 同步创建对齐 |
| ② node API 异步 + 轮询到 running | 否 | 仅数据面、远程 | `API_COMPLETE`（201=starting）与 `STATE_READY`（running）**必须分开** | 黑盒，产品中立 |
| ③ 同机 CLI 注入 `--ready-fd` | 是（须作为父进程） | 仅同机白盒 | 独立端点 `GUEST_READY` | 白盒；**不得**与 `STATE_READY` 并列为同列 |

### 9.5 对 benchmark 的落点

- 记录 `topology ∈ {router, node_direct, co_located_cli}` 作为一等条件。
- Kuasar router 路径：`API_COMPLETE` 已等于 `STATE_READY`，**不得**再单独报一次“额外就绪延迟”造成重复计量。
- Kuasar node 路径：`api_complete_s`（201 受理）与 `state_ready_s`（running）分别计量。
- ready-fd 路径（若启用）：新增 `GUEST_READY` 端点，显式标白盒、排除 HTTP 传输与 node 调度，并声明与黑盒数字不可直接横比。

### 附：三产品创建端点对照

| 产品 | 创建返回时状态 | 同步? | 需要轮询? |
|---|---|---|---|
| Kuasar router `/sandboxes` | 已 `running`（park 到 running，默认 ≤30s） | ✅ | 否 |
| Kuasar 数据面 node API `/sandboxes` | `starting`（已受理） | ❌ | ✅ 轮询到 `running` |
| CubeSandbox `POST /sandboxes` | 已建好并 probe 通过 | ✅ | 否 |
| AgentENV `POST /sandboxes` | 已 `Running`（`API_COMPLETE == STATE_READY`） | ✅ | 否 |

**登记落点（本轮）**：需求 `RT-42`；执行设计 `benchmark-design.md` BCD-13；测试合同 `benchmark-contract-design.md` BCT-10；节点 `test-nodes.md` §3.1/§3.2；任务 `tasks.md` 组 28。

### 9.6 Kuasar resume(connect) 同步性：静态证据冲突，待运行确认

- Kuasar **无 `/resume` 路由**；resume 走 `POST /sandboxes/{id}/connect`（body `memory`，`api.go:635`；注释 L287 "on connect/resume … then resumed"）。
- 指向**同步**：`routesync/cluster.go:295` "ConnectResult is the synchronous result of an accepted CmdConnect"；`registry.go` PAUSED 分支 `SendCommandAndWait(CmdConnect)`；`Reserve` 会 park，且 `applyRoute` 仅在 `rec.State == StateReady` 时 `finish`。
- 指向**异步**：`router.go:281` 注释 "the registry prepares the exact node-local target synchronously and the node resumes it **asynchronously**"。
- 结论：**仅靠静态阅读无法裁定** → `sandbox_bench/operation_sync._TABLE` 保守置 `('kuasar','resume') = unverified`；测量侧无论同步/异步均**轮询到 `running` 再停表**。定论待真实运行（tasks 40.5）。

## 10. 测试间环境还原：三产品预热池语义与删除/残留审计

2026-10-21 补查，纯源码审计，未运行任何沙箱。

**问题**：测试与测试之间要还原环境（删除本轮创建的沙箱），但不能一律“全删”——必须小批量多次删除、随后核验残留，并**排除预热／空闲池沙箱**。三产品是否都有预热池？如何区分测试归属的沙箱？

**结论（先行）**：

1. 三产品的“预热”都**不以可删除的沙箱记录形式**出现：Kuasar 是**预启动 runner 单元**（无 sandbox 行），Cube 是 **TAP/cgroup 资源池** ＋ idle 沙箱 `AutoPause`（暂停但仍列表），AgentENV 的 `warm-pool` 是 **ublk/网络设备级**。因此**按 sandbox 列表归属过滤**即可天然排除预热资源。
2. 归属判据是创建时写入的 **`metadata.run_id`**：三产品创建均接受 metadata；Cube 列表**原生支持 `metadata=key=value` 过滤**，Kuasar/AgentENV 返回 metadata 需客户端过滤（AgentENV 分页）。
3. 删除语义不同：**Kuasar 异步**（`deleting`→`dead`，需轮询 settle），**AgentENV 阻塞到删完**，Cube 同步。残留核验必须在删除后**重新列表**并处理异步 settle。

### 10.1 Kuasar：预热在 runner 单元层，不在 sandbox 记录层

- 预热池：`internal/orch/runpool.go` 的 `runPool` 维护 `idleRun`（预启动 systemd 单元）；`orch.go` L2730 注释 *“Idle prestarted units have no sandbox row”* —— **无 sandbox 记录，不出现在列表**。
- 列表：`internal/api/api.go` L467 `GET /v2/sandboxes` → `a.list`（L604）→ `core.List`（`orch.go` L1110），支持 `state` 过滤与 `limit`/`nextToken` 分页；`listed()`（L1374）返回 `sandboxID/templateID/state/metadata/...`。
- 删除：L468 `DELETE /sandboxes/{id}` → `a.kill`（L627）→ `core.Kill`，状态经 `deleting`→`dead`（`cluster.go` L1071/L1103），**异步**，需轮询 `GET /sandboxes/{id}` 直到 gone/dead。

### 10.2 CubeSandbox：预热在 TAP/cgroup 资源池层，idle 沙箱是暂停

- 列表原生过滤：`CubeAPI/src/services/sandboxes.rs` L92 `pub async fn list(..., metadata_filter: Option<&str>, ...)`，L120 `.filter(|sb| filter_by_metadata(sb.metadata, metadata_filter))`（L1246 `filter_by_metadata` 匹配 `key=value`）；`optional_metadata`（L1330）返回 `metadata`。
- 预热／空闲：`Cubelet/network/runtime/tap_pool.go`（idle pooled TAP）、`Cubelet/plugins/cube/internals/cgroup/pool*.go`（cgroup 池）是**资源池**，随沙箱删除回收；`README.md` L93 `AutoPause/AutoResume — idle sandboxes auto-suspend`，idle 沙箱是**暂停（仍列表）**。

### 10.3 AgentENV：warm-pool 是设备级，删除阻塞

- `crates/warm-pool/src/lib.rs`（`PoolConfig`/`PoolMaintenanceAction`）用于 `storage/ublk`（`overlaybd_target.rs` “daemon warm pool to reuse a ublk device”）与网络池（`cfg.rs` `network_pool_config`/`block_pool_config`、`startup_prewarm`）—— **设备/存储层**，不是沙箱记录。
- 列表：`X-Next-Token` 分页，metadata 返回。
- 删除：`src/orchestrator/service.rs` L1067 `delete_sandbox` → `delete_sandbox_inner`（L1089）→ `delete_sandbox_impl`（L1222），**阻塞到删完**。

### 10.4 归属判据

- 创建写入 `metadata.run_id`：三产品均支持。
- 列表过滤：Cube 原生 `metadata=run_id=<r>`；Kuasar 客户端过滤；AgentENV 客户端过滤＋分页。**只有 run_id 匹配的本轮沙箱会被删除**，预热／空闲／他轮沙箱天然排除。

### 10.5 还原删除脚本设计（见 BCD-14）

- **小批量多轮**：每轮删 `batch_size` 个、轮间 `gap`、`max_rounds` 上限；避免一次性全删冲击节点。
- **基线排除**：测试前快照 `baseline_ids`（同 run_id 的预存在实例）永不删除。
- **残留核验**：删完后**重新列表**（按 run_id），非空即 `incomplete`；异步产品需轮询 settle。
- **失败即残留**：单次删除失败记 `failures` 并计入 residue，不算成功。

**登记落点**：需求 `RT-43`；执行设计 `benchmark-design.md` BCD-14；测试合同 `benchmark-contract-design.md` BCT-11；任务 `tasks.md` 组 34；代码 `sandbox_bench/restore.py`、`Adapter.list_owned`。

## 11. 暂停时延的测量与三产品暂停同步性审计

2026-10-21 补查，纯源码审计。

**问题**：B03.PAUSE 的暂停时延怎么测？三产品的暂停是**同步**（调用返回即已暂停）还是**异步**（返回后仍需轮询）？Kuasar 的 node API 与 router API 是否不同？

**结论先行**：三产品暂停**均为同步**——暂停接口返回时沙箱已处于 `paused`，无需轮询；因此 B03 的同步暂停时延 = 暂停调用返回耗时（`API_COMPLETE` 即 `STATE_READY(paused)`）。Kuasar 的 router 暂停是**转发**到 node API（不同于 create 的 park），二者同步性一致。

### 11.1 Kuasar：node API 与 router API 均为同步

- node API：`internal/api/api.go` L471 `POST /sandboxes/{id}/pause` → L756 `a.pause` → L797 `a.core.Pause(...)`；成功后 **204**，`ErrAlreadyPaused`／`ErrSandboxStarting` → **409**。
- `core.Pause`（`internal/orch/orch.go` L1209）在 `StateRunning` 时调 `pauseSandboxLocked`（L1389）：`o.capture(...)`（L1401，执行检查点/快照）→ `o.st.CommitRunningPaused(...)`（L1413，Running→Paused）→ 清理并 `publishUpsert`。**返回即已 paused**。
- router：`internal/router/router.go` L1142 `handleSandboxVerb` 对非 DELETE 动词 `resolveRoute` 后 `forwardToNode`（L1195）——**直接转发并回传 node 状态**，不像 create 那样在 registry park（§9）。→ router 暂停同步性与 node 一致。

### 11.2 CubeSandbox：API→Master→Cubelet 全程阻塞

- `CubeAPI/src/services/sandboxes.rs` L336 `pause_sandbox` → `cubemaster.update_sandbox(pause)` 并等返回（`ensure_update_result`）。
- `CubeMaster/pkg/service/sandbox/sandbox_resume_pause.go` L51 `pauseSandbox` → L96 `cubelet.UpdateWithTimeout(ctx, ..., pauseCubeletRPCTimeout)` **阻塞**等 Cubelet；成功后 `pausesnap.Complete`。超时/失败 → RetCode 错误。→ **同步**。

### 11.3 AgentENV：await 到已暂停

- `src/api/impls/sandbox.rs` L1735 `sandboxes_sandbox_id_pause_post` → L1751 `orchestrator.pause_sandbox(...)`，成功回 **204**。
- `src/orchestrator/service.rs` L1441 `pause_sandbox_impl`：`sandbox.pause(artifact_root).await` **await 完成**，再置 `metadata.state = Paused` 并持久化。并发暂停经 `join_concurrent_pause`（L2202）等待进行中的暂停。→ **同步**。

### 11.4 对 B03.PAUSE 测量的落点

- **完成判据**：`API_COMPLETE`（暂停接口返回）即 `STATE_READY(paused)`；三产品均同步，**不需轮询**；为证据可加一次只读 `GET /sandboxes/{id}` 确认 `paused`（不计入时延窗口）。
- **需测指标**：T-01/T-02 暂停时延（同步）、R-05 状态正确性（实际 paused、非仅“不响应”）。
- **口径**：暂停时延含检查点/快照写盘（Kuasar `capture`、Cube pause-snapshot、AgentENV `pause`）；不得把网络不响应当已暂停，不把 `ErrSandboxStarting`（Kuasar 409）当成功。
- **与 create 一致的原则**：只测**同步**完成；异步入口（Kuasar 数据面 node create）必须轮询到 ready 再计为创建时延，或改用同步入口（Kuasar router）。

**登记落点**：需求 `RT-23`／`RT-24`（暂停恢复合同与来源分层）；节点 `test-nodes.md` §5 B03；逐场景 `scenario-spec.md` §4 B03；审计见 `alignment.md` §9（create）与本节（pause）。

## 12. 恢复／克隆／删除／快照的同步性审计

2026-10-21 补查，纯源码审计。同一原则：**只测同步完成**；✅同步=调用返回已达终点；⏳异步=返回仅受理，须轮询到终点。

| 操作 | Kuasar | CubeSandbox | AgentENV |
|---|---|---|---|
| 创建 | node ⏳（`starting`）／ router ✅（park→running） | ✅ | ✅ |
| 暂停（§11） | ✅（node & router） | ✅ | ✅ |
| 恢复 | ⏳（`Connect` 仅受理并调度异步 resume） | ✅ | ✅ |
| 克隆 | 无原生 fork（`B05.NATIVE` unsupported）→ 组合 capture+create | ✅（`fork` 等 fan-out） | ✅（待实证） |
| 删除 | ⏳（`deleting`→finalizer→`dead`） | ✅（`sync:true`） | ✅ |
| 快照 | `export`（先 pause 后发布；同步性待实证） | API 等 Master（待实证） | ✅ |

### 12.1 恢复

- **Kuasar**：`POST /sandboxes/{id}/connect` → `Connect`（`orch.go` L1588）：`prepareStandaloneTarget` 同步解析/鉴权后 `ensureResumeAccepted`（L1699）**仅受理**；`prepareStandaloneTarget` 注释 *“before scheduling an asynchronous resume”* → **异步**，须轮询到 `running`。
- **Cube**：`resume_sandbox`（`sandboxes.rs` L351）→ Master `resumeFromPauseSnapshot`（`sandbox_resume_pause.go` L286）→ `cubelet.Create(...)`（阻塞到 Cubelet 重建）→ **同步**。
- **AgentENV**：`resume_sandbox` 注释 *“On success the sandbox is ready for use when this method returns.”*（`service.rs`）→ **同步**；并发 resume 经 `wait_for_transition(Resuming)`。

### 12.2 克隆

- **Kuasar**：无原生 clone 路由；`B05.NATIVE` 明确 unsupported；克隆以 capture+create 组合实现（成本记 `B05.FULL`）。
- **Cube**：`fork`（`sandboxes.rs` L466）阻塞到 `cubemaster.fork_sandbox` 的 snapshot＋派生 fan-out 返回且 `results.len()==count` → **同步**。
- **AgentENV**：`fork_post`（`sandbox.rs` L1409）→ 逐 child；同步性待实证（预期等派发完成）。

### 12.3 删除

- **Kuasar**：`DELETE /sandboxes/{id}` → `Kill`（`orch.go` L1126）→ `killSandboxLocked` → `acceptSandboxDeleteLocked`（`sandbox_finalizer.go` L22）：`BeginSandboxDelete`→`deleting`，`startSandboxDeleteFinalizer` **异步** goroutine → **204 仅受理**；`deleting`→`dead` 须轮询（restore §10 的残留核验即依赖此）。
- **Cube**：`kill_sandbox`（`sandboxes.rs` L313）`DeleteSandboxRequest{ sync: Some(true) }` → **同步**。
- **AgentENV**：`delete`（`sandbox.rs` L1382）→ `delete_sandbox().await`（`service.rs` L1067 阻塞到删完）→ **同步**。

### 12.4 快照

- **Kuasar**：`POST /sandboxes/{id}/export`（`api.go` L1226）→ `ExportSandbox`（`migrate.go` L36）**要求先 pause**（`sb.State != Paused` 报错），发布不可变结果；同步性待实证。
- **Cube**：`POST /sandboxes/{id}/snapshots`（CubeAPI）→ 等 Master；同步性待实证。
- **AgentENV**：`snapshots_post`（`sandbox.rs` L1772）→ `capture_snapshot(...).await` → **同步**。

### 12.5 测量落点（统一原则）

- **同步终点**：`API_COMPLETE` 即 `STATE_READY`（或逻辑不存在／已发布）。**异步产品必须轮询到终点**再计为对应时延。
- **B04 恢复**：Kuasar 分列 `api_complete_s`（受理）与 `state_ready_s`（running）；Cube/AgentENV `API_COMPLETE≈STATE_READY`。
- **B06 删除**：Kuasar 删除时延 = 轮询到 gone/dead；Cube/AgentENV 204 即已删。
- 不得把“受理”当“完成”，不得用异步验收时延冒充同步时延。

**登记落点**：需求 `RT-24`（来源分层的创建用例）／`RT-27`（删除多终点）；节点 `test-nodes.md` §5 B04、§6 B05/B06/B08；逐场景 `scenario-spec.md` §3–§9。
