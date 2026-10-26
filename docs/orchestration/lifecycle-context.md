# 节点最小上下文与证据 capability

本批提供 `LifecycleContextService` 和 `runtime.agents.LifecycleContextBuilder`，用于现代 lifecycle/provider 装配。它们生成严格的 `ContextPacket`，读取明确授权的证据；当前确定性 `LifecycleSupervisor` 尚未消费此 builder，模型诊断链、provider 调用与提案下单不属于本批交付。

## 可信输入和实际接口

服务构造参数为 `repository`、真实 `FileArtifactStore`、明确的 `artifact_scope`、可信登记的 `secret_values`，以及 `max_read_bytes`（默认 65536）、`total_evidence_bytes`（262144）、`max_packet_bytes`（32768）、`max_source_bytes`（1048576）。四个限额均要求正整数，布尔值不能充当整数。实际生产装配应明确选择有限额度。

| 接口 | 输入与行为 |
| --- | --- |
| `grant_evidence(lifecycle_id, *, artifact, producer_identity, visibility, offline_sealed=False)` | **仅供可信生产者/授权边界调用**。`artifact` 必须为配置存储 scope 的 `EvidenceRef`；生产者必须依据其原始生产记录提供身份，而不能采信模型声明。身份字段完整且仅为 `run_ref/scope/node_id/graph_ref/plan_ref/contract_ref`。`private` 要求全部匹配；`shared` 只允许生产节点不同，其他身份必须相同。返回 `evidence-cap:<digest>` 句柄。 |
| `build(lifecycle_id, *, expected_revision, token, task_role='diagnosis', evidence_mode='live')` | 从 lifecycle、原 receipt/source、当前预算账本与可信 grants 组装 packet。检验 revision 与实际 lifecycle owner，模型不能提供字段字典、证据清单、预算使用量或处置布尔值。 |
| `read_evidence(lifecycle_id, handle, *, offset, length, token, evidence_mode='live', expected_revision=None)` | 句柄必须属于该 lifecycle/node/run/scope/版本和当前 sanitizer。只返回脱敏派生产物的字节范围；逐次验证 owner 与现场模式的真实处置证明。可传 `expected_revision` 限制并发版本。 |
| `LifecycleContextBuilder.build/read_evidence` | 上述两个消费接口的关键字适配器。由可信装配持有服务与 token，不提供 `grant_evidence` 模型工具，不调用旧 `AgentGateway`。 |

`node_supervisor_for(node_id, scope, run_ref)` 保持既有 `NodeContext.node_supervisor_id` 的身份规则，按 run、既有 owner、node 区分。N02/N09 即使共用同一 work owner，也有独立 supervisor 身份；旧 `owner_agent_for` 和三级 owner 合同不变。

packet 只包含原失败的状态、错误摘要、效果确定性、重试分类与原节点复审的 `decision/findings`，加上 graph/plan/contract 引用、当前账本使用量和证据句柄。它不复制父对话、input/result refs 全集、原始 output、任意复审附加字段或其他节点日志。事实与空 hypotheses 分开，超过 packet 上限直接拒绝并要求缩小证据范围，不截断失败事实。

`BudgetUsage` 来自 `LifecycleBudgetService.snapshot_in`；业务和安全计数包含已花费与仍预留的数量，`tokens_reserved` 在此 packet 中保守表示已占用 tokens 总数（spent + reserved）。双层预算的 lineage 引用必须与 lifecycle 相同。模型调用的 reservation/settlement 仍由预算/provider 服务完成；本模块不伪造调用与费用。

## 证据、秘密与范围核算

原 artifact 保持逐字节不可变。登记时先验证真实存储 URI、摘要、大小与允许根，再在独立 `<artifact_scope>:sanitized` scope 提交脱敏派生产物，私有 ledger 保存原件和派生件的关系。使用独立 scope 可避免相同字节、不同 producer 的不可变 manifest 冲突。

可信准备阶段以 `max_source_bytes` 限制完整文本大小，先检验 metadata 上限，再进行有界完整读取和脱敏。过大的原件需要生产者预先提供更小的完整证据产物；不先无界读文件再截断。完整性 hash 可以流式扫描原件，不进入模型上下文。实际模型读取只调用派生件 `open_range`，以返回的原始字节长度入账，不以字符数或解码后的字符串长度估算。

