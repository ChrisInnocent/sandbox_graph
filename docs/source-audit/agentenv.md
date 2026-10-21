# AgentENV 源码审计

审计日期：2026-10-20。范围：本地源码、API schema、测试和脚本静态阅读；没有启动沙箱、访问远端、读取部署凭据或运行性能测试。本文中的能力证据不代表目标部署已启用或通过实测。

## 版本、名称与部署模式

2026-10-21 设计修正：创建的状态完成、内部耗时和命令完成分别测量，不强制 exec 作为唯一创建终点；密度原列表净增长与新增健康停止分为不同方法。最新节点及 PSS/CPU 观测合同见 [场景节点设计](../../openspec/changes/sandbox-evaluation-workflow/test-nodes.md)，本文源码证据保留，早期测量建议按新合同解释。

节点级分工：R1/T0 通过脚本核对版本和引用，模型分析 ready、路由、模板资源继承与协议语义；E1/E2/C1–C3 脚本检查角色／观测权限并执行已定配置；N00–N10 脚本处理 TTL、placement、逐 child 结果、采样、每秒创建吞吐、状态／内容断言及资源清理。PSS 不能由 allocatedMemoryBytes 顶替，未知 JSON codec／Guest OS 分离能力不得默认通过；需要新语义由模型分析、未定条件由人工明确。全部方法的脚本／模型／人工与 planned/draft 状态见 [职责表](../../openspec/changes/sandbox-evaluation-workflow/node-automation.md)，内部流程见 [Mermaid](../../openspec/changes/sandbox-evaluation-workflow/node-workflows.md)。

