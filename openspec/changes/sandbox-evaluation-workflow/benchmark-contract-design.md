# 测试执行代码的契约测试设计

## Context

被测对象是 [benchmark-design.md](benchmark-design.md) 的测试器代码，而非候选沙箱本身；与 [ORC 编排测试](orchestration-test-design.md) 分开。本文定义拟实现的测试代码目录、夹具、独立 oracle、错误注入与产物，不能把代码契约通过称为沙箱性能合格。BCD 指执行设计章节，BTC 为稳定契约测试 ID。

## Goals / Non-Goals

**Goals:** 证明发压节奏、完成判据、计数、资源归属和公式按合同执行；检测异步接纳冒充完成、伪冷态、缺测补零、错分母和恢复重复操作；为每种 B/E 方法关联实现任务。

**Non-Goals:** 不复制被测函数作为 oracle，不靠 mock 验证真实产品行为，不预设真实性能数值。不另造递归无穷的“测试测试代码”：用独立已知值、参考协议服务、变异反例和真实环境抽查验证契约测试的辨错能力。

## Decisions

### BCT-01 测试代码结构与层级

| 目录（拟新增） | 被测范围 | 证据边界 |
|---|---|---|
| tests/benchmark/unit/ | schema、纯 parser、formula、状态 reducer、选择断言 | 仅算法和结构 |
| tests/benchmark/protocol/ | 实际 HTTP 客户端、真实 TCP 流、各候选报文映射 | 受控服务协议，非产品实测 |
| tests/benchmark/scenarios/ | 真实 LoadEngine／Scenario + ScriptedSandboxService + ManualClock | 测试器行为和精确发压轨迹 |
| tests/benchmark/recovery/ | 真实 runner 子进程硬杀、持久日志与资源账本 | 恢复与副作用次数 |
| tests/benchmark/linux/ | 真实 Linux procfs／cgroup 的受控自有进程，冻结 proc 树夹具 | 测量读取与单位；无宿主能力时 blocked |
| tests/benchmark/live/ | 已授权环境上的真实三产品方法矩阵 | 只对实际产品／版本／环境／方法成立 |

pytest 测试按 BTC ID 标记并输出机器可读索引，文件名描述被测模块；fixture 文件存 `tests/fixtures/benchmark/`，含来源、digest、schema_version、预期值和为何独立。每次运行保存 `test_id,case_variant,layer,versions,seed,input_refs,expected_ref,assertions,outcome,actual_refs`。缺依赖／真实环境时 skipped 或 blocked，不能写 pass。

API fixture 以审计的 schema 和源码分支手工编码，不通过生产 adapter 反序列化后再当预期；HTTP server 独立记录每次请求及资源变化，可脚本化延迟头／body／终态、关闭连接、返回部分 fork 和不完整分页。ManualClock 显式推进“服务事件时刻”和“观察到达时刻”，不靠 sleep 猜时序；真实 socket deadline 另外测实际 wall 上界。

### BCT-02 契约格式与独立基准

每项合同用 `{id, requirements, design_refs, subject, given, actions, oracle, negative_variants, evidence_required}` 表达。schema 由测试基础设施校验，不能用模型判断测试结果。数值用精确整数 ns/bytes/ticks 或有理数 oracle；浮点展示使用明确误差，不给任意大 epsilon。引用样例数据只能标 synthetic 或 captured，captured 需要部署版本及脱敏来源。

以下黄金数据必须写成独立 fixture：

