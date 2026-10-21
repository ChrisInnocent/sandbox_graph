# 资源采集、统计与离线交付

这组模块实现测试器算法和只读采集，不把合成数据、冻结 proc 树或本机测试当成三产品实测。当前宿主是 HarmonyOS / HongMeng Kernel 1.13.0；真实 Linux procfs 受控自有进程用例在这里明确 skipped。Linux 部署还需实际读取 `smaps_rollup`、`stat`、boot ID，以及选用 cgroup 时的 `cpu.stat` 权限。

## 模块接口

- `sandbox_bench.resources`：`ProcessIdentity(environment,node,boot_id,pid,start_ticks)`；`parse_proc_stat(text)` 正确处理 comm 中空格／括号；`parse_pss(text)` 严格要求唯一 Pss kB；`parse_cgroup_cpu(text)` 读取微秒并输出 CPU 秒。`sample_process(proc_root,pid,environment=...,node=...,clock_ticks=...)` 在资源读取前后核对 boot/PID/start，保留真实读取区间及失败原因；CLK_TCK 不硬编码为 100，读不到 PSS 不用 RSS 或零替代。
- `deduplicate_inventory(rows)` 保留角色／组件标签，只计一次进程地址空间；线程要求显式 `thread_group_identity`，不能把 TID 猜成独立地址空间。冲突的 sandbox/generation 拒收。`validate_counter_scopes(rows)` 拒绝进程与 cgroup、父子 cgroup 的范围重叠；这不代替部署 inventory 的证据。
- `guest_net_pss(raw_bytes,proof,domain=...,window=...)` 要求减项定义、域、窗口、全覆盖、方法、版本、来源和 bytes；缺证明为 unavailable，减项越界为 invalid，均保留 raw。没有任何内置 Guest OS 常数；Q12 未提供时不能声称已得到净底噪。
- `sandbox_bench.steady.SteadyConfig` 必填 `window_s,min_samples,max_gap_s,cohort_stability,metric_bounds,slope_bounds,max_wait_s`。`find_steady_window(samples,config)` 按时间顺序选第一个合格窗口。值界限检查所有选中点，斜率定义为该段首末点差除以真实时间；跨域、缺值、间隙和 cohort 变化不拼接。返回 `steady / not_steady / insufficient_evidence`，最后两种区分有证据但不满足界限与根本没有合格连续证据；在线等待／重试由调用方控制。
- `sandbox_bench.metrics`：`summarize_latency`、`summarize_repeats`、`summarize_quality`、`throughput`、`projection`、`cpu_segments`、`cpu_intervals`、`cpu_summary`、`resource_mean`、`cohort_pss`。公式有版本，字典结果可 JSON 化。`capture_awk_rounding` 是唯一有外部进程 I/O 的参考捕获适配器，其余统计函数不读宿主状态。

CPU 样本字段为 `t_s,cpu_seconds,identity,component,clock_domain`。`cpu_segments` 返回有效 intervals 和断点 gaps；身份更换、计数回退、缺端点、显式采样缺口不直接相减。`cpu_summary(intervals,start,end,expected_components=[...])` 必须传入已知 inventory 才能识别整个组件缺失。其 `cpu_seconds` 是各组件实际可观测 CPU 秒总和；`aligned_cpu_seconds` 是所有预期组件同时覆盖的区间积分；完整均值要求全覆盖。峰值先对齐再相加，所以 `[8,1]+[1,8]` 的总峰值为 9 核。无时钟同步证明的跨域只提供各域结果，不产生“同时总峰值”。

PSS 序列字段为 `t_s,value,cohort,clock_domain`。`resource_mean(...,max_gap_s=...)` 使用左端分段常值积分，最后一点不向后外推；显式丢样切段。`cohort_pss(['a','b'],{'a':20MiB,'b':30MiB},shared_bytes=10MiB)` 的直接均值 25MiB、含共享摊销均值 30MiB。只读到 a 时，子集均值 20MiB、coverage 0.5，完整均值 unavailable；请求数不能代替当前真实 cohort。共享标量应先经 inventory 范围去重；该函数不证明其来源。

