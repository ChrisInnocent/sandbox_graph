# 首次部署 helper 与离线资产

该入口为已有环境图准备远端执行端。它使用初始 SSH 账户安装 helper、私有执行授权密钥、recipe catalog 和固定资产；不会安装沙箱产品、创建实例或签发 EnvironmentAcceptance。后续仍运行 `env check/plan/apply/worker` 和独立复检。

## 前置条件

- 明确授权的目标 SSH 账户、已核对的 known_hosts、私有 SSH key 和 `/etc/machine-id`。初始部署应使用后续自动化执行的同一账户；工具不自动 sudo 或改变文件所有者。
- 目标具备 Python 3.11–3.14、venv 和 ensurepip。缺少它们时明确失败，不通过在线安装绕过。
- 与目标 Python/架构兼容的本项目 wheel 和**全部传递依赖 wheel**。本工具不下载、不源码构建依赖；pip 使用 `--isolated --no-index --no-deps`，并运行 `pip check`。不完整 wheel 集合不能激活。
- 一个专用私有安装根，例如 `/opt/sandbox-graph-helper`，其父目录已获写权限。路径只接受 ASCII 字母、数字、下划线、连字符、点及路径分隔符；拒绝相对路径和 `..`。
- 与控制端 `grant_signing_ref` 对应的同一执行授权 key（32–4096 bytes），以及合法 recipe catalog。首次可使用 `{"schema_version":1,"recipes":{}}`，此时 helper 可探测，但没有任何已注册产品安装动作。

## 构造 bundle

控制端输入示意如下。`files` 必须列入 wheelhouse 中全部依赖；下例展示字段格式，不是一套完整依赖清单。

```json
{
  "files": [
    {"path": "/secure/wheels/sandbox_graph-0.1.0-py3-none-any.whl", "kind": "wheel"},
    {"path": "/secure/execution-grant.key", "kind": "grant_key"},
    {"path": "/secure/recipes.json", "kind": "recipes"},
    {"path": "/secure/node-ctl", "kind": "asset"}
  ]
}
```

```bash
.venv/bin/python scripts/environment/bootstrap_helper.py build \
  --input /secure/bootstrap-files.json --bundle /secure/helper-bundle
```

构建时冻结每个文件的 SHA-256/长度，复制到私有 bundle，发布 manifest 前检查源文件没有变化。目标目录必须不存在；未完整发布 manifest 的目录不能用于 deploy。grant key 必须是私有文件，bundle 不应提交到 Git。所有资产保持 0600，目录保持 0700。

目标资产路径为 `<root>/objects/<sha256>`，recipe 可提前引用该路径，无需将大文件嵌入 JSON 或模型上下文。`kind=asset` 可以投递二进制、安装包或镜像归档；投递**不执行** Docker load、产品安装或服务重启。

## 部署与接回环境图

部署输入绑定一个稳定 operation_id。首次 `expected_launcher_sha256` 为 null；明确升级使用上次回执中的 launcher_sha256。地址示例不可连接，需要替换为已授权目标。

```json
{
  "operation_id": "bootstrap/node-a/v1",
  "machine_id": "已核验的目标-machine-id",
  "root": "/opt/sandbox-graph-helper",
  "expected_launcher_sha256": null,
  "ssh": {
    "address": "node-a.example.invalid",
    "port": 22,
    "user": "bench",
    "known_hosts": "/secure/known_hosts",
    "identity_file": "/secure/id_ed25519"
  }
}
```

```bash
.venv/bin/python scripts/environment/bootstrap_helper.py deploy \
  --input /secure/bootstrap-node-a.json --bundle /secure/helper-bundle \
  --output /secure/bootstrap-node-a.receipt.json
```

SSH 使用严格 host key 校验及仓库固定标准库接收器。秘密和用户输入只走 stdin；资产分块传输，总量最多 2 GiB、512 项，单 wheel 最多 128 MiB，展开预算 2 GiB/50000 项。每个安装子进程限时 300 秒、日志最多 1 MiB，整次接收限时 900 秒。接收器先核对 machine_id，再取得与 recipe 执行器相同的宿主锁及 bootstrap 锁。

安装顺序为：接收并校验资产 → 独立版本目录创建 venv → 离线安装 → pip check → 真实 helper 自检 → 冻结目录摘要 → 原子切换入口 → 保存完成回执。旧版本不删除。自检加载实际 grant key、recipe catalog 和执行依赖；它只证明 helper 可用，不证明产品已安装或环境已就绪。

把回执中的入口加入控制端 runtime 文件：

```json
{
  "helper_paths": {
    "node-a.example.invalid": "/opt/sandbox-graph-helper/bin/sandbox-graph-remote-agent"
  }
}
```

将该字段合入现有 runtime，保留其中的 references、grant_signing_ref、recipe 等设置。`helper_paths` 按声明的 SSH address 匹配，普通探测和 `--stdio-grant-v1` 共用此入口；没有配置的地址继续使用已有 PATH 入口。然后用新连接执行环境检查。bootstrap 不取消 PostgreSQL、授权、宿主预约或 nonce/grant 要求。

## 中断恢复与维护

重复运行**相同** deploy 输入和 bundle。原 request ID 绑定整个输入；改机器、bundle、根路径或 CAS 值须使用明确的新请求，不能覆写历史请求。

- 构建期间断开：子进程继承宿主锁，仍存活时第二次安装被拒绝；结束后最多在三个独立尝试目录中构建，不复用未知半成品。
- 已 ready、尚未切入口：复验目录摘要后继续 CAS。
- 入口已切、最终回执未写：同请求从 ready 记录和入口内容恢复，不再次安装。
- 完成后的入口被修改或删除：报告漂移，不自动恢复旧版本。既有 grant key 与新 bundle 不同也拒绝，轮换需要单独的授权流程。
- 任何失败：保留 `root/requests/<operation-id-sha256>.json`、`root/releases/<operation-id-sha256>/<attempt>/` 和私有步骤日志。不要删除目录后使用原 request ID 冒充第一次执行。

请求记录包含每个安装步骤的 attempt、started/completed/failed/unknown、退出码（若已确定）、日志摘要及私有位置；公开回执不包含日志原文、grant key 或 SSH key。该账本为单宿主部署账本，不冒充图的 PostgreSQL 业务账本。旧 release 的回收需先确认无在途执行，本工具不自动回收。

## 验证

本地测试通过真实 venv/pip/helper 进程及合成 wheel 验证协议、离线安装、CAS、重入、宿主锁继承和秘密隔离；不将合成 wheel 记为本项目发布包部署验收。真实项目的配置自检另用实际 `remote_agent.bootstrap_check` 测试。

真实目标验收入口：

```bash
SANDBOX_GRAPH_BOOTSTRAP_CONTRACT=/secure/bootstrap-live.json \
  .venv/bin/python -m pytest tests/environment/live/test_bootstrap_live.py -q
```

合同格式为 `{"bundle":"/secure/helper-bundle","deployment":{上述部署输入}}`，显式提供该合同会部署到其中的 SSH 目标。harness 验证首次部署、同请求重入及独立新连接的 helper 身份；未提供合同则明确 skip。真实 SSH 断连/主机崩溃矩阵仍需现场验收，单元测试的子进程硬杀不能替代它。