- **滑窗** Q=5,C=2，服务耗时按请求依次 [1,3,2,1,1] 秒；无额外开销，提交时刻 [0,0,1,3,3]，完成 [1,3,3,4,4]。同刻事件按 request_index 稳定处理，不因 gather 变成 [0,0,3,3,5]。accepted 在提交后立即到达，不能释放状态槽。
- **分批** 同 Q/C，B=2,gap=2；提交 [0,0,5,5,9]，完成 [1,3,7,6,10]，包含两次 gap，自然结束 natural_drain 的总 wall=10，含最后终结事件，吞吐0.5/s；末批后无额外 gap。
- **吞吐桶** 固定截止窗口 fixed_deadline=[0,2.5)，唯一完成 [0.2,0.7,1.2,2.2]；count [2,1,1]，duration [1,1,0.5]，rate [2,1,2]，整体1.6/s；2.5 的完成列 deadline_outside，重复1.2事件不加数。断流桶为 null；完整无事件桶为0。
- **外推** 同域 A0=1000MiB,Amin=800MiB,N=4,R=100MiB：原 d=50.0，原剩余预测16；统一总预测18、可新增14。仅3/4健康则统一预测 invalid，原视图仍标 requested 分母及限制。整 MiB 截断与 awk 舍入边界另有捕获参考输出。
- **PSS** 两个存活实例分别20/30MiB，平台共享10MiB：直接均值25MiB，含共享摊销30MiB；请求数5不得作分母。只观测到一个实例时只能报该子集20MiB及50%覆盖，整体均值 unavailable。
- **CPU** 同范围两组件区间核占用 [8,1] 与 [1,8]：总峰值9，不是16。一个实例2秒增3 CPU秒→1.5核；缺起点或PID启动身份变更不可相减。
- **分位数** 成功耗时 [1,2,3,4]，另有1个超时：线性 p50=2.5,p95=3.85,p99=3.97，n_success=4,n_attempts=5；超时不是第5个等于截止的成功值。

### BCT-03 输入、传输、完成与适配契约

| ID | Given / When | 独立 Then 及负例 | 代码归属 |
|---|---|---|---|
| BTC-001 | 完整／缺项／未知字段／bool 数字／NaN 合同 | 缺单位、预算、完成判据时在网络调用前拒绝；无性能阈值可测但不判 pass | unit/test_contracts.py |
| BTC-002 | 2v2G 未定；明确2GiB；Cube 2000m；模板规格不符 | 不猜G；明确单位精确换算；requested/effective/measured 分开 | unit/test_contracts.py |
| BTC-003 | split body、慢滴、超限、断流、redirect、错误body含token | 总deadline覆盖完整流，headers/body时刻分列，秘密不泄漏，写请求不自动重发 | protocol/test_transport.py |
| BTC-004 | Connect 分片跨frame/base64/UTF8；end-stream错误或缺end | 增量保留原bytes/hash，完整end/exit/stream终结才成功；start/HTTP200不足 | protocol/test_envd.py |
| BTC-005 | create201→starting→running，命令失败 | STATE_READY有效并保留命令失败；一个create，槽位不在201释放 | scenarios/test_completion.py |
| BTC-006 | EXEC_COMPLETE，命令延迟；STATE后批后验证 | EXEC终点包含真正命令；STATE计时不含后验；没有等待未执行命令的环 | scenarios/test_completion.py |
| BTC-007 | 状态unknown/终态失败、跨epoch、poll延迟、timeout后成功 | 不生成负时延或精确内部时间；超时及迟到分列，不静默转成功分位数 | scenarios/test_completion.py |
| BTC-008 | Kuasar terminal kind/profile/sid/run错配、重复、失败、缺起点 | 只同一操作且成功且起点有效计内部duration；采集到达不替换服务端时间 | unit/test_logs.py |
| BTC-009 | Cube create与detail的clientID不同、列表忽略cursor | placement用核验后的detail/runtime；完整性不足不能称密度封顶，无无限翻页 | protocol/test_cube.py |
| BTC-010 | Cube build202→building→ready/error，cpu=2000 | 按真实job状态等候，用返回templateID；不传2当2vCPU，失败保留build成本 | protocol/test_cube.py |
| BTC-011 | Kuasar pause/export/connect；metadata资源字符串 | keepSource显式，内存／磁盘分支；detail全局规格不代实例证据，201非ready | protocol/test_kuasar.py |
| BTC-012 | Kuasar注册tid/bid→trigger→ready返回新持久tid | 用新ID创建，重入只轮询原job；409不是创建成功，builder未部署blocked | protocol/test_kuasar.py |
| BTC-013 | AgentENV两页、重复cursor、metadata特殊字符、TTL更新 | header翻页并去重，编码不串过滤；续期是从当前时间计算，未验证codec拒绝 | protocol/test_agentenv.py |
| BTC-014 | 三产品同方法、各自不同内部阶段／资源字段 | 同合同完成边界，不能按产品快捷返回；能力unsupported与未实现分开 | protocol/test_adapter_contract.py |
| BTC-015 | host/component/process重启，模板cache/spec漂移 | 前置证据失效，缺必要采集权限只影响相应指标；不安装或改规格掩盖差异 | scenarios/test_fixtures.py |