吞吐输入为唯一完成事件 `operation_id,completed_s,clock_domain`，迟到可显式 `late=True` 单列。`fixed_deadline` 整窗右开；`natural_drain` 的最后非零桶右闭，包括恰好整秒结束的末事件，不加 epsilon。缺覆盖桶为 null，真实完整空桶为 0。active 使用区间并集，分子也限制在同一并集。调用方须传所选完成判据事件，不能拿 accepted/API 接纳或服务端内部 duration 充当完成时刻。

## 外推与旧 API

`metrics.projection(baseline_bytes,samples,requested,ready,reserve_bytes,domain=...,sample_domain=...,window=...,compatibility=...)` 的 samples 是整数 MemAvailable bytes。统一视图用有理数算成本、total、additional；部分健康、非正成本、预算导致 total<N 都明确 invalid。两域不一致为 unavailable。

兼容视图先将 A0/Amin 各自截断整数 MiB。先调用 `capture_awk_rounding(delta_mib,requested,expected_sha256=...)`，其运行真实 awk `printf %.1f`，固定 LC_ALL=C，记录二进制 SHA256、版本、输入、输出。首次捕获后应将 digest 保存到运行合同，后续传 `expected_sha256` 锁定参考；不锁 digest 的首次捕获只证明本次参考版本。当前捕获参考为 toybox 0.8.12；独立手算及捕获边界包括 `1/4 -> 0.2`, `11/20 -> 0.6`, `23/20 -> 1.1`，不使用 Python round 冒充 awk。

黄金输入 `A0=1000MiB,Amin=800MiB,N=4,R=100MiB`：兼容剩余 16、统一总量 18、可新增 14。结果带 formula/window 信息且 `measured_capacity=False`。

旧 `sandbox_bench.density.projection` 保留已有调用与输出键，委托新公式，捕获真实 awk 并保留 reference；找不到参考时只有 legacy unavailable。旧调用不提供域／窗口证据，明确标 `provenance_status=legacy_domain_unverified`，新会话应直接调用新版。旧 `observe.py`、`measure.py` 未改动。

## 持久采样

```python
from sandbox_bench.sampler import ResourceSampler
with ResourceSampler(inventory, 'samples.sqlite', run_id='run-1',
                     clock_domain='node:boot:epoch', proc_root='/proc',
                     clock_ticks=100, max_pending=256) as sampler:
    sampler.sample_once()
    result = sampler.reconcile(idempotent_sink)
    # 发压器必须检查 sampler.safe_to_submit，再决定是否允许新提交。
```

inventory 每条包含完整 identity、role、component，以及适用时的 sandbox_id/generation。单 sampler 只接受一个宿主 boot 域，冻结／远程 proc 树要传正确 CLK_TCK。SQLite 连接／所有权文件锁在采样器生命周期内持有；连接允许专用工作线程使用，采样、reconcile、pending 和 close 共用一个可重入锁，关闭会等待当前采样结束。safe_to_submit 是不阻塞事件循环的状态快照。采样 CPU 开销使用工作线程的 thread_time，不混入发压线程 CPU；proc 文件每次重开，以免持久 FD 掩盖进程退出。完整配置指纹与 spool 持久绑定，不能以另一身份恢复同一队列。

每次采样先持久写入 `interrupted_sampling` 缺样占位，再读资源并替换；中断后可 reconcile 为明确缺样。队列最多 `max_pending` 样本，溢出以单条 `sample_gap` 范围压缩保存，包括 seq/through_seq/dropped_count；积压期间不静默丢弃。队列满或 sink 失联将 `safe_to_submit=False`；采样本身只读，不产生沙箱副作用。sink 应按稳定 `sample_id=run_id:seq` 幂等，成功后才删除本地记录，确认丢失可能重放同一个 ID。sampler CPU/wall 开销和 poll/续期请求计数与被测资源分列。此实现是本地单拥有者 spool，不声称实现分布式数据库 fencing；load/图执行器必须接入 guard。

