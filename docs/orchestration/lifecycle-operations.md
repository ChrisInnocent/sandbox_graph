# 生命周期部署与操作入口

基础运行仍由 `env worker` 或 `eval worker` 领取持久任务。可选 `--lifecycle-runtime` 是独立、受控的运行看护配置；运行 ID 生成后可以配置 binding，不改写该运行已冻结的测试配置摘要。没有该参数时维持原有确定性流程。

配置根接受 `schema_version: 1`、`bindings`、可选 `model`、`reconciliation`、`evolution`。不接受模型提供的 factory 或 shell。模型 bridge 使用本项目 [provider 协议](agent-provider.md)，不是编辑器工具的隐式转发。

每个 binding 必须提供 `binding_ref/run_ref/scope_ref/node_id/policy_ref/authorization_ref/principal`，以及 `limits/lineage_limits/deadline/lineage_deadline/safety_limits/lineage_safety_limits/safety_deadline/cost_unit`。预算字段和登记方法见 [监督配置](lifecycle-supervision.md)。policy、权限和预算由受信控制面预登记；worker 配置不会自动授予权限。环境组、分析和报告使用自身 scope，不构造假的 WorkInstance。

`model` 包含：

- `bridge`：AgentBridge 的 schema_version、provider_id、endpoint、scope、超时与请求/响应字节上限，禁止 inline bearer_token。
- `credential_ref`：已登记的 `secret://`；路径、摘要和私有权限通过环境 runtime 的 references 解析。凭据仅在进程内注入，并登记为上下文脱敏哨兵。
- `team`：ModelTeamConfig 的模型 ID/版本、token/cost/字节/观察次数/截止预算。
- `context`：LifecycleContextService 的 artifact_scope 和各项证据字节上限。

`evolution` 已接入普通 `env/eval worker --lifecycle-runtime`，依次装配 CandidateWorkspace、独立 coding/review provider、CandidateWorker、CandidateGateRuntime、EvolutionCoordinator 与 CandidateSafety。配置必须显式提供 repository、candidate_root、bindings、coding、review、gates、lease_seconds、owner，可选 isolation。每个候选 binding 包含 proposal、target，以及按修订号字符串索引的 revision_targets；每个新图修订绑定自己的冻结 plan/contract/auth，不沿用上一修订的目标。

coding/review 分别包含 provider（bridge、credential_ref）、config（CodingConfig）、identity。reviewer 身份与 coder 分开，bridge scope 必须指向该 proposal。isolation 使用 backend_id、rootfs、rootfs_hash、固定 commands、state_dir；Bubblewrap 清除环境、网络与宿主挂载，候选测试只读挂载。缺 backend 能力会 blocked，不把 worktree 当隔离。

gates 包含 profile_ref、environment_ref/environment_hash、producer_identity/version、runner_ref/version/runner_bundle_hash、cases，及可选 case_timeout_s/ttl_s。cases 支持固定 `observer: controller-pytest.v1` 或由操作者安装在源码树和候选树之外的摘要锁定 async 观察器。后者的字段为 path/sha256/callable/version/configuration。这是可信控制面代码，不是模型代码加载接口；producer/profile/环境资格必须预登记，配置不会自行授权。

controller-pytest 的 configuration 为 suite_root/suite_hash/required_ids/output_root/command_ref/stdin/timeout_s，可选 candidate_tests_command_ref。先收集固定 controller suite，再由隔离程序产出观察数据，最后 controller pytest 执行断言并核对完整 setup/call/teardown、JUnit、退出码和 suite 摘要。suite 通过 `suite_digest` 固定；输出不挂载给候选。测试通过 `SANDBOX_OBSERVATIONS` 读取 JSON，不能 import 候选代码。候选新增测试在另一原请求下隔离执行，其自报成功不能覆盖 controller 失败。

## 操作命令

