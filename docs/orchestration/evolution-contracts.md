# 候选演进合同

`sandbox_graph.domain.evolution` 对应 ALD-04/07/08 和任务 42.3，提供候选、门禁、独立审查及晋级请求的版本化数据合同。所有模型要求显式 `schema_version=1`，采用 strict、frozen、extra=forbid；嵌套模型同样拒绝未知字段，集合使用 tuple。布尔值不能冒充 revision，摘要必须是 64 位小写 SHA-256 十六进制，Git commit 接受完整 40/64 位小写标识。JSON 导出与 `model_validate_json` 可往返；计算属性不写入 wire payload。

本模块不读文件、不接触主工作树、不创建候选、不派发任务、不认证生产者、不检查账本授权，也不执行风险分类或门禁决策。合同有效表示结构符合要求，不能据此标记授权、测试或隔离通过。

## 提案与候选

`ChangeProposal` 必须包含来源 `LifecycleKey`、基线代码/图引用、非空失败证据和事实、假设、目标行为、允许路径、节点/合同范围、验收引用及 `scope_auth`。目标和事实不能只有空白；假设可为空，合同范围可为空。`scope_auth` 是待核验的范围授权引用，不是模型自己授予权限。

`CandidateRevision` 显式保存候选引用、proposal_id、revision、parent_revision、base_commit、tree/patch/graph/lock 摘要、graph_ref、changed_paths、risk、workspace_ref 和 budget_ref。revision 从 1 开始；首版 parent_revision 必须显式为 null，后续必须为 revision−1。服务另行核验父记录确实属于同一提案和 lineage，不能仅凭这个数字确认谱系。risk 仅接受 R0–R3；最终风险须由可信影响分类器按所有实际命中规则的最高风险确认，不能因为 `.md` 后缀或候选自报 R0 而减免门禁。

候选、门禁、审查和晋级请求均必须显式冻结以下目标字段，没有“最新”或隐式继承默认值：

| 字段 | 含义 |
| --- | --- |
| target_plan_ref / target_plan_hash | 后继运行计划及内容摘要 |
| target_contract_ref / target_contract_hash | 后继运行合同及内容摘要 |
| target_auth_ref / target_auth_hash / target_auth_revision | 后继运行授权快照及版本 |

未改变的目标也记录实际继承的引用、摘要和 revision。服务必须解析这些固定快照并核验存在性、归属、兼容性及撤销状态，不能在晋级时重新读取未固定的最新目标。

`content_identity()` 返回独立的内容身份字典；`candidate_hash` 对带 `kind=candidate-content` 的规范 JSON 做 SHA-256，固定 UTF-8、按键排序、紧凑分隔符、保留 Unicode、不允许 NaN。身份包含 schema_version、base_commit、tree/patch/graph/lock 摘要、graph_ref、risk、排序后的 changed_paths 以及所有冻结目标字段。

candidate_ref、proposal_id、revision、parent_revision、workspace_ref、budget_ref 是来源信息，不参与内容摘要。因此搬移同一内容到另一工作区、给同内容建立新的修订记录，不会改变候选摘要；换 tree、lock、graph、风险、路径或任何目标引用/摘要/revision 都会改变摘要。此设计只允许服务在兼容性复核后复用同内容证明，不允许服务忽略父谱系、预算或授权。

所有记录冻结，内容修改应新建 revision；旧记录和失败日志仍保留。Pydantic 的 `model_copy(update=...)`、`model_construct` 是绕过校验的底层接口，不应用于持久化入口；入口应执行 `model_validate` / `model_validate_json`，并在写入事务中核验派生身份与可信事实。

changed_paths 和 allowed_paths 必须是唯一、规范的相对 POSIX 路径，拒绝绝对路径、反斜线、NUL、空分段、`.`、`..`。每组至多 1024 项，每条至多 4096 字符；文本至多 16384 字符，一般引用至多 512 字符，事实/假设每组至多 1024 项。这些是输入大小边界，不代替运行期的字节、费用和时间预算。路径结构校验不能识别实际 symlink；服务/沙箱仍必须解析实际文件边界、核对允许范围和登记输入、固定脏工作树快照，并阻止主工作树写入。worktree 不代表执行隔离通过。

## 门禁记录

`GateManifest` 绑定 candidate_ref/hash、全部冻结目标、trusted_suite_hash、policy_hash、environment_ref/hash、runner_ref/version/bundle_hash、必需 gate、预期与实际 case 集合、逐 gate/逐 case 结果、日志摘要及 producer_identity/version、observed_at/expires_at。时间是显式 wall 时钟，必须有限且大于零，expiry 必须晚于 observation。

`GateResult` 使用 G0–G5、G6.pre、G6.post，状态仅为 pass/fail/blocked/not_applicable。N/A 必须有 rule_ref、input_diff_hash 和非空 reason，但可信规则真实性仍由门禁服务验证。`GateCaseResult` 保留 pass/fail/blocked/skipped/xfail，不能将 skip/xfail 折算成 pass。gate、预期/实际 case 及逐项结果标识不可重复；逐项结果引用的 case 必须属于实际 case 集合。case 集合及 case_results 每组至多 10000 项。