## 离线分析与封存

```python
from sandbox_bench.analysis import analyze_case, export_case, verify_case, recompute_case
records = {
    'case_id': 'case-1',
    'operations': [{'operation_id':'o1', 'status':'success', 'elapsed_s':1,
                    'method':'WINDOW', 'repeat':1}],
    'events': [{'operation_id':'o1', 'completed_s':1, 'clock_domain':'n:epoch'}],
    'window': {'start_s':0, 'end_s':1, 'window_kind':'natural_drain',
               'clock_domain':'n:epoch'},
    'resource_series': [], 'cohorts': [], 'assertions': [],
    'cleanup_ledger': [], 'raw_refs': []
}
manifest = export_case(records, 'out/case-1', raw_payloads={
    'load.json': b'{"captured":true}\n'  # 使用实际文件 read_bytes()，不重新序列化冒充原始字节
})
verify_case('out/case-1')
summary = recompute_case('out/case-1')
```

`resource_series` 支持 sampler 原始快照，或 `kind='cpu'/'pss'` 的上述纯指标行；缺样记录保留并切段。PSS 平均还必须给 `resource_max_gap_s`。`expected_components` 可传给 CPU 完整性验证。`final_cohort` 用于 quality 的最终存量，和累计完成数分列。analysis 将 repeat 键 JSON 规范化为字符串。

封存包含 case_manifest、各类原始记录、summary.json、summary.csv，以及传入 raw_payloads 的真实原始字节。raw_payloads 是 logical_name→bytes 映射，导出器自动生成 raw_refs，并把 payload 保存到 raw/<sha256>；逻辑名不用于路径，相同内容去重。manifest 将 raw 一并列入大小／SHA256 校验，验证引用可达、无孤立 raw、路径／目录／manifest 不为符号链接。缺 raw、篡改 raw、引用与实际字节不一致都拒收；默认无 raw 的空引用情况保持兼容，非空 raw_refs 不允许只封存悬空索引而不提供 payload。每个原始数组序列必须从 1 连续，合法 sample_gap 可显式覆盖一个连续丢样区间。没有 seq 的新输入由导出器编号；已有缺序列、重复或乱序拒收。文件先在临时目录写入、flush/fsync、自检，再在锁下原子移动；已有 case 绝不覆盖。manifest 保存大小／SHA256；verify 验证文件、序列、case 身份及从 raw 重算的摘要。篡改 raw 或修改窗口使摘要不匹配会拒收。摘要校验不是密码学签名；需要防攻击者同时重写 manifest/raw 时，应把返回 manifest 的摘要保存到外部可信账本。

```bash
.venv/bin/python -m sandbox_bench.analysis out/case-1 --verify-only
.venv/bin/python -m sandbox_bench.analysis out/case-1
```

默认 JSON/CSV 没有额外依赖。显式 `xlsx=True` 需要可选 `openpyxl`，缺少时明确报依赖错误，不自行安装。未在当前环境声称验证 XLSX。原始日志保存在内容寻址文件中，模型只需读取指标及引用，不能将大段 raw 送入上下文。摘要证明保存后的字节未改变；来源及真实环境证明仍由上游采集合同负责。

## 验证与任务边界

测试采用独立字面黄金值和冻结 proc 树，不从生产算法生成预期。TDD 首次运行因新模块缺失失败；补充边界曾分别因缺组件、线程去重、legacy缺参考、缺样跨接、manifest窗口改动等行为失败，再由实现修正。

专项验证命令：

```bash
.venv/bin/python -m pytest -q \
 tests/benchmark/unit/test_resources_procfs.py \
 tests/benchmark/unit/test_metrics_resources.py \
 tests/benchmark/unit/test_metrics_statistics.py \
 tests/benchmark/unit/test_metrics_quality.py \
 tests/benchmark/unit/test_steady_windows.py \
 tests/benchmark/unit/test_sampler.py \
 tests/benchmark/unit/test_analysis.py tests/benchmark/unit/test_extensions.py tests/test_density.py
```