```bash
python -m sandbox_graph lifecycle status --lifecycle-id '实际 lifecycle ID'
python -m sandbox_graph lifecycle request-repair --input repair-command.json
python -m sandbox_graph lifecycle request-change --input change-command.json --runtime candidate-paths.json
python -m sandbox_graph lifecycle decision --input decision-command.json
python -m sandbox_graph lifecycle cancel --input cancel-command.json
python -m sandbox_graph lifecycle rollout --input rollout-command.json
python -m sandbox_graph lifecycle rollback --input rollback-request.json
python -m sandbox_graph lifecycle gates show --manifest-ref '实际 manifest 引用'
```

这些命令使用 `SANDBOX_GRAPH_POSTGRES_DSN`，没有默认数据库。写入 envelope 必须恰好包含 `request_id/lifecycle_id/expected_revision/principal/authorization_ref/input`。同 request_id 同输入复用原结果，改输入拒绝；陈旧 revision 或作用域、期限、撤销状态不符拒绝。claim 和外部候选准备后再次核验控制授权，避免撤销窗口。

`request-repair.input` 为 `{ "evidence_refs": ["实际证据引用"], "action_ref": "已登记动作" }`；`decision.input` 可增加 route 和 proposal_ref；`cancel.input` 为 `{ "reason": "实际取消原因" }`。request-change 需要严格 ChangeProposal 与 TargetBinding，candidate-paths.json 只接受 repository、candidate_root 两个绝对路径，候选根不得处于用户仓库内。

取消进入统一 StopJobs 协议，仅关闭目标 lifecycle 的工作；请求成功不等于远端已停止。原 provider/operation 的 children_stopped、清理证明和资源处置仍由原请求对账。不得通过改 request_id 绕过未知状态。

`rollout` 使用独立 envelope：request_id/run_ref/expected_revision/principal/authorization_ref/mode/scopes；首次 expected_revision 为 null，后续为当前 rollout revision。scopes 为 `{scope_ref,node_ids}` 数组。模式 off/actions/scoped/candidate 分别允许无新操作、已登记修复、修复加模型诊断、再加候选演进。控制面需登记 lifecycle_rollout_authorizations，含精确 principal/run_ref、允许 modes/scope_refs、expires_at/revoked。开关只收紧原 policy、预算和授权，不能扩大权限或重置预算；关闭后原请求清理仍运行。无新开关记录的旧 worker 保持既有显式 binding 行为；无 lifecycle-runtime 的部署默认不创建自动看护任务。

`rollback` 输入为严格 RollbackRecord：晋级请求字段加 failed_run_ref/reason。必须已有 release-observer.v1 的真实失败记录、目标版本当前 gate/review、精确 rollback_authorizations、冻结 bundle 和实际 ready worker。缺 worker 保存 verified_waiting_worker；重复 request_id 保留原结果，变更输入拒绝。回退创建新的 run/thread，旧运行保持原 checkpoint 与原操作清理。不会把人工批准当成失败测试的豁免。

候选 worker 可通过 `eval worker --runtime ... --bundle-ref ...` 启动；解释器须实际导入该不可变 bundle 的 sandbox_graph/sandbox_bench。启动时核对 requirements.lock 的已安装版本和模块来源，多个 worker 可登记同一冻结 deployment。当前依赖证明为 installed_version_match，不能当作 wheel 内容供应链证明。

## 固定门禁

```bash
python -m sandbox_graph lifecycle gates run --profile development --output artifacts/gates/development-current
python -m sandbox_graph lifecycle gates run --profile persistent --output artifacts/gates/persistent-current
python -m sandbox_graph lifecycle gates run --profile isolation --output artifacts/gates/isolation-current
python -m sandbox_graph lifecycle gates run --profile release --output artifacts/gates/release-current
```

输出目录每次使用新目录。维护者增加测试后运行 `python scripts/freeze_lifecycle_profiles.py` 更新并审查固定清单；执行门禁本身不刷新 required_ids。persistent 仅使用专用 SANDBOX_GRAPH_TEST_POSTGRES_DSN；isolation、release 分别要求显式的测试合同路径。缺条件、必需 skip/xfail、收集不符、无日志均不能 pass。

上述本地 pytest 产物标记 local_only，不具备生产候选晋级权。TrustedGateRunner 的 controller observer、登记 producer、冻结 suite、有效期与目标摘要另行核验；观察器配置摘要也属于 suite 身份。