### BCT-04 请求算法及基础场景契约

| ID | Given / When | 独立 Then 及负例 | 代码归属 |
|---|---|---|---|
| BTC-016 | BCT-02滑窗黄金序列 | 精确提交／完成序列、max active=2、Q=5；201不补槽 | scenarios/test_load.py |
| BTC-017 | BCT-02分批；B>C；余数批；失败 | 全批终结后gap，尾批无gap，失败不补成功数，实际并发不超C | scenarios/test_load.py |
| BTC-018 | 开环到达高于处理能力，queue/drop有限 | scheduled/actual/drop独立，目标λ未达到明确标记；不改闭环且不无限队列 | scenarios/test_load.py |
| BTC-019 | slot分配前后、submission_started、响应丢失各切点 | 预算不重置；同操作不会再次无条件POST；unsent/unknown区分 | recovery/test_budget.py |
| BTC-020 | 超时释放客户端槽但远端继续运行 | unknown_external达到上限停止新增；不是无限超时补槽 | scenarios/test_load.py |
| BTC-021 | B01模板／快照、preflight和warmup污染 | 方法来源/内容独立；warmup不混正式样本，冷态不能无证据标cold | scenarios/test_create.py |
| BTC-022 | B02独立FULL／共享build K子／PREPARED | 总成本包含全部必要准备，build只计一次；缺阶段事件不猜分段 | scenarios/test_create.py |
| BTC-023 | B03 pause后只读观察，断网与真实paused | 不触发autoResume；网络失败不当暂停；恢复后验不算B04性能样本 | scenarios/test_lifecycle.py |
| BTC-024 | B04内存token／磁盘文件／重启进程同PID | 内存连续检查不能靠同PID或从文件重建token；DISK不要求内存继承 | scenarios/test_lifecycle.py |
| BTC-025 | B05 fork201内1成功1失败、源需恢复、SDK清兄弟 | child逐项保留；原生fork原子预留K个child槽＋1个父请求额度，K>C/余额<K拒绝；响应丢失保留K个未知额度，不能补成功；父/child吞吐不同；FULL完整时间含必要源恢复 | scenarios/test_clone.py |
| BTC-026 | clone对私有可写区写唯一值，外部卷声明共享 | 源和兄弟私有值不变，共享卷不套私有断言；未知部分不假通过 | scenarios/test_clone.py |
| BTC-027 | B06已删ID、401、404、残留mount、共享snapshot | 不以二次DELETE补首次时延；鉴权/网络非消失；共享产物不误删 | scenarios/test_lifecycle.py |
| BTC-028 | B07 FIRST预检已执行；固定输出截断；非零退出 | 失去FIRST资格；byte/hash/exit全验，预期错误不进成功命令吞吐 | scenarios/test_command.py |
| BTC-029 | B07 deadline后进程继续；重入 | 有限对账与清理，无重发；部分输出和失败保留 | recovery/test_command.py |
| BTC-030 | B08仅mmap未触页、远端未ready、本地可恢复 | 不声称已触页/已发布；restore后验时间另列，logical/physical分列 | scenarios/test_snapshot.py |
| BTC-031 | B09零实例基线前误建cohort、paused卸载证据缺 | 拒绝伪基线；缺读取不当零；有效卸载可记对应项零 | scenarios/test_noise.py |
| BTC-032 | GROWTH_LIST有新增20旧丢15；净增5，两批低于10 | 连续净增长判停，非新增20；完整列表、域变化及预算下界输出 | scenarios/test_density.py |
| BTC-033 | GROWTH_READY与LIST分歧；中途增长恢复 | 各自stop状态独立，恢复增长重置stall；不从同轨迹冒充两轮完成 | scenarios/test_density.py |
| BTC-034 | BCT-02外推及d=0.5／负成本／部分健康 | 原剩余16与统一总18分列；阈值、精度、invalid原因一致 | unit/test_capacity.py |
| BTC-035 | 固定Q不同C/WINDOW/BATCH，最后实例退出 | 创建数保留，最终cohort减少；性能、PSS、CPU同时存在，不叫最大容量 | scenarios/test_density.py |
| BTC-036 | SLO_VERIFY无阈值／已有实例无新建 | 缺阈值blocked；创建吞吐N/A；不自动二分寻峰或增大N | scenarios/test_density.py |