最终专项输出：`67 passed, 1 skipped, 53 subtests passed in 3.72s`。跳过的是要求实际 Linux kernel 的自有进程检查（使用 `os.uname().sysname` 判断；当前 Python 的 `platform.system()` 会将 HarmonyOS 映射为 Linux，不能用它宣称 Linux 验证）。

最后一次完整 `.venv/bin/python -m pytest -q` 输出：`238 passed, 4 skipped, 89 subtests passed in 21.60s`。前面因并行模块尚未生成导致的收集错误在该次全量运行已消除。此记录对应本次执行时的共享工作树；后续集成修改应由主任务再验证。

任务映射（由主任务最终核验勾选，本文件不改 tasks.md）：

| 任务 | 本模块交付 | 尚需的集成／环境证据 |
| --- | --- | --- |
| 21.3 | 两公式、整数 MiB、真实 awk 指纹及阈值／部分健康／预算冲突黄金用例 | 真实同域 MemAvailable 和指定窗口输入 |
| 22.1 | proc stat/PSS/cgroup parser、进程双验、角色与线程去重 | 真实 Linux 受控进程用例在 HarmonyOS skipped；部署 inventory 来源 |
| 22.2 | 范围重叠拒绝、持久单拥有者 spool、缺样恢复、有限队列、开销与 guard | cgroup 实际部署读数／权限；执行器 guard 接线，非分布式 fencing |
| 22.3 | 真实集合子集分母、时间覆盖、首个稳态窗口、重启／缺样切段 | 在线等待调度及各场景采样窗口接线 |
| 22.4 | Guest 同域证明校验和 raw/减项/来源保留 | Q12 仍未定义，不声称完成真实净底噪 |
| 23.1 | 线性分位数、失败分类、repeat 分层、显式 raw 池化、可比性拒绝 | N08/A3 调用同一 analysis 服务的集成 |
| 23.2 | 固定秒桶、自然结束边界、覆盖／迟到、overall/active 并集 | 各场景完成事件与密度曲线引用接线 |
| 23.3 | 时间加权 PSS、CPU秒／核／对齐峰值、直接／共享／增量子集视图 | 真实资源时钟和共享范围证据 |
| 23.4 | 清单／原始记录／摘要封存、自检、离线重算、JSON/CSV、可选XLSX入口 | 来源真实性、图账本引用接线；XLSX当前未实测 |

## E01–E06 受控扩展执行骨架

`sandbox_bench.extensions` 提供 `ExtensionProfile`、`ExtensionStep`、`ActionRegistry` 和 `ExtensionRunner`。profile 固定 kind/version/profile_id/seed、最大步骤数、期限、clock_domain、拥有权作用域和有序步骤；嵌套配置发生变化也会在执行前通过指纹拒绝。默认 `run(selected=False)` 不调用动作，也不创建状态文件。缺工作量、reset 合同或适配回调返回 blocked；未知动作、非法阶段、越界 target、缺少必要补偿在执行前拒绝。

动作通过 `registry.register(name, callback, side_effect=..., compensate=...)` 登记。回调签名为 `(parameters, context)`，context 带稳定 action_id/step_id、seed、拥有的 environment/resource_ids、deadline_s/remaining_s；不接受任意 shell 命令字符串。参数和作用域复制后传入回调，防止修改运行合同。框架以有界等待调用回调和补偿，期限到达后阻止后续动作。非合作回调留在隔离的 daemon thread，未知副作用不会被自动重放，也不会与尚在执行的动作并发补偿；它不靠强行杀线程撤销可能已发生的远程副作用。

