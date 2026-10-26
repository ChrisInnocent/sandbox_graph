# Agent provider 应用协议 v1

`sandbox_graph.runtime.agent_bridge` 提供本项目自己的应用内 `AgentProvider` 接口、持久客户端 `AgentBridge` 和 `HttpAgentProvider`。应用内实现可以直接实现六个 async 方法；HTTP 实现只调用下述公开约定。这里没有 Codex/OpenCode 私有端点、父会话读取或自动宿主发现。

本模块的受控协议验证状态为 **locally_verified**。测试包含真实 TCP loopback HTTP 服务，但服务和响应为受控夹具；这既不是生产模型调用，也不是宿主适配验证。真实部署与权限/隔离审计留给 49.4 的 `adapter_verified` 验收。完整 lifecycle worker、预算/授权事务和诊断/候选 provider 调用链尚须集成，不能因本模块存在而宣称全自动闭环可用。

## 调用接口与权限边界

```python
from sandbox_graph.runtime.agent_bridge import AgentBridge, BridgeConfig, HttpAgentProvider

# config 由受信任的部署配置构建，不从模型输出或候选代码加载。
provider = HttpAgentProvider(config)
bridge = AgentBridge(repository, provider, provider_id=config.provider_id, scope=config.scope)
record = await bridge.submit(envelope)
record = await bridge.status(envelope.request_id)
record = await bridge.cancel(envelope.request_id)
# cancel accepted 后继续有限 status；只有 record.terminal 才可 collect。
if record.terminal:
    result = await bridge.collect(envelope.request_id)
```

`AgentProvider` 的底层方法为 `capabilities()`、`submit(SubmitEnvelope)`、`lookup(request_id)`、`status(handle)`、`cancel(handle)`、`collect(handle)`。`AgentBridge` 的 `lookup/status/cancel/collect` 则均接收原 `request_id`，从账本解析 handle；调用者不自行替换任务身份。`capabilities(*required)` 返回 capability 合同或抛出含缺口列表的 `CapabilityMissing`。

提交前，调用服务必须在其真实 job claim、owner fence、scope 授权和 lifecycle/lineage 预算事务中预留费用并冻结 envelope。bridge 不代替该事务，不接收 `authorized=True` / `resources_disposed=True` 等布尔信任捷径。provider capability 是所配置服务必须兑现的合同，不能证明调用者已获授权，也不能凭模型自报获得能力。受信任部署还须验证服务确实强制上限及隔离边界。

新请求的自动 submit 必需 `idempotent_submit`、`lookup_by_request_id`、`durable_handles`、`status`、`cancel`、`collect`、`scope_enforced`、`usage_limits`。候选工具路径额外调用 `submit(envelope, required_capabilities=('isolated_tools',))`；缺少隔离工具只阻塞依赖它的路径，普通诊断提交不要求该能力。重复 submit 先读取并核验账本绑定：无 handle 只需 lookup 能力，有 handle 只需 status 能力；新的 submit 所需 cancel/usage_limits 或 required_capabilities 丢失不阻塞原任务恢复。终结请求重放直接返回缓存，不调用 provider（包括 capabilities RPC）。后续 lookup/status/cancel/collect 分别检查自己需要的能力，已有终结事实和已保存结果可以直接读取。新请求在 capability 查询后再次以同一事务检查 request_id，只有实际创建 intent 的调用者可发送；竞争写入的不同 envelope 必须拒绝。缺口如 `capability_missing:lookup_by_request_id`、`capability_missing:scope` 明确返回。

## 严格数据合同

所有 wire 模型要求显式整数 `schema_version=1`，拒绝 bool/float/string 版本、未知字段和类型强转；模型冻结，数组在 Python 端为 tuple。标识最多 512 字符，不允许空值和空白。JSON 文本及 HTTP body 均有独立字节界限，拒绝 NaN/Infinity 和重复 JSON key。

| 模型 | 必需字段/含义 |
|---|---|
| `ProviderScope` | `kind` 为 run/group/work/node/candidate；`ref` 为该作用域身份 |
| `UsageCeiling` | 正整数 `input_tokens`、`output_tokens`、`cost_microunits`；费用单位固定为所配置计价单位的百万分之一 |
| `SubmitEnvelope` | `request_id`、`lifecycle_id`、scope、`input_json`、`input_hash`、`model_id`、不可变 `model_version`、usage_ceiling、绝对 UTC epoch `deadline` |
| `ProviderCapabilities` | provider_id、上述九个布尔能力字段、精确 scopes 列表；不接受通配符隐含授权 |
| `ProviderReceipt` | 原 request_id/scope/input_hash/model_id/model_version/usage_ceiling、持久 handle、唯一 receipt_id、state、children_stopped、usage |
| `Usage` | 非负整数 input_tokens/output_tokens/cost_microunits；全部不得超过预留上界 |
| `LookupReply` | acceptance=accepted/not_accepted/unknown；accepted 必须带 receipt，另外两种不得带 receipt；v1 的两种负向观察都不构成非执行证明 |
| `CancelReply` | 原 request_id/scope/handle、accepted；只确认取消请求已接收 |
| `CollectReply` | 终结 receipt 与有界 `output_json`；结构化建议仍需下游业务 schema 和证据验证 |