### BCT-05 资源、统计、证据及恢复契约

| ID | Given / When | 独立 Then 及负例 | 代码归属 |
|---|---|---|---|
| BTC-037 | proc stat名字含空格/括号、PSS kB、CLK_TCK不同 | 正确定位字段／单位；非法或重复必需字段拒绝；不硬编码100 ticks | unit/test_procfs.py |
| BTC-038 | PID重用、读取中退出、权限失败、只有RSS | boot/start身份双验、缺测带reason；不换RSS或当0 | linux/test_process_sampling.py |
| BTC-039 | 多角色同进程、cgroup父子、shared控制面 | 去重聚合；进程+cgroup不重复和；控制面不重复分摊 | unit/test_inventory.py |
| BTC-040 | CPU负delta／缺端点／同域真实3秒增量 | 生命周期分段；有效区间按实际dt；丢尾值标coverage | unit/test_resource_metrics.py |
| BTC-041 | [8,1]+[1,8]、跨节点无同步证据 | 同域总峰值9；跨域不造同时总峰值；各组件峰值仍保留 | unit/test_resource_metrics.py |
| BTC-042 | 2实例PSS20/30、请求5、只观测1个 | 25MiB直接均值或受限子集20MiB；禁止资源子集除完整N | unit/test_cohort.py |
| BTC-043 | 先不稳后稳、样本缺口、cohort变化、等到max_wait | 第一个合格窗口；不挑后面最好窗口；缺证据与未达稳态分开 | unit/test_steady.py |
| BTC-044 | Guest减项无定义、跨域、比raw大、合法同域证明 | 前三净值unavailable/invalid且保留raw；合法才相减；不减配置内存 | unit/test_resource_metrics.py |
| BTC-045 | BCT-02吞吐与边界、重复、日志回溯、断流 | fixed_deadline右开、natural_drain末桶右闭分别精确计数，整秒结束不加epsilon，零时长N/A；缺测null、真实0；迟到和最终存活不替代完成 | unit/test_throughput.py |
| BTC-046 | 不规则采样、最后桶、gap及active并集 | 按真实时长加权；active分子同窗，未覆盖时间不外推 | unit/test_resource_metrics.py |
| BTC-047 | BCT-02分位数、全失败、混方法、不同repeat | p95=3.85，n与失败保留；全失败N/A；不能平均P99，跨方法拒绝聚合 | unit/test_statistics.py |
| BTC-048 | 原始证据被改、缺seq、不可读manifest、假成功摘要 | 交付拒绝／相关指标不足，hash不能替代来源证明 | unit/test_evidence.py |
| BTC-049 | 硬杀在create响应前、已知ID未写receipt、seal前后 | 重启先对账；无重复创建，缺计时标缺测，连续性不伪造 | recovery/test_runner.py |
| BTC-050 | 控制器退出、sampler退出、spool满、数据库失联 | 有限授权运行；无新无账本提交；丢样明确，恢复序列去重 | recovery/test_runner.py |
| BTC-051 | cleanup部分失败、迟到child、共享源、另组资源 | 只清拥有对象；残留可交付但不写clean；后续对账仍保护冲突域 | recovery/test_cleanup.py |
| BTC-052 | 全流程模型Spy抛异常、日志10000行 | 确定性执行无模型调用；节点仅收有界摘要和引用 | scenarios/test_delivery.py |