- E01：固定工作量、seed、输入／输出 SHA256，结果证据逐项匹配；没有真实 workload 合同就 blocked。
- E02：阶段 `baseline → mutate → reset → verify`，核对内容证据；`reset_mode='reset'/'recreate'` 显式分组并分别验证保持／更换 sandbox identity。
- E03：有限合法 `create/pause/resume/reset/delete/probe/audit` 状态序列，显式 `audit_every_steps`，预检审计频率；恢复保留已完成前缀和独立 segments，不把中断两侧当连续窗口。
- E04：`pressure → work → release → recovery`，可重复相邻 pressure 阶段形成有限阶梯。
- E05：`baseline → interference → measure → remove → recovery`。
- E06：`inject → observe → recover → verify`。

E04–E06 的副作用登记必须有补偿回调。每步先持久意图及补偿项，再执行并保存 receipt；补偿逆序执行，也先写 started。正常撤销步骤可用 `ExtensionStep.compensates=(prior_index,...)` 声明已完成哪些对应补偿，避免再次撤销。未声明时，最终收尾仍执行登记的幂等补偿。

普通异常代表副作用结果 unknown，状态 isolated；`run()` 或 `recover()` 不重放未知动作／补偿。适配器能证明没有歧义时使用 `KnownActionFailure(applied=...)`。外部核验后通过 `reconcile_step(index,outcome='applied'/'not_applied',evidence=...,result=...)` 或 `reconcile_compensation(index,applied=...,evidence=...)` 写入证据，再允许继续或收尾。状态文件原子替换、fsync、文件锁保证本地单拥有者恢复；不冒充跨宿主分布式 fencing。

`tests/benchmark/unit/test_extensions.py` 包含 E01 工作量／hash、E02 reset 内容／身份、E03 前置状态与恢复 prefix、周期审计，以及 E04/E05/E06 每个阶段注入确定失败的矩阵；另测未知副作用和未知补偿不会重放、越界作用域、期限及配置变更。24.1/24.2 的受控执行骨架可由主任务依据这些测试核验完成。真实工作负载、reset 产品适配、压力级别／干扰窗／故障类型、拥有权、期限及可执行补偿仍必须由用户运行合同提供，未选择时不进行真实实验。

2026-10-21 后续线程／raw 封存补强：`tests/benchmark/unit/test_analysis.py tests/benchmark/unit/test_sampler.py` 输出 `21 passed, 2 subtests passed in 0.70s`；包括线程归属／关闭串行化、线程 CPU 开销、原始文件缺失／篡改、悬空引用和 raw 目录符号链接。

2026-10-21 资格汇总补强：同一专项命令输出 `25 passed, 6 subtests passed in 0.77s`。`analyze_case` 返回独立 `qualifications`，含整体 `status`、`post_validation`、`effective_spec`；后两者均保留完整 `assertions`（包括上游提供的源规格证明与实际规格）。任一 invalid 优先，否则缺失或未验证为 unverified，所有必需断言有效才 valid。`validation_policy='none'` 且没有语义断言时，post_validation 为 not_applicable，绝不生成伪有效断言；存在 clone 隔离等语义断言仍按其状态处理。后验验证失败不改写 measured operations、成功数、延迟或吞吐。

线程／raw 修改后的中间全库运行结果是 `2 failed, 272 passed, 4 skipped, 91 subtests passed in 24.74s`，失败为 `test_actual_new_review_invalidates_run_closure_snapshot` 和 `test_identical_delivery_review_replay_does_not_dirty_new_analysis`；当时 orchestration agent 正在这两个用例的 TDD 循环。后续完整验证由主任务统一执行，此处不将该次运行写为全绿。

## 冻结输入的横向比较与事实报告

`sandbox_bench.comparison.compare_cases(directories, *, analysis_input_ref=None, expected_manifests=None, case_aliases=None)` 仅接收已封存 case 目录，逐个先 `verify_case`，再读取摘要。重复 case 身份、篡改证据、冻结摘要不匹配或冻结 case 集合不完整都会拒绝。`expected_manifests` 为 `{case_id: manifest_sha256}`；显式 `analysis_input_ref` 同时进入事实 JSON 和比较导出 manifest，服务不访问 latest，也不触发新测量。可选 case_aliases 只表示当前工作到原始测量的引用覆盖，不生成新样本；上游图负责授权和复用合同验证，详见 [分析接线](analysis-report.md)。