`input_hash` 是 **input_json 原始 UTF-8 字节** 的小写 SHA-256。禁止只绑定部分上下文；即使 JSON 语义相同，字节变化也属于不同输入。模型 identity/version、scope、lifecycle、截止时间、usage ceiling 与整个 envelope 一起构成 request_id 的不可变绑定。

state 仅有 queued/running/succeeded/failed/cancelled。只有后三者且 `children_stopped=true` 才构成终结证明。`usage=null` 明确表示未知，不能按 0 结算；调用方预算服务须保留/消耗全部预留，直到可信结算有据可查。实际 usage 超过 ceiling 返回 `usage_ceiling_exceeded`，不认可为合法回执。

## 持久提交与恢复

Repository 使用通用 ledger，不需要额外数据库表：

| ledger kind | 用途 |
|---|---|
| `agent_provider_requests` | 以全局 request_id 保存 provider_id、原 envelope、当前 receipt/result、取消确认和状态 |
| `agent_provider_handles` | 固定 provider + scope + handle 对 request_id 的唯一绑定 |
| `agent_provider_receipts` | 按 provider + receipt_id 保存不可变原回执；相同内容幂等，冲突拒绝 |

首次 submit 先在事务中持久写入 `submitted_unknown`，事务结束后才访问 provider。过期的首次提交不会建 intent 或调用 submit。网络超时、响应丢失或无效提交回执保留原 intent，记录稳定错误码，返回 `submitted_unknown`；进程在提交前后任意死亡也保留同一个 request_id。查询异常直接抛出，原未知状态不丢失。

恢复/重复 submit 不再发送 submit：无 handle 时调用原 request_id 的 lookup；已有 handle 时查询原 task。lookup 返回 unknown 或 not_accepted **均保留 `submitted_unknown`**。瞬时查无记录可能发生在原 submit 尚未送达、远端提交排队或响应丢失时，不能证明之后不会接受。后续模糊提交错误同样保留未知，不能保留旧版本可能写入的负向确定状态。禁止为此换 request_id 重提；并发到达的旧负向 lookup 不能覆盖已知接受或终结事实。

本协议 v1 没有表达或验证持久 tombstone 证明的字段，因此不产出可用于 `proven_not_submitted` 的结论，不能退款/释放预留或按未执行结算。即使 wire acceptance 使用 not_accepted，也只是负向观察。未来若要支持确定非执行，必须新增由受信任 provider 原子持久化、不可撤销且绑定 provider/request_id/scope/完整 envelope 的拒绝证明，并保证所有延迟在途 submit 永远不能接受该 ID；单次 lookup 缺失或时间戳不满足此条件。

相同 request_id 的 provider/scope/lifecycle/模型/输入/ceiling 任一不同都拒绝。一个 scoped handle 不能绑定两个 request_id；已知任务也不接受换 handle。回执重放不生成新调用；同 receipt_id 不同内容、终结后改回执或 collect 内容冲突均拒绝。status 和 collect 应重复使用同一份不可变终结 receipt；服务不能为同一终结事实随意换 receipt_id。

取消 accepted 保留 `cancel_accepted`，不写 terminal。调用者继续受限查询，直到终结回执同时证明所有子任务停止。未知效果仍归原生命周期/候选对账；bridge 不释放预算、工作区、资源身份或 scope 隔离。

## HTTP/RPC 配置与认证

`BridgeConfig` 必须显式给出以下全部字段；示例数值仅用于配置形状，不是生产额度：

```json
{
  "schema_version": 1,
  "endpoint": "https://provider.internal.example/agent",
  "provider_id": "configured-provider",
  "bearer_token": "LOAD_FROM_SECRET_STORE",
  "scope": {"schema_version": 1, "kind": "work", "ref": "work:example"},
  "timeout_seconds": 10.0,
  "max_request_bytes": 65536,
  "max_response_bytes": 65536
}
```

部署入口从凭据存储取得 token，构建 `BridgeConfig` 后传给 provider；本任务不新增隐式环境变量发现或全局 bootstrap 配置。token 不写 request ledger；配置 repr 隐藏 token，协议错误只暴露稳定码。不要记录配置完整 `model_dump`。换 token 不改变已有 request_id 与 provider_id；更换服务必须保留旧 provider 的持久任务查询能力。

每个请求携带 `Authorization: Bearer <token>`；server 必须独立认证 token，并确认 token 对请求 scope、nested envelope scope、request_id/handle 原始归属和动作权限均有授权。请求中的 scope 是被核验的数据，不是权限证明。跨 scope 访问拒绝；错误不得泄露其他 scope 的任务存在性。客户端同时验证返回 scope、provider identity 及所有已知绑定。

非 loopback 仅允许 HTTPS，使用系统 TLS 信任校验；仅 localhost/127.0.0.1/::1 的受控测试允许 HTTP。禁止 URL 用户密码、query、fragment、空白或控制字符。客户端不跟随 redirect，不使用环境代理/cookie。401/403 返回 `authentication_failed`；更新凭据后继续查询原 request_id，不重新提交。

