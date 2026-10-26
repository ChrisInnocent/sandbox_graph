# 环境图命令与恢复

入口为 `.venv/bin/python -m sandbox_graph env --help`。环境声明不依赖测试 Case；只读检查可以不连接 PostgreSQL。实际准备与 worker 使用 `SANDBOX_GRAPH_POSTGRES_DSN`，数据库先运行 `python -m sandbox_graph migrate`。

当前命令已经接入真实 SSH/逐步骤执行协议。产品的完整配置生效、注册、鉴权和 canary 验证器仍在补齐；缺少这些证明时报告保持 blocked，不能据安装命令返回 0 直接开始性能测试。任务状态见 [环境 tasks](../openspec/changes/implement-sandbox-environment-adapters/tasks.md)。

## 输入

- `environment.yaml`：`EnvironmentDeclaration`，包含所选环境组的真实 machine ID、SSH、角色、制品摘要、精确源码集合和凭据引用。参考 [声明样例](examples/environment/)。
- `runtime.json`：执行主机的引用映射与登记动作，结构为 `schema_version: 1`、`references`、`actions`、`bindings`、`grant_signing_ref`。`references` 将 `secret://…` / `artifact://…` 映射到 `{path, sha256}`；文件必须在本机存在，secret 文件权限不能向其他用户开放。内容是路径和摘要，不是密钥值。
- `actions` 使用 `ActionDefinition` 字段，绑定产品/profile、repair_codes、action_ref、roles、timeout_seconds 和 reconcile_rule。
- `bindings` 每条绑定 `node_id`、`action_ref`、`recipe`、`recipe_hash`、本地 `catalog_path` 与目标文件的 `expected_before` 摘要。原语由 `products/{cubesandbox,kuasar,agentenv}.py` 的 `install_recipe` 生成；本地和远端必须安装同一摘要的 recipe，输入不是任意 shell 命令。
- `authority.json` 是显式执行授权：`declaration_hash`、`node_ids`、`actions`、`expires_at`（UTC epoch 秒）、`revoked: false`，必须覆盖冻结计划。

recipe catalog 格式为 `{schema_version: 1, recipes: {NAME: {digest, steps}}}`。`steps` 是 `dataclasses.asdict(RecipeStep)` 列表，`digest` 使用项目 `canonical_hash`。远端固定读取 `/etc/sandbox-graph/recipes.json`；该文件不得由其他身份写入。helper 的 signing key 固定为 `/etc/sandbox-graph/execution-grant.key`，至少 32 字节且仅所属用户可读，需与控制端 `grant_signing_ref` 对应。远端需安装本项目的 `sandbox-graph-remote-agent` 入口；环境声明不能覆盖其命令。

## 使用

下面的输入路径是操作员准备的实际文件，不是仓库自带可运行环境：

```bash
.venv/bin/python -m sandbox_graph env check --environment environment.yaml --runtime runtime.json --artifacts artifacts/environment --output artifacts/environment/check-ref.json
.venv/bin/python -m sandbox_graph env plan --environment environment.yaml --runtime runtime.json --artifacts artifacts/environment --report artifacts/environment/check-ref.json --output artifacts/environment/plan.json
.venv/bin/python -m sandbox_graph env apply --runtime runtime.json --artifacts artifacts/environment --plan artifacts/environment/plan.json --authority authority.json --request-id prepare-001
.venv/bin/python -m sandbox_graph env worker --runtime runtime.json --artifacts artifacts/environment --once
```

`check` 返回 0 表示全部已选节点的所需证明已满足，返回 2 表示有缺口或输入/访问错误。两种情况都应先看报告。`plan` 检查报告摘要、声明绑定和有效期；`apply` 入库后只排队，退出客户端不取消已提交任务。worker 校验冻结的 runtime 摘要，只领取环境任务，避免领走其他版本的测试任务。正式长期 worker 去掉 `--once`。

```bash
.venv/bin/python -m sandbox_graph env status --run-id RUN_ID
.venv/bin/python -m sandbox_graph env reconcile --run-id RUN_ID --request-id reconcile-001
.venv/bin/python -m sandbox_graph env report --run-id RUN_ID
```

`reconcile` 创建新的观察 activation，沿用原 plan/operation/step ID；不会把未知安装当成尚未提交。已完成步骤不重复安装、配置或生成凭据。原资源域保留到原操作和归属资源可验证关闭；存在未知状态时不做通用回滚。只有新修复轮次才产生新 repair plan，并继续扣原 scope 的累计步骤/轮次预算。

报告 JSON 是权威交付件，Markdown/Excel 从同一 manifest 生成。缺少原始安装历史的既有环境标记 `installation_history: unavailable`。原始敏感配置及凭据只留私有存储，公开报告检查引用的原始证据是否泄密。