封存 metadata 的 `comparison` 必须含 `product`、`product_version`、`method_id`、`method_version`、`completion`、`spec`、`cache_profile`、`condition`、`window`。condition 是经核验的实验条件，window 是可比较的窗口合同，不是不同运行的绝对单调时钟坐标。缺项（包括 condition=None）保留 `missing:<field>`，不同方法／方法版本／完成端点／规格／缓存／条件／窗口保留 `mismatch:<field>`，资格未验证和无成功样本也显式不可比。同产品不同产品版本不可比；跨产品必须保留各自明确的产品版本，不能要求不同产品的版本字符串相同。接口不会推断不同宿主的时钟可同步，也不会相加其同时峰值。

结果含 `cases`（各 case 原摘要）、`facts`（逐 metric JSON pointer、原值、case_id、清单 SHA256 和稳定 fact_id）、`pairs`（comparable/incomparable 及理由）和 `product_strata`（产品／版本下保留 case／repeat），没有跨轮 P95/P99 平均或隐式 raw 池化。`conclusions` 固定为空，事实渲染器不生成性能排名或因果结论。

`export_comparison(directories, destination, *, xlsx=False, interpretation=None, analysis_input_ref=None, expected_manifests=None)` 原子生成 `facts.json`、`report.md`、`comparison_manifest.json`，可选 `facts.xlsx`。report 引用具体清单摘要、summary.json 与 metric JSON pointer，Excel 的 facts/cases/comparability 三个表保存数值和引用，外部文本强制为字符串单元格，避免公式注入。ZIP 日期及工作簿时间固定，等价输入生成相同工作簿字节。已有目录不覆盖。

Excel 依赖可选 `openpyxl`；父任务已安装 3.1.5，本模块实际使用它生成、读取并验证工作簿，不再仅验证接口 stub。没有该依赖时请求 xlsx 会明确报错，JSON/Markdown 无新增依赖。可选 `interpretation` 仅接受 `[{text, fact_refs}]`，每条必须引用现有 fact_id，单独写入标为 `unverified_model_interpretation` 的文件，不能改写事实、可比性、报告或结论。引用验证不声称证明模型文字本身正确；这里不调用模型。

这部分提供任务 10.2/10.3 的实际统计比较、Excel 和事实报告服务；A3/A4/A7/F1/F4、图账本归档与同版引用接线仍由父任务验证，不凭本模块专项测试勾选整项。

比较服务最终专项：`.venv/bin/python -m pytest -q tests/benchmark/unit/test_comparison.py tests/benchmark/unit/test_analysis.py tests/benchmark/unit/test_sampler.py` 输出 `33 passed, 6 subtests passed in 1.86s`。其中 comparison 覆盖全部不可比合同差异、condition 缺失、无成功样本、资格未验证、清单篡改、重复身份、逐轮黄金 P95、冻结 case 集合和 digest、独立解释引用，以及真实 XLSX 读写／防公式注入／字节确定性。当前不重复全库运行，由父任务在并行集成完成后统一验证。


## 最终集成补强（2026-10-21）

`ResourceSampler.update_inventory(snapshot, source_ref, *, cohort_ids=None)` 接收显式收集器快照，不扫描 PID 猜归属。更新校验 environment/node/boot 不变、PID/start_ticks 合法、角色和组件明确、sandbox 身份带 generation；支持 singular 与 plural roles/components。版本、source_ref、snapshot_cohort_ids 和 inventory_complete 随样本进入 spool。缺部分 cohort 不给完整分母；`invalidate_observation(reason)` 持久缺样并关闭提交 guard，reconcile 不能绕过未恢复的 inventory。线程 worker 与 API 通过同一可重入锁串行化；SQLite 支持 Session 专用线程。