每次 `length` 必须在正整数范围内，offset 必须为非负整数；拒绝负数、布尔值、越文件尾与超过单次上限。累计读数持久写入 `lifecycle_context_usage`，包括重复或重叠读取，按 lifecycle 累计，重新构造 packet 或更换 context hash 不会归零。不同 evidence_mode 共用额度。配置变更也不能隐式扩大已登记限额；当前 API 会拒绝，需要后续明确的可信调整流程。失败读取不交付内容、不计作成功读取。

本入口复用 `SecretRedactor` 并补充 `x-api-token` 等结构化敏感键处理；可信登记的秘密值在文本中替换，敏感键值在结构化 JSON 中替换。秘密出现在 JSON 键名时保守拒绝；最终序列化字节也检查已登记秘密，拒绝数字字段重新带出秘密值，异常不带原值。私钥等无法安全交付的材料也拒绝。完整派生件在切片之前脱敏，因此横跨范围边界的已登记秘密不会泄露片段。这里不声称正则能够识别所有未知秘密；生产者必须登记实际秘密，禁止将秘密库或父会话伪装成共享证据。服务不记录原件、秘密值或读出内容。

句柄不接受路径替代。底层真实 `FileArtifactStore` 拒绝绝对路径、路径穿越、scope 软链与文件软链；每次读取仍重新核对原件完整性及派生件完整性。本批未修改共享 store 或共享 redactor。

## live 与离线封存的 schema 兼容

`ContextPacket.schema_version` 保持 1，增加 `evidence_mode`，缺省值是 `live`。旧 wire 若提供原来必填的非空 `disposition_receipt_ref`，继续解析为 live。live 不接受 None；服务每次构造和读取均调用 `LifecycleSafetyService.verify_disposition_in`，核验当前 owner、有效期、run/scope、资源事实和处置已关闭，并将证明的 run/scope/graph/plan/contract 逐项匹配原失败版本，拒绝用新 work 版本的证明为旧失败赋予现场权限。布尔 `resources_disposed` 不能替代证明。

`offline_sealed` 必须显式选择，wire 强制 `disposition_receipt_ref=None` 且 `tool_capabilities=()`。服务要求至少一项本入口明确登记为 `offline_sealed=True` 的证据，只开放这些证据的只读范围，仍检验实际 lifecycle owner/source/身份。它可以在现场处置证明过期、未关闭或缺失时做封存诊断，但不会获得现场工具。历史证据或旧 packet 不会因为新增字段自动获得封存资格；wire 合法性也不能替代服务端 grant 校验。

这是新解析器接收旧 wire 的向后兼容；旧的严格解析器会拒绝新增字段，不能宣称双向 wire 兼容，也不能把 offline packet 发给旧端。部署时须协调解析器与现代 adapter 的版本。所有 packet 仍禁止未知字段、非法 schema_version 和类型混用。legacy `AgentGateway` 的 context-hash budget、caller `accessible_evidence` 和 `resources_disposed` 只为既有调用保留；现代 lifecycle 不应回退到它。

## 同一处置证明的多 lifecycle 引用

同一可信 work 处置证明可被同 scope 的多个 lifecycle 引用。`LifecycleService.bind_evidence_in(tx, record, evidence_ref, *, kind)` 写 `lifecycle_evidence_bindings`，使用 `evidence_binding_key(lifecycle_id, evidence_ref)` 的配对键；`bind_disposition` 在实际核验处置后使用此接口。`_evidence_in` 先检验配对归属，兼容读取旧全局 `lifecycle_evidence` 时仅接受其原 lifecycle，不覆盖或重解释历史记录。跨 scope 的处置证明仍由 safety 服务拒绝，私有配对证据不能为另一 lifecycle 授权。

新增 `lifecycle_context_grants`、`lifecycle_context_usage` 与 `lifecycle_evidence_bindings` 沿用通用持久 repository，不新增内存额度或任务队列。MemoryRepository 定向测试与真实文件存储验证不等于 PostgreSQL 并发/进程崩溃验收；真实 provider、候选构建及模型提案到下单链仍待后续接入。
