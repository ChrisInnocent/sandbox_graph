# 显式配置与可执行入口

`configured_session(case, config, folder)` 只注册本仓库已有的 provider 类型，不从配置动态 import Python，不扫描 PID 来猜角色，不生成来源或环境证明。CLI `case` 已调用该入口；配置文件与 case 分别冻结摘要，改变配置或 repeat_id 后不能接管原目录。

## 命令

在项目虚拟环境中，三个入口的 `--help` 已实际执行通过：

```bash
.venv/bin/python -m sandbox_bench.runner --help
.venv/bin/python -m sandbox_bench.guest_probe --help
.venv/bin/python -m sandbox_bench.observe --help
```

提供真实、完整的 case/config 后执行；路径是调用者选择的本地路径，以下名称不对应仓库内置的环境：

```bash
.venv/bin/python -m sandbox_bench.runner case \
  --case /absolute/operator-input/case.yaml \
  --config /absolute/operator-input/config.yaml \
  --output /absolute/evidence/work-repeat-1
.venv/bin/python -m sandbox_bench.runner verify /absolute/evidence/work-repeat-1/evidence
.venv/bin/python -m sandbox_bench.runner recompute /absolute/evidence/work-repeat-1/evidence
```

旧 `run --scenario create` 仍是有限 WINDOW 创建入口，`cleanup` 只清理相应 run 账本归属资源；密度与生命周期使用 `case`。`observe --proc-root /proc` 只读一次宿主观测，不代替有窗口、inventory 与持久 spool 的资源采样。Guest probe 必须明确部署进目标 guest，并将每个 guest 的 Unix socket 转发为 `<socket_directory>/<sandbox_id>.sock`，此模块不代为安装或创建转发。

## 配置绑定

下面是配置结构示例；尖括号项必须替换成实际证据与路径，不可直接用作实验输入。已有 config 的 product/api_url/data_url/source/create_body、环境/来源/effective-spec 证据仍必填。凭证通过已有 `api_key_env` 配置引用，不写入 wiring。

```yaml
wiring:
  version: wiring.v1
  environment_ref: <与 CaseContract 完全一致>
  sampler:
    kind: collector_procfs
    inventory_path: /absolute/input/initial-inventory.json
    inventory_sha256: <上述文件原始字节的64位小写SHA256>
    snapshot_path: /absolute/collector/current-inventory.json
    proc_root: /proc
    clock_ticks: <实际目标宿主CLK_TCK整数>
    max_pending: 256
  steady:
    metric: pss_bytes
    minimum: <明确的PSS下界bytes>
    maximum: <明确的PSS上界bytes>
    slope_minimum: <明确的bytes每秒下界>
    slope_maximum: <明确的bytes每秒上界>
    cohort_stability: true
  cohort:
    kind: owned_api
    node: <实际部署node标识>
    node_field: placement.node
    scope_ref: <此work的cohort范围证据>
    complete_listing_evidence_ref: <已审计完整列表协议证据>
```

`node_field` 是已核验 detail JSON 中真实 placement 的字段路径，不能照抄示例而假定产品有该字段。cohort provider 使用当前 work_id 的完整 owned list，并对每个 detail 重查 run_id、node 与 state；placement/ownership 不符即拒绝，不自动迁移或补创建。B09.PAUSED 的 pause 逐实例先记持久 intent、再提交和 receipt，随后只读观察实际 paused IDs。

initial inventory 是有界 JSON 文档：

```json
{
  "environment_ref": "<case environment_ref>",
  "inventory_ref": "<case resources.inventory_ref>",
  "node": "<node>",
  "boot_id": "<实际procfs boot ID>",
  "evidence_ref": "<部署归属证据>",
  "processes": [
    {
      "identity": {"environment": "<case environment_ref>", "node": "<node>", "boot_id": "<boot ID>", "pid": 123, "start_ticks": 456},
      "role": "<已证明角色>", "component": "<已证明组件>"
    }
  ]
}
```

PID/start_ticks 只是格式示例。共享进程允许多条角色标签并按地址空间去重。sandbox 进程还必须有 sandbox_id 与 generation。不要把请求数或仅按名字命中的进程当完整 inventory。