Session 的 N04 基线等待受 resources.max_wait_s 限制；超时为 unknown，保持 observation_pending，单线程 worker 中的读取不会被谎称已停止。N07 在统一关闭 deadline 下等待采样、确认 spool 全部交付后关闭；断连保留原 spool，超时保留关闭 future，重试不会重复关闭或并发访问。N06 将明确 steady wrapper 持久为 steady.json。N08 合并已持久化 child terminal 回执，即使父请求取消而 value 丢失，成功 child 的延迟、吞吐和后验身份仍保留；冲突回执拒绝，尚未完成的已预留槽标 unknown/unsent，不能按最终 population 推测完成。

Session records 使用 case.repeat_id。后验命令按原始 stdout_bytes/stdout_sha256 比较 UTF-8 expected bytes，显示用 replacement 字符串不会改变判定。source recovery/residual、未知 stage 和待交付采样成为独立资格缺口；validation_interference 单列说明，不覆写已测成功。N10 封存 load、density、validation、targets、steady、执行 journal、资源样本、host_samples、provider_evidence、fixture_restore 的实际原始 JSON 字节。seal_inputs_sha256 绑定当前记录和所有原始字节；封存后有新 cleanup/恢复/采样证据时拒绝沿用旧 seal，须使用新证据版本。

analysis 资源输出含全局及 by_role、by_component、by_identity、by_node、by_node_role_component：PSS 时间加权均值/采样峰值，CPU 秒/均核/同刻合计峰值。一个进程只进全局一次，角色别名视图不能相加。CPU 在 PID/start/generation、缺样或时钟域切换处断段。不同节点无时钟映射时全局 unavailable；各节点只能保留明确标为 observed_span 的本地窗口。steady 分支使用保存的独立稳态窗口，筛选其边界样本，不让早期旧 PID 污染后来稳定区间。

final_cohort 明确 scope=final_snapshot、observed_at、clock_domain、inventory_source_ref、实际 observed_n/n 和 inventory_complete；只有覆盖完整才输出 direct/shared/incremental 完整均值，部分集合只保留 observed 视图。Guest 输出同样明确 final_snapshot，只接受同域同快照的 Q12 证明；不把 float 时间均值舍入成整数再扣除。缺证明时 status=unavailable 且保留原始 PSS。这里没有宣称真实 Guest 净底噪已测得。

capacity.projection 要求有限、正长度真实窗口，输出 model_version 和 window_version；None、零长度、倒序或非有限窗口不给估算值。旧 density 算术 API 保留 legacy_domain_unverified 的兼容输出，不充当合格估计。density_curve 每点引用 density.json 的实际内容 SHA256、raw 路径和 /trace/index 指针，verify_case 对原始 trace 与记录一致性复核。

BTC052 集成 `tests/benchmark/integration/test_large_evidence.py` 实际运行 N00–N10、A3/A4/A7/F1/F4 图链、离线重算、真实 XLSX、事实报告和归档；注入抛异常 ModelSpy、10000 行带唯一标记的原始日志，验证模型调用数为零、每个 NodeResult 有界且不含原文、封存 raw 字节可取回、重算结果稳定。它采用受控适配器，不声称 HarmonyOS 已完成真实 Linux procfs 观测或外部产品性能验收。

新增回归先复现：父取消漏成功 child、资源角色视图缺失、generation 跨接、旧 PID 污染稳态、跨节点视图缺失，再验证修复。最新 Session/analysis 单独专项为 `41 passed, 6 subtests passed in 2.15s`。最终本组联合验证结果见下条记录；父任务仍需最终全库验证。任务 21.3、22.3、22.4、23.1–23.4、24.1/24.2 的实现及受控验收已有对应覆盖；22.1 的真实 Linux 验收仍明确未验证，不能以冻结 proc 树或本机 HarmonyOS 冒充。

最终本组联合命令覆盖 resources_procfs、metrics_resources/statistics/quality、steady_windows、sampler、analysis、extensions、comparison、session、旧 density 和 integration/test_large_evidence，输出：`116 passed, 1 skipped, 59 subtests passed in 26.47s`。唯一 skipped 仍为需要真实 Linux kernel 的受控 procfs 检查；本次未运行全库，也未提交或切换分支。