### BCT-06 扩展、交叉移植和真实验收

| ID | Given / When | 独立 Then 及负例 | 代码归属 |
|---|---|---|---|
| BTC-053 | E01固定工作量、E02 baseline/reset/身份 | hash/工作量匹配；reset/recreate分组，缺适配blocked | scenarios/test_extensions.py |
| BTC-054 | E03同seed混合序列，中断后继续 | 生命周期前置合法、预算有限，连续窗口分段，丢失/重复保留 | scenarios/test_extensions.py |
| BTC-055 | E04/E05/E06每个动作后失败 | 已登记压力/干扰/故障有补偿及所有权，未选择无注入，恢复unknown隔离 | recovery/test_extensions.py |
| BTC-056 | 三产品×GROWTH_LIST/PROJECTION/WINDOW/BATCH矩阵 | 相同算法与完成合同，来源标签及兼容偏差；无适配显式覆盖项 | scenarios/test_cross_product.py |
| BTC-057 | 固定官方源码／awk参考视图，与统一算法差异 | 兼容数值匹配黄金文件；安全修正标差异，绝不运行全局reset | unit/test_legacy_views.py |
| BTC-058 | 硬杀恢复的真实图＋测试runner＋统计交付 | D11 operation、N阶段、指标原始证据一致，owner切换无双提交 | recovery/test_graph_bridge.py |
| BTC-059 | 有凭据/版本/合同的真实候选最低规模矩阵 | 每支持方法有功能断言和原始证据；未适配／未知／没跑逐项区分，不能以fixture通过升级状态 | live/test_capabilities.py |
| BTC-060 | 已验证功能后的真实密度/资源联合测量与独立抽查 | 核对实际ID、完成事件、PSS/CPU域和重算一致；发生器饱和/缺测使相应结论受限，实际数值无预填 | live/test_measurements.py |

### BCT-07 需求、方法、任务追溯

| 需求／方法 | BCD | BTC | tasks 组 |
|---|---|---|---|
| RT-04/06/18/19/23/35；N00–03 | 01–04 | 001–015、021 | 15–18 |
| RT-07–12/22；完成与命令 | 04/07/10 | 003–008、014、023–030、047 | 16–20 |
| RT-14/34/36；所有密度发压 | 06/08/10 | 016–020、032–036、045、056–057 | 18/21/23 |
| RT-16/24–29；B01–B08 | 05/07 | 021–030 | 17/19/20 |
| RT-13/20/21/31/32/37；B09/B10资源 | 08–10 | 031、037–046 | 21–23 |
| RT-03/33；E01–E06 | 11 | 053–055 | 24 |
| WF-06–08/20–24/31/36；交付／恢复 | 01–03/10 | 019、048–052、058 | 15/25 |
| RT-01/02/07/08/30；跨候选实际证据 | 04–10 | 056–060 | 26 |

B00–B10 的所有子方法以场景文件中的参数化 case_id 展开，不能只测一个示例就声称全方法覆盖；E01–E06 按其显式选择与适配状态记录。原 ORC-059–064 只验证图透传，BTC 验实际算法，两者都保留。