动态 collector snapshot 使用同样 scope 和 processes，另含 `snapshot_version`（正整数）、`complete: true`、`work_id`、`cohort_ids`、`clock_domain`、`observed_monotonic_s`。clock_domain 为 `sandbox_bench.runner.digest([environment_ref,node,boot_id])`，必须处于同一宿主单调时钟；读入时年龄不得超过 resources.max_gap_s。版本回退、同版本内容冲突、跨 boot/env/node、缺完整性或新 cohort 缺进程映射都停止新的准入。每轮采样调用 `update_inventory`，持久 inventory_version/source_ref/snapshot_cohort_ids；原始 snapshot 一并留存，PID/start/generation 改变后 CPU 不跨身份相减。

只有固定进程范围可用 `kind: procfs` 并省略 snapshot_path；这种模式不能声明 `cohort_stability: true`。它不代表动态新增 sandbox 的完整进程集合。projection 的宿主 MemAvailable provider 保存实际 `[started_s,ended_s]`、`window_version=host_snapshot.v1` 与 `window_kind=declared_snapshot`，不会将单次读取升级成稳态窗口或实测容量。

## FIRST、guest proof 与终态日志

AgentENV 的 B07、EXEC_COMPLETE 或命令后验在 N01 要求部署 codec 证据。下面字段放在 config 顶层；environment_ref/product_version 必须与 case 精确一致，evidence_ref 必须指向实际预检：

```yaml
protocol_evidence:
  environment_ref: <case环境>
  product_version: <case产品版本>
  codec: connect-json
  evidence_ref: <部署协议预检证据>
```

缺失或不匹配的证明会 blocked；protobuf 当前明确 unsupported，不回退发送 JSON。仅 STATE_READY 且不执行命令后验时不要求该执行协议证明。

FIRST 的配置明确指向跨 work 共用的单机 authority；不得为每个 work 新建一份文件：

```yaml
wiring:
  version: wiring.v1
  environment_ref: <case环境>
  first:
    kind: local_file
    path: /absolute/shared-authority/first-command.json
    authority_ref: <单机公共authority的部署证明>
```

target 还必须给 sandbox_id、generation，fixture_input 必须提供 first_command_eligible 和相应 fixture/environment 证据。预留键包含 environment、sandbox 与 generation；未知命令不会恢复 FIRST 资格。此 provider 使用文件锁，**没有声称跨主机数据库 authority**；正式多 worker authority 仍应注册持久数据库适配。

Guest proof 配置：

```yaml
probe:
  kind: unix_socket
  socket_directory: /absolute/guest-forwards
  before_path: /absolute/input/guest-before.json
  before_sha256: <冻结原始字节摘要>
  timeout_s: 2
  max_reply_bytes: 65536
```

before 文档必须含 environment_ref、fixture_ref、evidence_ref 和 `sandboxes: {sandbox_id: <GuestProbe.inspect原始结果>}`。MEMORY 比较驻留 nonce、实际内存 bytes/hash，DISK 比较非空文件集合哈希。clone 隔离验证要求先有已触页内存和 baseline 文件；对 child 写入私有内存及唯一文件，核验 source 和 sibling 在操作前后不变。验证发生在性能终点之后，标记 validation_interference；原始 before/after 与挑战内容保存在 provider_evidence。probe 不通过 exec 启动，避免提前消费 FIRST。它不能证明一个已经重启的进程与原进程内存连续。

Kuasar SERVER_TERMINAL 使用明确 collector 文件：

```yaml
terminal:
  kind: kuasar_jsonl
  path: /absolute/collector/terminal.jsonl
  accepted_path: /absolute/collector/accepted.json
  collector_ref: <collector身份>
  profile: <准确launch profile>
  launch_kind: create  # B04必须为resume
  max_bytes: 8388608
```

accepted.json 是 `{environment_ref, operations:[{sid,run_id,kind,profile,accepted_at,evidence_ref}]}`。每个 sid 与当前 work_id、kind、profile 精确关联，AcceptedAt 必须有来源证据和带时区的非零时间。只消费本地 collector 已捕获的真实 slog；未部署 collector 时不产生内部耗时。相关 raw terminal、AcceptedAt 和 parser_version 会封存于 provider_evidence，成功 duration 与客户端观察时钟分列。

