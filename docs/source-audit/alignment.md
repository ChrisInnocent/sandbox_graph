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