### BCT-08 变异与准入

必须能捕获的变异：201释放槽、WINDOW误写gather、失败补到成功Q、尾批后加gap、net_growth改新增成功、requested当真实存活、去掉start_ticks、PSS换RSS、各组件峰值相加、缺样填0、Guest减配置内存、超时当成功时延、恢复新建operation、cleanup删全环境、ready使用注册tid、缺end照算命令成功。

实现前独立 fixture/oracle 先落地；每组任务同时交付 tests 与模块文档。核心算法测试通过后再做真实Linux及产品 smoke。真实环境无法提供时实现和离线验证可继续，但 BTC-059/060 与 environment_verified 保持未完成，不据此声称完整选型测试完成。

### BCT-09 来源产物与构建场景的独立测试合同

对应 [benchmark-design.md](benchmark-design.md) BCD-12 与 tasks 组 27。oracle 不得调用被测函数生成期望值。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-09-SRC | `resolve_source` | 测试内字面量表 | 固定 `(product, parameter_name, declared_kind)` → 期望 `artifact_kind`／`launch_path`；缺版本→`unverified`；名为 template 实为 snapshot → `snapshot_manifest` |
| BCT-09-ROUTE | `method_for_source` | 决策表 12.2 的独立副本 | 全表覆盖；未覆盖组合→`ScenarioBlocked`；同族冲突不静默 |
| BCT-09-BUILD-READY | `build_source` | 脚本化 job status 序列 fixture | `(building,ready)`→`terminal='ready'`，`artifact.kind` 正确，trigger 恰好一次 |
| BCT-09-BUILD-ERROR | 同上 | `(building,error)`／`(building,unknown)`／超时 | `terminal='error'`，无伪造 ready；证据封存 |
| BCT-09-BUILD-LOST | 同上 | 写响应丢失脚本 | 重启后按 handle 对账，trigger 不重复；`unresolved_external` 正确 |
| BCT-09-COST | 成本归集 | 手工预算账本 | 共享 `producer_ref` 两用例→构建只计一次；`reuse_of` 记录 |
| BCT-09-SNAP | `B01.SNAPSHOT.*` | 固定 resolution | DISK／MEMORY 得不同 method_id，分析层不合并 |
| BCT-09-REJECT | `CaseContract` 校验 | 非法用例 fixture | B01. 缺 kind／path 报错；MEMORY 配 disk_restore 报错；TEMPLATE 配 snapshot_manifest 报错 |

层级与目录：`tests/benchmark/unit/test_source_resolution.py`、`tests/benchmark/contract/test_build_scenario.py`、`tests/benchmark/integration/test_b00_build_reentry.py`（三类独立 marker，不共用数据库／故障夹具）。受控轨迹通过只提升代码契约状态，不声称真实环境验证。

### BCT-10 创建端点拓扑与就绪信号的独立测试合同

对应 [benchmark-design.md](benchmark-design.md) BCD-13 与 tasks 组 28。oracle 不得调用被测函数生成预期值。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-10-TOPO | `resolve_endpoint` | 测试内字面量表（topology × 产品 → endpoint／synchronous） | Kuasar router→同步；Kuasar node_direct→异步需轮询；Cube／AgentENV→同步；缺证据→`unverified` |
| BCT-10-SYNC-ROUTER | 同步入口 | 一次调用即 running 的脚本序列 | `endpoint_requires_poll==False`；不得再报一次就绪延迟；`api_complete==state_ready` 为同一时刻 |
| BCT-10-ASYNC-NODE | 异步入口 | `(starting,…,running)` 轮询序列 fixture | 受理记 `api_complete_s`，轮询到 running 记 `state_ready_s`；二者分列；不得把受理当完成 |
| BCT-10-READY-FD | `read_ready_fd` | 独立 `control_ready\nready\nEOF` 字节流 | 正确解析；缺 `control_ready`、超长行、ready 后多余字节、超时均判 `unverified`，不补零 |
| BCT-10-BLOCKED | 拓扑核验 | 无管理面角色的声明 fixture | `status=='blocked'`，不静默回退 node_direct |
| BCT-10-COMPARABILITY | `endpoint_comparable` | 手工端点对 | 异 `endpoint_kind`→False；`guest_ready` vs `state_ready`→False；同黑盒端点→True |