## RPC 路由与响应

所有操作均为 `POST <endpoint>/v1/<operation>`，请求/响应 `Content-Type: application/json`。请求顶层固定 `schema_version` 与 scope；其余字段如下：

| operation | 请求附加字段 | 200 响应模型 |
|---|---|---|
| capabilities | 无 | `ProviderCapabilities` |
| submit | `envelope` | `ProviderReceipt` |
| lookup | `request_id` | `LookupReply` |
| status | `handle` | `ProviderReceipt` |
| cancel | `handle` | `CancelReply` |
| collect | `handle` | `CollectReply` |

请求示例：

```json
{
  "schema_version": 1,
  "scope": {"schema_version": 1, "kind": "work", "ref": "work:example"},
  "request_id": "diagnosis:original-id"
}
```

返回完整 `LookupReply`，不能仅返回一个裸 handle。server 的 submit 必须原子落实 request_id 幂等绑定、费用/输入输出强制上限及持久 task handle 后再确认接受；同 ID 同 envelope 返回原 task，同 ID 不同 envelope 返回冲突。server 对暂时缺失应返回 unknown；兼容性 not_accepted 在 v1 也不能被客户端升级为非执行证明。未经持久不可撤销 tombstone 协议扩展，不得承诺它能阻止稍后到达的原 submit。cancel 必须作用于整个受控子任务集合；终结状态同时确认子任务已停，不能只报告父协程结束。

客户端使用一个覆盖连接、TLS、写入、header 与 body 读取的总超时，不能通过持续小包刷新超时。header 上限 16 KiB；请求/响应 body 上限由配置限定，最大 1 MiB。HTTP 200 仅支持 JSON，服务应提供 Content-Length 或关闭连接；v1 不接受 Transfer-Encoding。短包/断链/总超时返回 `transport_unknown`；超响应上限返回 `response_too_large`；错误 schema、重复 JSON key、错误版本返回 `invalid_protocol`。所有这些错误都不能证明 submit 未发生。

## 验证范围

`tests/orchestration/unit/test_agent_bridge.py` 覆盖受控应用 provider 的丢响应/进程死亡恢复、原 ID 查询、全 envelope 幂等冲突、精确缺能力、取消未终结、子任务未停、usage 未知与越界、重复/冲突回执、跨 scope/handle 绑定、迟到 unknown 竞争，以及负向 lookup 先于远端接受且原 submit 丢响应的精确交错。恢复测试另覆盖无关能力缺失、必须的 lookup/status 能力缺口、provider 离线时缓存终结重放、capability I/O 期间同 ID 的首写竞争。真实 loopback 服务验证六个 RPC、Bearer/scope、认证失效、断链、超时、响应过大、协议错误和请求本地边界。

这些测试的内存 Repository 是显式夹具，不能当作 PostgreSQL 故障/并发证明。生产持久性沿用 Repository 实现；真实数据库 crash/restart、provider 后端上限强制、宿主认证/权限/隔离，以及完整诊断/候选/停止链路均须由后续集成与 49.4 现场验收补齐。

## 现代受限团队调用入口

现代模型诊断现在可通过 `AgentTeamService`、`AgentTeamJobHandler` 和
`build_job_handlers(..., model_team=team, agent_provider=provider)` 显式装配。
完整配置、实际接口、严格提案与 scope/owner/预算边界见
[受限角色团队与现代模型诊断](agent-team.md)。旧 `AgentGateway` 不作为回退。

模型版本、input/output token 最大值、cost microunits、全部字节上限、观察轮次、
轮询间隔和绝对 deadline 都必须明确给出。模型预算 cost_unit 必须为 `microunits`，
与本协议 `cost_microunits` 保持一致；不做隐式单位换算。提供方须兑现
`usage_limits`、scope enforcement、持久 handle、原 request ID lookup 等必需能力；
声明能力不代表真实生产验收。

健康路径及规则可处理的确定性修复零模型调用。未知路径排持久 investigator job，
网络前先持久预算/请求身份，并在每次外部调用前、结果发布事务内核验实际 owner。
provider unknown 和 v1 negative lookup 保留原 ID，不换 ID 重发、不退未知用量。
取消 accepted 不能代替 children_stopped；观察耗尽后保留 waiting_provider 和并发槽位，
不自动无限排队。候选 coder/reviewer 工具后端及原 ID 的 exhausted-task 安全恢复消费者
仍待46.*等后续工作，不能把合同或受控 provider 测试报告成实际编码/评审成功。

Round1 修复后，配置还必须显式提供正整数 `max_cancel_cost_microunits`。
同原 request/handle 的取消先写一次性 durable intent，预留独立 safety stop_calls/cost，
拒绝或 unknown 重放只返回原事实，不能免费再发。协议无取消费用 receipt，按预留上限结算；
缺安全额度不发取消。网络与结果发布统一核验真实 role thread、active investigator
和与冻结 envelope 完整绑定且尚未结算的模型预留；coder/reviewer 不能借公开 observe 绕过。