SLO_VERIFY 还必须配置 `slo: {kind: collector_json, path: /absolute/collector/slo.json, collector_ref: <ref>, max_bytes: 65536}`。文档必须含相同 environment_ref/node/work_id/clock_domain、近期 observed_monotonic_s，以及 `metrics: {名称: {value, operator: "<=或>=", evidence_ref}}`；缺值、方向或证据不会当作通过。

## B08 独立恢复后验

B08 在 `wiring` 内同时配置上述 probe 和下列 restore。发布方法还需 `source: {kind: collector_json, path: /absolute/collector/source.json, collector_ref: <ref>, max_bytes: 65536}`；来源文档必须具有匹配 environment_ref 和每个 source_id 的真实 published 观察，API ACK 不充当发布证据。

```yaml
restore:
  kind: budgeted_create
  max_requests: <不大于性能Q的独立恢复请求总预算>
  timeout_s: <独立恢复与验证总截止秒数>
  validation_mode: memory_and_disk  # 或明确选择disk
```

性能完成先写 durable child receipt 并释放性能槽，再运行恢复；恢复不计入性能 Q 或捕获延迟，也不延长性能终点 deadline。callback drain 额外覆盖 restore.timeout_s，TTL 必须覆盖运行、两段 drain 与清理。恢复使用独立 `fixture_restore` FileJournal，提交前持久 intent，返回身份先持久登记 ownership，再只读观察 ready 和检查明确的 guest baseline。该目录及 provider 原始证据由 N10 封存。

每个 request_index 只允许一个恢复尝试。已完成后验可重读 receipt；提交已开始但缺响应、超时或验证未知时只进入对账，不重新 POST。已知恢复 sandbox 即使验证失败仍交 N09 归属清理。配置下述 source_cleanup 后，恢复提交前会 pin 来源并绑定已知 child。恢复 unknown 或未完成时持续保留引用；collector 声称 child 不存在也不会将未 settled 的恢复擅自完成。只包含独立恢复 unknown、主账本没有 unknown 的情况同样进入 reconcile、evidence_gaps 和隔离；发现只接收精确 work、fixture role、source 与 operation metadata，不重发或自动认定原操作 settled。

## 单宿主共享来源与清理

`source_cleanup` 是可选静态 provider；不配置时来源保留。所有 work 必须指向同一宿主的共享 ledger，不能为每个 work 新建一份：

```yaml
source_cleanup:
  kind: local_ledger_collector
  ledger_path: /absolute/shared-authority/sources.json
  collector_path: /absolute/collector/source-usage.json
  authority_ref: <完整来源与consumer观测authority>
  max_bytes: 8388608
  max_age_s: 5
  timeout_s: 10
  delete_capabilities:
    - environment_ref: <case环境>
      product: <case产品>
      product_version: <case产品版本>
      source_kind: template
      endpoint: /templates/{source_id}
      evidence_ref: <该部署版本已核验的template删除能力>
```

能力列表可为空，此时只登记引用并保留来源。当前注册的删除路由仅限明确证明的 template；snapshot/image 不猜测路由，返回 blocked。`Adapter.delete_source` 存在不等于产品支持来源删除，也不证明物理文件释放。来源身份保留 owner work；其他 work 可引用已登记来源，不能改写 ownership。

collector 文档须有匹配 environment_ref、product、product_version、authority_ref，以及 `sources` 和 `consumers` 字典。`sources[source_id]` 包含 source_id/environment_ref/kind、complete=true、exists 布尔、完整 consumer_refs、observed_wall_s 与 evidence_ref。`consumers[consumer_ref]` 包含 source_id、准确 sandbox_id、operation_settled=true、observed_wall_s、evidence_ref，以及 sandbox_absent=true；或同时给该 source 的权威零引用证明。时间必须新于 pin 且位于 max_age_s 内。

创建前持久 pin，收到身份后绑定 child。缺失 child 身份、未 settled restore、陈旧/不完整/跨环境证明不会释放引用；DELETE ACK 也不释放引用。N09 先执行 owned sandbox 清理，再检查来源。借入来源永不由当前 work 删除，但当前 work 的 consumer 仍须独立证明后释放；残留 pin 使清理 incomplete 并留下 evidence gap。来源 DELETE 先有持久 intent，未知响应不重发；逻辑不存在必须由删除 callback 返回后的独立新观察确认，物理释放仍 unverified。