层级与目录：`tests/benchmark/unit/test_endpoint_resolution.py`、`tests/benchmark/contract/test_ready_fd.py`（独立 marker，不共用数据库／故障夹具）。受控轨迹通过只提升代码契约状态，不声称真实环境验证。

### BCT-11 测试间环境还原的独立测试合同

对应 [benchmark-design.md](benchmark-design.md) BCD-14 与 tasks 组 34。oracle 为手写的假列表／假时钟，不调用被测函数生成预期。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-11-BATCH | `restore_environment` | 手写 7 个归属实例，批大小 3 | 轮次 `[3,3,1]`，非一次性全删 |
| BCT-11-WARM | 预热／基线排除 | 假列表含无 run_id 的预热、同 run_id 基线 | 两者均不被删；不因此判失败 |
| BCT-11-ASYNC | 异步 settle | 删除后实例仍列出 N 轮 | 未 settle → `incomplete` ＋ `residual_ids` 保留 |
| BCT-11-FAIL | 单删失败 | 假列表指定某个 id 删除失败 | `failures` 记录身份，计入残留，`incomplete` |
| BCT-11-SCOPE | `select_batch` | 字面量集合 | 只本轮归属、排除基线、批大小上限 |

层级与目录：`tests/benchmark/unit/test_restore.py`（独立 marker `restore`，不共用数据库／故障夹具）。受控轨迹通过只提升代码契约状态，不声称真实环境验证。

### BCT-12 背景条件合同与 B00 背景密度/指标的独立测试合同

对应 BCD-16 §16.4、BCD-12 §12.9/§12.10 与 tasks 组 36/37。oracle 为手写字段表／假 adapter／注入采样序列，不调用被测函数生成预期。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-12-COND | `validate_conditions` | 字面量字段表 | 缺 R 字段→合同错误；O 缺省→`not_selected`；未选不默认取值 |
| BCT-12-ACTUAL | 声明 vs 实现 | 声明 N=5 实际 N=3 | 标条件受限，不当达成 |
| BCT-12-BGDENSITY | `BuildScenario` 背景 | 假 adapter 预创建 N | 背景 N 个被创建；构建在背景存在下；增量资源=total−baseline；背景不足→blocked |
| BCT-12-BUILDMETRIC | `build_source` 指标 | 注入采样序列 + 产物元数据 | `resource_peak`/`artifact.size_bytes`/`cache_state` 被填充；缺样本 `unavailable` 不补零 |

层级与目录：`tests/benchmark/contract/test_conditions.py`、`tests/benchmark/contract/test_build_density.py`（独立 marker，不共用数据库／故障夹具）。受控通过只提升代码契约状态，不声称真实环境验证。

### BCT-13 操作同步/异步终点映射的独立测试合同

对应 BCD-17、RT-46 与 tasks 组 40。oracle 为手写字面量表；不调用被测函数生成预期。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-13-SYNC | `completion_plan` | 字面量 product×operation 表 | 暂停同步、恢复异步（Kuasar）、删除异步（Kuasar）/同步（Cube/AgentENV）；未覆盖→`verified=False` |
| BCT-13-ROUTER | Kuasar create 入口 | router vs node_direct | router 同步、node 异步，分别记录 |
| BCT-13-POLL | `await_operation` | 假 adapter 状态序列 | 异步轮询到 `ready_state` 停表；同步不轮询 |
| BCT-13-GONE | 删除轮询 | 假 adapter 消失序列 | `ready_state='gone'` 轮询到逻辑不存在 |