模型允许保存失败、阻塞、零收集、必需测试缺失等观察，不把“能解析的报告”自动视为晋级绿灯。服务必须比较可信预期集合、实际收集集合、逐项状态及必需 gate；必需 case 缺失、零收集、skip/xfail 或报告缺失均不能晋级。G6.post 是激活后观察，不作为激活前循环依赖。

当 required_gates 或 results 任一包含 G6.post 时，manifest 必须同时提供非空 activation_ref 和 run_ref，分别绑定本次激活记录与实际被观察的后继 run；缺失或只提供一个均拒绝。其余 manifest 的这两个字段只能为空，G5/G6.pre 因而可以在激活之前独立生成，不等待未来 run。服务仍须解析并核验这两个引用的实际关联、候选/目标匹配及观察权限；字符串绑定不代表真实运行身份已认证。

| API | 边界 |
| --- | --- |
| input_hash | 摘要包含候选 hash、冻结目标、suite/policy/environment/runner 身份、排序后的 required_gates 和 expected_case_ids；G6.post 还包含 activation_ref 和 run_ref；不含其他来源引用、实际结果、日志、生产者或观察时间 |
| manifest_hash | 摘要覆盖完整记录，包含逐项结果、日志与 provenance，用于完整性绑定；不提供签名或生产者认证 |
| matches_candidate(candidate) | 仅检查 candidate_hash 及冻结目标逐字段相等；同内容不同来源引用可以匹配，不检查时效或信任 |

换 suite、policy、环境或 runner bundle 会改变 input_hash；改结果/日志/生产者会改变 manifest_hash。服务还必须独立核验生产者、报告完整性、suite/policy/environment/runner 当前预期、有效期、原始日志、所需 gate 和授权；不能仅调用 matches_candidate 放行。伪造 producer 字符串可以通过结构校验，但不能因此获得可信身份。

同一候选的不同激活或不同被观察 run 会产生不同 G6.post input_hash 和 manifest_hash；matches_candidate 仍仅比较候选及冻结目标，不能据此复用另一 activation/run 的观察证明。

## 独立审查与晋级

`ReviewRecord` 绑定 candidate_ref/hash、冻结目标、gate_manifest_ref/hash、policy_hash、coder/reviewer 身份、reviewer_kind、producer_version、审查结果、findings、证据及有限有效期。coder_identity 与 reviewer_identity 必须不同；服务进一步核验实际任务/上下文独立和身份来源。`ReviewFinding` 带 finding_ref、info/warning/blocking 严重度、摘要和非空证据。`approved` 是审查意见，模型审查可以产生该意见，但不产生发布授权；服务另行判定 findings 是否阻止晋级。

`PromotionRecord` 是带围栏的晋级请求，显式绑定 request_id、candidate_ref/hash、gate_manifest_ref/hash、review_ref/hash、发布 authorization_ref/hash/revision、worker_bundle_ref/hash、冻结目标、expected_predecessor_revision、created_at/expires_at。predecessor revision 可为零，不能是负数或布尔值；记录必须有有限且向后的有效期。

target_auth_ref 表示后继运行权限；authorization_ref 表示本次发布批准。二者分开保存、分别核验，不能相互替代，也不能用测试通过或模型 `approved` 意见代替授权。已有仍有效且精确覆盖候选范围与约束的 DecisionRecord 可以复用；需要新授权与否由可信服务决定，模型中没有 authorization_valid、trusted 等自报布尔字段。

PromotionService 仍须在同一 fenced 事务中解析所有引用、比较内容/摘要、重验生产者、gate、review、policy、目标及发布授权、撤销/过期状态、worker bundle/handler 就绪及 predecessor revision，并按 request_id 加输入摘要处理幂等冲突。本合同不会调用 RevisionService 或自动创建后继 run。

## 验证范围

单元文件 `tests/orchestration/unit/test_evolution_models.py` 覆盖 ALU-14/16/18 的合同部分：内容与来源分离、旧 revision 冻结、内容/目标身份变化导致旧 gate 不匹配、suite/policy/environment/runner 输入摘要变化、未知字段拒绝、JSON 往返、路径与大小边界、有限时间和审查/授权分离；还覆盖 ATG-05 的 G6.post 必需激活/run 绑定及身份变化，保留 G6.pre 的激活前独立性。

运行命令：

```bash
/storage/Users/currentUser/IDEProjects/sandbox_graph/.venv/bin/python -m pytest tests/orchestration/unit/test_evolution_models.py -q
```

这些本地纯模型测试不证明真实文件隔离、风险最高等级分类、授权覆盖/撤销判定、可信生产者认证、PostgreSQL 并发或原子晋级。相关服务及 G3/G4 后端验收仍需后续任务完成，不能把合同校验称为 environment_verified 或 adapter_verified。