本 work 的 bindings、使用证明原始字节和共享 ledger 快照进入 N10 封存。重开恢复 pins/owned；共享账本或迟到事实变化后，原 seal 拒绝复用。此 provider 的边界是同一宿主文件锁与明确 authority，不是分布式数据库 fencing。

## 尚未接好的代码范围

这些是实现/接线缺口，不应全部归因于没有真实环境：

- B02 共享一次 build 再创建 K child 的方法、共享成本分组和可接管 build 阶段机仍未完整提供。
- B08 已接独立预算和持久恢复；产物大小、完整来源副作用证明和物理清理终点仍需补齐对应合同，不能凭恢复通过完成整个任务 20.2。
- SourceLedger 已接创建、restore、重开、N09 与 N10；snapshot/image 删除能力和物理释放仍未注册，不将模板删除接口套用到三产品全部来源。
- FIRST 文件 authority 仅支持一个共享文件系统的单机范围；跨主机 authority 尚未注册。远端 command task identity/未知执行对账也未因本地 FIRST 账本而解决。
- collector_procfs 已支持动态、显式身份快照；产生该快照的部署 inventory agent、socket 转发、源/规格证明、真实日志捕获和 Linux 权限需由实际环境提供。代码不猜测这些事实。
- build 共享方法、远端命令未知对账及更多已审计来源删除/物理释放适配的受控协议测试仍可继续开发，不能因服务未部署就标为实现完成。

任务 16.3/16.4/18.1/18.2/18.3 的完成与时序算法已有专项证据，18.1 强杀三切点恢复由主代理 journal 测试覆盖；20.1 在 snapshot 来源清理及完整隔离矩阵验收前保持未勾。其余任务按整条验收条件核对，不能仅因一个 provider 可调用便勾选完整方法矩阵。

## 创建终点声明必须与审计的同步性一致（issue #5）

创建类用例（`B00.`/`B01.`/`B02.`）的 `completion.endpoint` 必须匹配实际创建入口的（已审计）同步性，否则 `runner case` 在预检直接 `blocked`（退出码 `2`，`reason` 含 `endpoint_declaration_mismatch`）：

| 产品/拓扑 | 创建同步性 | 必须声明 |
|---|---|---|
| CubeSandbox、AgentENV（任意拓扑） | 同步（返回即 ready） | `API_COMPLETE` |
| Kuasar `router` | 同步（park 到 running） | `API_COMPLETE` |
| Kuasar `node_direct` | 异步（返回 `starting`） | `STATE_READY`（须轮询到 ready） |
| 未审计（未知产品/缺拓扑） | 未验证 | 不强制（记原因，不猜） |

原因：对同步创建声明 `STATE_READY` 会多做一次 `GET /sandboxes/{id}` inspect 往返（约 `0.1s`），把 create 时延虚高；对异步创建声明 `API_COMPLETE` 则把“受理”当“完成”。校验实现：`sandbox_bench/endpoint.py::endpoint_declaration_consistent` / `validate_endpoint_declaration`，复用 `operation_sync.completion_plan` 作为唯一审计来源。

### 异步入口（仅数据面 node API）必须**密集轮询**

有 router/管理面入口时，**直接测 router 的同步创建用时**即可（`API_COMPLETE`，无额外轮询）。只有在部署**没有管理面**、只能用数据面 node API（`node_direct`，返回 `starting`）时才必须轮询；而沙箱状态读数粒度粗、我们的创建是 **ms 级**，所以轮询间隔必须足够密，否则测到的是**轮询粒度**而不是创建耗时：

- `endpoint.poll_interval_adequate(readiness_signal, poll_interval_s, max_interval_s=0.005)`：`poll_state` 入口要求 `0 < poll_interval_s <= 0.005s`；
- `validate_endpoint_declaration` 对异步创建一并校验，过粗 → `runner case` 预检 `blocked`（`endpoint_poll_too_coarse`）；
- 同步入口（`readiness_signal='none'`）不做此校验。