层级与目录：`tests/benchmark/unit/test_operation_sync.py`（独立 marker，不共用数据库／故障夹具）。受控通过只提升代码契约状态，不声称真实环境验证。

### BCT-14 创建时延口径的独立测试合同

对应 BCD-18、RT-47 与 tasks 组 41。oracle 为手写分时/计数表，不调用被测函数生成预期。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-14-HEADER | 同步创建停表点 | 假 transport：响应头在 T1、body 在 T2（T2>T1） | `create_latency==T1`；`body_read_s==T2-T1`；完成时延不得用 T2 |
| BCT-14-REUSE | run 级连接复用 | 假连接计数器 | 同一 `(scheme,host,port)` 并发 N 请求只建连接 ≤ `max_per_host`；run 后 `close()` |
| BCT-14-WARMUP | 预热排除 | 字面量：warmup=3、实测=10 | 统计 `n_attempts==10`；预热轮次被记录且不入分位 |
| BCT-14-CALIBER | 口径版本与可比性 | 两个 `caliber_version` | 同版本可比；跨版本→不可比；缺口径→`unverified` |
| BCT-14-POLL-DENSITY | 异步轮询密度 | 轮询间隔字面量 | 过粗（相对 ms 级创建）→ 拒或 `unverified`（接 RT-47 / #5） |

层级与目录：`tests/benchmark/unit/test_create_caliber.py`（独立 marker；手动时钟/计数器，不用真实 sleep 猜时序）。受控通过只提升代码契约状态，不声称与官方基准已完成对比。

### BCT-15 资源观测合同与缺失显式化的独立测试合同

对应 BCD-19、RT-48 与 tasks 组 50。oracle 为手写字面量用例与假采样器，不调用被测函数生成预期。

| 编号 | 被测 | 独立 oracle | 关键断言 |
|---|---|---|---|
| BCT-15-REQUIRED | 未声明 `resources` 的需测方法 | 字面量用例（`resources=None`） | 结果 `resource_gaps` 含 `resource_observation_not_requested`（不阻断 cleanup）；不声称已测 PSS/CPU |
| BCT-15-BLOCKED | 声明 `resources` 但 `sampler=None` | 字面量：`resources` 有、`sampler` 无 | `blocked/resource_sampler_required`，不静默跳过 |
| BCT-15-INVENTORY | 装配期校验 | 假 inventory：缺 `boot_id`/`processes`/scope 不符 | 拒绝（`ValueError`/`blocked`），不构造采样器 |
| BCT-15-NOZERO | 空资源序列 | 字面量：`resource_series=[]` | `unavailable`；数值为 `None`，**不得为 0**、不得从夹具推默认 |
| BCT-15-WINDOW | 短寿命窗口 | 字面量：样本数 `< min_samples` | `insufficient_samples`/未验证；不补样 |
| BCT-15-CALIBER-SPLIT | 净底噪 vs 每沙箱开支 | 字面量：raw=100、deduction=20 | `net_pss_bytes==80` 与 `raw_pss_bytes==100` 分列；缺证明→`net_pss_bytes=None` 而 raw 保留（`test_resources_procfs.py`） |

层级与目录：`tests/benchmark/unit/test_resource_contract.py`（独立 marker；手工时钟与假采样器，不用真实 sleep 猜时序）。受控通过只提升代码契约状态，不声称已在真实环境采集到资源。

## Risks / Trade-offs

- 控制服务与产品都可能符合错误理解 → API fixture 关联固定源码证据，真实环境抽查形成独立证据。
- 测试预期错误 → 小型手算数据、官方公式参考、变异反例与人工可读trace相互校验。
- 时间测试抖动 → 算法用ManualClock，网络deadline用有界真实服务分别检验，不混成毫秒性能断言。