- HEAD：`49684233698d40dd800c1d0edba15ab30a745eeb`；审计开始时工作树干净。脱敏核对的 origin 为 `github.com/kvcache-ai/AgentENV.git`。
- `AGENTS.md` 是 `CLAUDE.md` 的符号链接。项目为 AgentENV/AENV：Rust sandbox 服务、Firecracker、OverlayBD/ublk、E2B 兼容 HTTP API；Go Gateway/Scheduler 为独立模块。见 [项目指南](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/CLAUDE.md#L3)、[服务说明](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/README.md#L3)。
- 实际 server 构造 `FirecrackerSandboxFactory`，抽象 backend trait 不等于其他 runtime 已实现：[server.rs:128](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/bin/server.rs#L128)。
- KVM 默认，PVM 可选；快照禁止跨虚拟化模式：[virtualization.rs:3](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/virtualization.rs#L3)、[config.rs:497](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/sandbox/firecracker/config.rs#L497)。
- snapshot repository 为 OSS/S3 或 POSIX FS；capture 保持源运行，pause/resume 保留同一 sandbox ID：[概念约束](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/CLAUDE.md#L89)。
- 测试前需固定单节点直连或 Gateway 集群、节点数、KVM/PVM、OSS/POSIX、镜像类型与缓存状态、规格和 scheduler 限额。它们不能隐含在同一个“AgentENV”标签内。

## B01–B10 能力矩阵

| 编号 | 接口与源码证据 | 测试/脚本情况 |
|---|---|---|
| B01 模板/快照创建 | `POST /sandboxes` 或 `/v2/sandboxes`，必填 `templateID`；[schema:647](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L647)、[实现:1008](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1008) | SDK template create、snapshot relaunch 有脚本 |
| B02 镜像创建 | `POST /sandboxes-cold`，必填 `image`，可设 `cpuCount,memoryMB,diskSizeMB`；[schema:743](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L743)、[实现:666](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L666) | [volume suite:103](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/suites/15_volume.sh#L103) 使用此接口 |
| B03 暂停 | `POST /sandboxes/{id}/pause`，await 完成后 204；[实现:1735](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1735) | [lifecycle suite:28](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/suites/02_sandbox_lifecycle.sh#L28) |
| B04 恢复 | `POST /sandboxes/{id}/resume`，body `timeout`，成功 201；也可 `connect`；[实现:1922](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1922) | [SDK pause/connect:155](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/e2b_python_sdk_compat.py#L155) |
| B05 克隆 | `POST /sandboxes/{id}/fork`，`count` 1–100，默认 1，支持 `timeout`；HTTP 201 的数组逐项为 `sandbox` 或 `error`；[schema:851](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L851)、[实现:1521](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1521) | [integration:126](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/tests/integration/orchestrator.rs#L126)，[部分失败单测:5346](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/tests.rs#L5346) |
| B06 删除 | `DELETE /sandboxes/{id}`，await 后 204；[实现:1382](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1382) | lifecycle/SDK 脚本有 |
| B07 命令 | envd `process.Process/Start`；SDK `commands.run` 校验退出码和 stdout；[SDK:128](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/e2b_python_sdk_compat.py#L128) | 有真实执行验证；不能假设存在通用 `/exec` |
| B08 快照 | `POST /sandboxes/{id}/snapshots`，body `{name?}`；await capture + publish 后 201；[实现:1772](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1772)、[publish:1841](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1841) | [snapshot suite:25](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/suites/13_snapshot.sh#L25) 校验 `snapshotID,names` 及源删除后复用 |
| B09 底噪 | 有 host sampler，[host.rs:92](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/observability/host.rs#L92) | 未找到专用持续底噪 benchmark；不能据此判能力不支持 |
| B10 密度 | 有节点资源和计数接口 | [node metrics suite:195](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/suites/11_node_metrics.sh#L195) 仅核对额度/计数；未找到连续两批新增成功 `<10` 的密度脚本 |

## 同步 ready 与计时边界

创建源码明确承诺成功返回时 fully ready 且持久化为 Running：[service.rs:419](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L419)。实现先 [wait_for_ready:2495](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L2495)，再更新 Running、发布 proxy route，最后返回。Firecracker [wait_for_ready:912](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/sandbox/firecracker/sandbox.rs#L912) 等待 envd health 并执行 init；[envd.rs:118](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/sandbox/envd.rs#L118) 的探针有总 deadline 和单次 timeout。恢复使用同一 launch 路径，见 [恢复约定:1630](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L1630)。

B02 镜像解析也在同步请求内；[sandbox.rs:676](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L676) 的 TODO 明示 202+polling 尚为未来工作。可统一记录请求开始至同步返回的 `api_ready`，另记录请求开始至唯一 marker 命令成功退出的 `first_exec`。若重试须保留全部等待时间、次数和失败信息。ready 表示 guest/control plane 就绪，不承诺用户自行启动的所有应用都就绪。

现有 benchmark 不能直接搬成 B01–B10：

- [snapshot_benchmark.rs:233](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/benchmarks/benches/snapshot_benchmark.rs#L233) 名为 `snapshot_creation`，实际上只计 `sandbox.pause()`，不含 HTTP、orchestrator 或持久 publish。
- [热恢复:286](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/benchmarks/benches/snapshot_benchmark.rs#L286) 保留 warm sandbox 使共享 memory ublk 活跃；“冷”恢复未清空宿主页缓存/OCI缓存，不是 B02 镜像冷启动。
- [并发恢复:387](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/benchmarks/benches/snapshot_benchmark.rs#L387) 使用 50 worker，并将各时长总和除并发度，非整批 wall-clock，非密度。
- 默认 10 samples，输出 mean/min/max；[setup 失败:117](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/benchmarks/benches/snapshot_benchmark.rs#L117) 可仅输出 `Skipping`，不能拿成功进程退出当测量成功。
- [code_interpreter_compat.py:250](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/code_interpreter_compat.py#L250) 的 `cold_to_first` 是模板 create + 首次 run_code（含重试），并非 OCI 冷启动；[并发 wall:327](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/code_interpreter_compat.py#L327) 含 create、warmup、workload。
- [run-e2b-benchmark.sh:74](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/e2b/run-e2b-benchmark.sh#L74) 只是克隆外部 infra 并调用外部脚本，不能从此仓库确定其测量口径。

## stdlib HTTP 适配：数据面与协议

控制面使用 `X-API-Key`；数据面使用创建/详情返回的 `envdAccessToken` 作为 `X-Access-Token`，二者不可混用。[CLI transport:92](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/aenv/src/grpc/mod.rs#L92) 的已证实路径：

- `POST {API_BASE}/process.Process/Start`，header `x-agentenv-sandbox-id: {id}`、`x-agentenv-target-port: 49983`、`Connect-Protocol-Version: 1`。
- 不需要 `/proxy` 前缀，因为 fallback 会识别路由 header；[helpers:171](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/lib/helpers.sh#L171) 也证明 `{API_BASE}/proxy/...` 路径可用。选一种固定路径，避免重复 `/proxy`。
- E2B header 别名 `e2b-sandbox-id`、`e2b-sandbox-port` 在 [gateway server:24](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/gateway/internal/server.go#L24) 定义。
- 配置 proxy domains 时可用 `{port}-{sandboxID}.{proxy_domain}` host；见 [服务说明:29](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/README.md#L29)。域名、TLS及暴露端口必须从部署获得，不能仅用返回 domain 推断 scheme。
- guest 用户通过 HTTP Basic username 选择；root 为 `Authorization: Basic base64("root:")`，不是控制面 Bearer。直接证据：[process.rs:18](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/sandbox/process.rs#L18)。不设置时继承 envd 默认用户；root 是否符合基准工作负载要显式固定。

本地 CLI 证实的是 `application/connect+proto`，不是 JSON codec。Start 为 server stream；[transport:177](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/aenv/src/grpc/mod.rs#L177) 把请求编码为 1 字节 flags + 4 字节大端长度 + payload（[编码:243](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/aenv/src/grpc/mod.rs#L243)），正常数据 flag 0，结束 envelope flag 2。不能把 HTTP 200、收到 start PID、或流结束单独当命令成功。

[process.proto:32](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/thirdparty/envd/proto/process.proto#L32) 给出请求字段 `process:{cmd,args,envs,cwd?},stdin,tag?,pty?`，建议非交互短命令 `stdin:false`。响应为 `event.start`、`event.data.stdout/stderr`、`event.end`；end 含 `exit_code,exited,status,error?`。JSON 方案应以 `application/connect+json` 的 Connect envelope 及 protobuf JSON 映射实现，bytes 输出需 base64 解码，字段一般为 `exitCode`；**本仓库没有 JSON server codec 的直接实现证据或实际请求结果，必须在已固定 envd 版本上先做兼容预检，不能声称已验证。** CLI 在 [transport:107](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/crates/aenv/src/grpc/mod.rs#L107) 支持 unary List，可用于协议握手，但它不替代实际命令验证。

`thirdparty/envd` 是客户端库：[README:3](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/thirdparty/envd/README.md#L3)。guest 二进制由 tools image 从 upstream infra 构建，默认源码 ref `2026.17`：[Dockerfile:39](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/tools-image/Dockerfile#L39)。另一个本地 infra checkout 的 HEAD 不一定就是此二进制版本。

## 公共 list/info、metadata 与 placement

- 用 `GET /v2/sandboxes`，body 是数组，`limit` 默认/上限均100；分页 token 在响应头 `X-Next-Token`，下一页 query `nextToken`，不能找 body.nextToken。[schema:84](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L84)、[list:1819](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L1819)。
- 过滤：`metadata` 是内层 URL query 字符串，例如先 `urlencode({"run_id":run})` 再作为外层 `metadata` 参数编码；源码用 `form_urlencoded::parse`，[sandbox.rs:486](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L486)。多个键为全部精确匹配：[in_memory.rs:63](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/store/in_memory.rs#L63)。
- `state=running` 或 `paused`；可逗号连接，缺省/多状态按无状态过滤处理；`order=asc|desc`、`startedAfter`、`template` 也支持。[实现:2020](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L2020)。`X-Total-Running` 是匹配集合的 running 计数，不代替遍历 ID 对账。
- `GET /sandboxes/{id}` 的 detail 有 `cpuCount,memoryMB,diskSizeMB,state,metadata,startedAt,endAt,lifecycle,envdAccessToken,domain`，见 [schema:522](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L522)。基础 create 返回没有 state 和规格；不能凭 create payload 判定有效规格。
- 公共 Sandbox/SandboxDetail/ListedSandbox **没有 nodeID 字段**，`clientID` 为弃用兼容字段且实现为空：[sandbox.rs:424](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L424)。
- Gateway 仅在 debugMode=true 时响应 `x-agentenv-node-id`，默认关闭：[server.go:64](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/gateway/internal/server.go#L64)、[注入:361](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/gateway/internal/server.go#L361)。缺失时不应虚构 placement。
- 无 debug header 时，已有脚本枚举已知 runtime URLs，直接 `GET /sandboxes/{id}` 找 HTTP200 的节点，再用该节点 `/nodes` 的 `.id` 关联：[helpers:514](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/lib/helpers.sh#L514)。这需要节点URL清单及访问权限。`GET /nodes` 与 `/nodes/{id}` 不公开 sandbox ID roster。

## TTL、自动暂停与模板规格

- 创建/恢复/续租的 `timeout` 单位是**秒**：[转换函数:499](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L499)。
- V1 create/cold/resume schema 默认15秒；实际省略时取服务器 `default_sandbox_timeout_secs`（默认15）：[config:210](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/config/default.toml#L210)。V2 create 强制缺省300秒且 secure=true：[sandbox.rs:1180](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1180)。
- `autoPause` 缺省/true 是到期 Pause，false 是到期 Delete，并非关闭 TTL：[sandbox.rs:1105](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L1105)。`autoResume` 是对象 `{"enabled":false}`，缺省false；它是数据面自动恢复开关，也不是 TTL 开关。
- `timeout:0` **不是无限期**。metadata 直接以 now+duration 设置 expires_at，并用 deadline<=now 判断过期：[metadata.rs:112](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/store/metadata.rs#L112)。V2 schema 还规定最小1。
- 内部 `NewTimeout::None` 能表示无超时，但未找到公共 HTTP 参数暴露它。长测设置足够长的 timeout，或定期 `POST /sandboxes/{id}/timeout {"timeout":seconds}`；此操作从当前时间重新计TTL而非累加：[schema:2274](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/openapi.yml#L2274)。详情 `endAt,lifecycle.onTimeout,autoResume` 用于核验。
- 模板创建继承 `*snapshot.resources()`：[service.rs:525](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L525)。对 B01，应先用规定规格构建模板，再创建一个探测实例，GET detail 校验 `cpuCount,memoryMB,diskSizeMB`；这些字段从 runtime metadata 输出：[sandbox.rs:435](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/api/impls/sandbox.rs#L435)。不要向 B01 payload 填 schema 没定义的资源字段期待覆盖。

## 底噪、实测密度与基线增量估算

`/nodes.metrics.allocatedCPU/allocatedMemoryBytes` 是配置额度，不能当实际CPU/RSS。`memoryUsedBytes` 默认由 MemTotal−MemAvailable 得到；存在有效 cgroup limit 时优先 memory.current：[host.rs:118](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/observability/host.rs#L118)、[meminfo:163](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/observability/host.rs#L163)。CPU百分比来自宿主 `/proc/stat`，cpuCount 却可受 cgroup quota 裁剪，容器部署要统一采样域。节点直连接口采样更即时，Gateway汇聚受心跳延迟影响。

[11_node_metrics.sh:287](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/scripts/tests/e2e/suites/11_node_metrics.sh#L287) 把 memoryMB×1024² 累加，检查 allocatedMemoryBytes，是额度一致性测试，不是物理增量密度。

逐批密度应固定批大小（至少10）及并发；批内等待全部结果，保留成功且命令验证通过的实例，复检存活，连续两批“新增成功数<10”才触发该停止规则。显式长TTL/续租防止早期实例暂停造成虚高。create [cancellation-safe:430](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/src/orchestrator/service.rs#L430)，客户端超时后需用 metadata 对账迟到创建，不能把超时当自动删除。

scheduler 可按数量、starting数、CPU、内存以及含暂停额度筛节点：[filter.go:31](https://github.com/kvcache-ai/AgentENV/blob/49684233698d40dd800c1d0edba15ab30a745eeb/services/scheduler/internal/filter.go#L31)。新增失败必须分类为策略限额、超时、OOM、控制面错误等，不能都写成物理极限。fork HTTP201必须逐条判结果。

基线增量法只能提供估算：固定模板、规格、缓存和负载，在稳定窗口求 Δ真实资源/Δ存活实例 的边际斜率，再按CPU/内存/其他资源预算取最小约束。共享页缓存、COW、懒加载、balloon 会造成非线性，须多批采样并报告残差/范围；不得用分配额度或单点平均冒充实测饱和密度。B09基线应保留必需控制面服务，记录宿主与服务cgroup口径及采样窗口。
