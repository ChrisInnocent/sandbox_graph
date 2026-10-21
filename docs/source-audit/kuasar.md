# Kuasar 源码审计与统一测试接口

审计日期：2026-10-20。本报告依据本地源码，不是本次运行结果。未运行沙箱、远端测试、构建或性能测试；未读取运行时凭据。此前审计只读，本文件是唯一获授权写入的交付物。

## 版本与证据边界

2026-10-21 补查与设计修正：创建可以按经核验的状态／日志完成，不再强制命令计时。`sandbox launch terminal` 的成功路径在 Running 提交后，带 `starting_total_duration`，起点来自接纳后 `SetAcceptedAt`，不等于客户端完整请求时间；详情 startedAt 是 CreatedUnix。精确证据、字段与采集合同见 [节点设计第 3 节](../../openspec/changes/sandbox-evaluation-workflow/test-nodes.md)。本文后续 exec 与新增健康停止建议为早期方法选项；Cube 原增长口径以列表净增长为准，不能与新增成功数混淆。

节点级分工：R1/T0 的版本／引用／方法字段校验脚本化，模型核验日志起点、状态提交、artifact 和 pause/export 语义；E1/E2/C1–C3 按角色脚本检查并执行已定配置；N00–N10 脚本处理异步等待、日志关联去重、按所选终点释放槽、每秒创建吞吐、PSS/CPU、原／统一公式和所有权清理。日志内部 duration 不能直接推算客户端吞吐，源码已知不等于已运行；Guest OS 减项和缺失适配保持待验证。完整职责和 planned/draft 状态见 [职责表](../../openspec/changes/sandbox-evaluation-workflow/node-automation.md)，全部内部子图见 [Mermaid](../../openspec/changes/sandbox-evaluation-workflow/node-workflows.md)。

| 仓库 | 本地 HEAD | 审计时工作树 |
| --- | --- | --- |
| kuasar-sandbox | `ba218fc5f20973621277484b9144a8e838ffe840` | clean |
| kuasar-components/orchestrator | `c14da165ecc14de0e5c0afbfba2e1b737d64ee7c` | clean |
| kuasar-components/connector | `8fc4b6a266ff551b2747a83f43ce1c8a717ef187` | clean |
| kuasar-components/sandboxer | `3f4978c39499c98acd5e8a7b32d450bb66ea4db9` | clean |

脱敏检查的 origin 均指向 `github.com/kuasar-sandbox/<对应仓库>.git`。未 fetch 或核验远端 HEAD，因此只证明上述本地提交的行为，不能证明当前官方 main、某个发行版或部署中二进制完全一致。`kuasar-components` 本身不是独立仓库，在该目录读取 HEAD 会得到外层 sandbox_graph 的版本，不能用作组件版本。审计范围内未发现适用 AGENTS.md。

文档中源码链接后的行号对应上述提交；接口、测试代码和文档声明不代表当前环境已通过这些测试。

## 创建的异步语义

1. [API 路由](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L461) 注册 `POST /sandboxes`。请求参数在同文件 L336–345：`templateID`、`timeout`、`metadata`、`envVars`、`secure`、`autoPauseMemory`。
2. [创建 handler](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L567) 调用 `core.Create` 后返回 **HTTP 201**。
3. [初始沙箱](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/create.go#L193) 的状态是 `starting`。不是 ready。
4. [准入路径](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/orch.go#L338) 先持久化 starting、发布路由并等待 route-applied barrier，L375–379 调 `launches.Start` 后返回。HTTP 返回包含准入及路由屏障，不等待完整启动。
5. [创建响应](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L1335) 包括 `sandboxID`、`templateID`、`domain`、`envdVersion` 和访问令牌，**不包含 state**。获得 SDK 对象或 ID 不表示 ready。
6. [GET /sandboxes/{id}](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L575) 返回 detail；[L1356–1364](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L1356) 包含 `state`。状态枚举为 `starting/running/paused/deleting/dead`：[types.go L219–224](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/types.go#L219)。
7. [真正的 running 转换](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/sandbox_task_prepare.go#L265) 先等 runtime readiness，再执行 e2b `envdInit`，再 `CommitPreparedRunning`，最后更新 running。envd 初始化是 [POST /init，要求204](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/orch.go#L3145)。

该公共 API 路由没有通用创建任务 `/tasks/{id}` 查询接口。内部 run-id/launch attempt 不是公共任务查询合同，统一适配器应轮询沙箱 detail。

[生命周期 E2E](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/cases/orchestrator.lifecycle.sh#L205) 明确允许 201 后状态为 starting 或 running，并验证立即发出的数据请求 park 到 running。其 [wait_sandbox_state](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/execute_sandbox.sh#L66) 读取本地 sqlite（L11–17），不能直接复制为跨平台公共 HTTP 适配器。

统一测试分别记录 `request→201`、`request→running`、`request→首个唯一标记exec成功`。最终 exec 需要同时校验输出和退出码，HTTP200或 health 成功不能替代它。

## Python HTTP 适配器所需接口

### 控制面与 envd 数据面

控制面测试使用 `Host: api.<domain>` 和 `X-API-KEY`，body 为普通 JSON：[execute_client.sh L3–15](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/execute_client.sh#L3)。生产 URL 的 scheme、端口、TLS 和证书应来自部署配置，不能固定为测试的 localhost 端口。

envd 数据面可使用：

- `Host: 49983-<sandboxID>.<domain>`，或 `E2b-Sandbox-Id: <sandboxID>` 加 `E2b-Sandbox-Port: 49983`；若 ID header 存在，以 header 为准。[proxy.go L532–573](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/proxy/proxy.go#L532)
- `X-Access-Token: <创建或connect返回的envdAccessToken>`，不是控制面的 API key，也不是 forwardAccessToken。[proxy/auth.go L12–20](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/proxy/auth.go#L12)
- 连接外部代理监听地址；49983 是逻辑 guest envd 端口，不意味着外部 TCP 必须连接49983。[测试 dp helper](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/execute_client.sh#L197) 连接 PROXY_PORT，并单独设置 Host。

对 e2b profile，逻辑49983/49999路由到 UDS；bare profile 的同号端口是普通TCP服务：[proxy.go L205–228](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/proxy/proxy.go#L205)。代理保留普通HTTP请求并流式转发响应：[http.go L17–58](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/proxy/http.go#L17)。

**已有真实 envd Connect JSON 协议 fixture**：[envd_exec.py L9–26](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/envd_exec.py#L9)：

```text
POST /process.Process/Start
Content-Type: application/connect+json
Connect-Protocol-Version: 1
X-Access-Token: <envdAccessToken>

请求payload JSON:
{"process":{"cmd":"/bin/sh","args":["-c","<command>"]}}
```

请求 body 不是裸JSON，而是 `0x00 + uint32_big_endian(JSON字节长度) + JSON字节`。响应由同样的5字节帧头分隔；`flag & 2` 是结束/错误元数据帧；普通帧 `event.data.stdout/stderr` 为base64字节；`event.end.exitCode` 为命令退出码。适配器必须检查HTTP状态、完整帧、协议错误、end事件、退出码及输出nonce。fixture 的简化 parser 不是完整鲁棒性规范，不应把缺失退出码默认为成功。

[envd_start.py L10–17](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/envd_start.py#L10) 另有 `cwd: "/home/user"` 和 `Authorization: Basic base64("user:")`，并在收到 `event.start.pid` 后关闭流；这种“启动进程”确认不能代替命令完成。

证据强度：仓库 fixture 实际通过本地 envd UDS调用该RPC；公共代理代码支持相同HTTP转发，因此可据源码设计公共HTTP适配，但本审计没有实际跑通“公网代理→该RPC”。必须在运行前 smoke 验证这一组合链路。

**native exec 是另一种协议**。`E2b-Sandbox-Service: exec` 要求 HTTP CONNECT，普通POST会405：[proxy.go L305–308](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/proxy/proxy.go#L305)。不要把 native exec token/header 套在 envd `/process.Process/Start` 上。

### 模板与资源规格

模板ID格式：`<profile>-<kind>-<base64url(canonical-portable-ref)>`；kind 为 `img/sbx/snp`，profile 可为 e2b。编码使用 URL-safe Base64，**不带 `=` padding**：[types.go L253–306](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/types.go#L253)。

引用必须是规范 portable Manifest 或 located-file artifact ref；file img 指向 `.image/.bundle`，snp 指向 `.snapshot/.bundle`：[types.go L284–320](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/types.go#L284)。**不能将 `ubuntu:22.04` 等 Docker 镜像名直接base64编码成e2b-img后缀**。OCI→Image artifact 的准备/构建需单列，不得伪装成已有模板创建。

`img→LaunchImage`，`sbx→LaunchCold`，`snp→LaunchMemory`：[types.go L203–213](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/types.go#L203)。

创建资源采用以下二者之一：

```json
{
  "templateID": "<canonical-template-id>",
  "timeout": 120,
  "metadata": {
    "kuasar-sandbox.resource": "{\"capacity\":{\"cpu\":2,\"memory\":\"2GiB\"},\"allocatable\":{\"cpu\":0.5,\"memory\":\"256MiB\"},\"startup\":{\"memory\":\"512MiB\"}}"
  }
}
```

或将内层对象直接作为 `X-Kuasar-Sandbox-Resource` header 的JSON值。metadata值必须是JSON字符串，而header值是JSON对象文本。[node.md L860–899](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/docs/node.md#L860)、[header合并 L560–565](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L560)。

容量capacity与allocatable/startup是不同概念。镜像冷启允许资源覆盖，恢复的portable artifact capacity构成约束：[node.md L326](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/docs/node.md#L326)、[L913](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/docs/node.md#L913)。

重要代码/文档差异：当前 [CreateReq L336–345](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L336) 没有顶层 `cpuCount/memoryMB`；[创建decoder L550](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L550) 允许unknown，因此不可假定顶层这两字段生效。它们明确存在于模板Build接口，不能将Build资源规则搬到Sandbox Create。统一适配器应使用上述resource载体，并验证实际资源；GET detail中的cpuCount/memoryMB取API全局 `a.res`（L1359–1361），不能独自证明单实例配置已生效。

### 暂停、恢复、导出、导入

| 操作 | 请求及返回 | 源码 |
| --- | --- | --- |
| 暂停 | `POST /sandboxes/{id}/pause`，`{}`或`{"memory":true}`默认捕获内存；`{"memory":false}`只保留磁盘/冷恢复；成功204，无artifact返回 | [api.go L756–808](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L756) |
| 快照策略 | memory=true时可传 `checkpoint_merge_ref`、`checkpoint_drop_caches`；memory=false带这些字段会400 | [api.go L772–799](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L772) |
| 恢复 | `POST /sandboxes/{id}/connect`，body `{"timeout":120,"memory":true}`，字段可选；成功200，返回与create相同的sandbox响应/访问令牌；仍要等待running和exec | [api.go L635–685](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L635) |
| 导出 | `POST /sandboxes/{id}/export`，`{"toTemplate":true,"keepSource":true}`；200，`result`为可复用templateID | [api.go L1226–1237](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L1226) |
| 迁移token | 同一export，`toTemplate:false`使`result`为opaque `kmt1.*` token；`keepSource`独立控制源是否保留 | [migrate.go L146–167](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/migrate.go#L146) |
| 导入 | `POST /sandboxes/import`，`{"token":"<opaque>","sandboxID":"<optional-target>"}`；200 `{"sandboxID":"..."}`；创建paused状态，再connect | [api.go L1240–1284](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L1240)、[migrate.go L496–502](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/migrate.go#L496) |
| 自动导入并恢复 | connect可携带 `X-Kuasar-Migration-Token`，目标不存在时导入再恢复 | [api.go L286–288](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L286) |

Export必须先paused且ResumeSource有效，否则400：[migrate.go L81–83](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/migrate.go#L81)。`keepSource:true`保留源行、checkpoint及恢复来源不变：[L232–238](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/orch/migrate.go#L232)；省略该bool默认false，会走源终结流程。组合克隆/快照应显式true。

导出响应结构为 `{"result": "...", "snapshotRef": "...可选...", "sandboxRef": "...", "removedRefs": [...]}`：[types/export.go L12–15](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/types/export.go#L12)、[PublishReport L21–25](https://github.com/kuasar-sandbox/sandboxer/blob/3f4978c39499c98acd5e8a7b32d450bb66ea4db9/pkg/artifact/publish_result.go#L21)。`removedRefs`不是授权删除共享产物。

## B01–B10 能力及现有入口

| 编号 | 可测能力与入口 | 统一测试边界 |
| --- | --- | --- |
| B01 快照模板创建 | e2b-snp模板POST；[orchestrator.lifecycle.sh L190–245](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/cases/orchestrator.lifecycle.sh#L190) | 新ID创建，到running+exec；不是旧实例恢复 |
| B02 镜像创建 | e2b-img；[sandbox.lifecycle.sh L141–174](https://github.com/kuasar-sandbox/sandboxer/blob/3f4978c39499c98acd5e8a7b32d450bb66ea4db9/test/e2e/cases/sandbox.lifecycle.sh#L141)、`image.manifest-boot.sh` | 记录镜像准备、下载、缓存是否计时；不能与快照类合并 |
| B03 暂停 | pause默认memory=true；[orchestrator.snapshot.sh L25–27](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/cases/orchestrator.snapshot.sh#L25) | 捕获与暂停绑定，区别于仅freeze；另测资源释放 |
| B04 恢复 | connect或数据面唤醒；[orchestrator.snapshot.sh L235–249](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/cases/orchestrator.snapshot.sh#L235) | 200接受不等于ready；明确memory模式 |
| B05 克隆 | pause→export(toTemplate=true,keepSource=true)→POST新实例；[orchestrator.snapshot.sh L117–140](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/cases/orchestrator.snapshot.sh#L117) | 无独立clone endpoint；分开记录source捕获、模板发布、新实例启动；源保留但处于paused，恢复源需另connect |
| B06 删除 | DELETE `/sandboxes/{id}`成功204；[api.go L622–632](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/internal/api/api.go#L622) | 接受删除、查询消失、资源清理分别记录 |
| B07 命令 | envd Start RPC；[envd_exec.py L9–26](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/envd_exec.py#L9)；native exec强校验 [execute_client.sh L79–109](https://github.com/kuasar-sandbox/orchestrator/blob/c14da165ecc14de0e5c0afbfba2e1b737d64ee7c/test/e2e/lib/execute_client.sh#L79) | 两种协议分开；统一工作负载和输出/exit判定 |
| B08 快照 | 高层pause(memory=true)；如需portable产物再export；底层 [snapshot.restore-config.sh L100–105](https://github.com/kuasar-sandbox/sandboxer/blob/3f4978c39499c98acd5e8a7b32d450bb66ea4db9/test/e2e/cases/snapshot.restore-config.sh#L100) | 无公共独立snapshot route；分别命名本地捕获、发布；源暂停副作用需记录 |
| B09 底噪 | [density-perf.sh MODE=idle L19–20](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L19) | 只提供闲置内存负载，未形成完整CPU/IO/网络/服务底噪基准 |
| B10 密度 | [density-perf.sh](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh)、[Agent校验器](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/agent-density/benchmark.py) | 固定N资源回归、业务结果校验、实际递增容量三者不同 |

## 现有计时位置

- [sandbox-perf.sh L4](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/sandbox-perf.sh#L4) 明确case wall含setup/assertions/cleanup，**不是create-to-ready**；L43–56从公共runner `wall_seconds`取值。
- [sandbox-perf-manifest.sh L615–633](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/sandbox-perf-manifest.sh#L615) 测底层restore启动至首次TICK，跳过公共API和代理链路。
- [density-perf.sh L457–476](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L457) 的launch_wall含cgroup/TAP/diff/mkfs/YAML以及后台进程启动，结束时未逐个确认ready。

## 密度原公式、标签问题与缺口

原脚本 [L597–604](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L597)：

```bash
final_avail=$(read_avail)
peak_drop=$((host_baseline_avail - min_avail))
post_drop=$((host_baseline_avail - final_avail))
peak_per_sb=$(awk -v d="$peak_drop" -v n="$N" 'BEGIN{printf "%.1f", d/n}')
post_per_sb=$(awk -v d="$post_drop" -v n="$N" 'BEGIN{printf "%.1f", d/n}')
projected=$(awk -v a="$min_avail" -v p="$peak_per_sb" 'BEGIN{
    if (p > 0.5) printf "%d", a/p; else print "n/a (per-sandbox cost too small to project)"
}')
```

设 `A0=host_baseline_avail`，`Amin=min_avail`，`d=round((A0-Amin)/N,1)`；输出 `projected=int(Amin/d)`。

- A0在daemon启动后、首实例启动前读取：[L452–453](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L452)。
- 默认N=8，为请求数而非实际ready数；失败/未ready仍作为分母会低估成本。
- Amin为每5秒采样的最小MemAvailable，首次采样晚于启动循环及额外等待，可能漏过启动峰：[L478–479](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L478)、[L507–521](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L507)。
- [L642](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L642) 标签是“projected ceiling”。数学上公式计算的是剩余内存可再增加数，未加已存在N，也未扣安全预留，不能直接当总容量。
- 控制器配置有HOST_RES_MEM默认1GiB、HIGH_FACTOR=.85、STARTUP_FACTOR=.50等，但它们没有进入该projected公式：[L132–145](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L132)。
- final_avail在停止sandbox-ctl之后才读取：[L573–601](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L573)；after-release不是“仍存活idle实例”的单实例成本。
- PASS仅依据admit/reject/cancel/queue/OOM/hang，无逐个guest任务完成/exec/输出断言：[L606–628](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L606)。
- 脚本只激活资源控制器、直接启动sandbox-ctl，未走完整E2B链路：[L316–319](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L316)、[L467–471](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/density-perf.sh#L467)。

文档本身要求区分guest OOM与host cgroup OOM，不能由零host OOM推定业务存活：[perf.md L148–157](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/docs/perf.md#L148)。

## 跨移 Cube 的统一口径建议

保留两组结果，不相互冒充：

1. **实测密度**：若采用“连续两批成功数<10”停止规则，成功定义为本批新实例在截止内running+唯一exec标记正确；保留先前实例，并持续复验存量。记录requested/accepted/ready/exec-ok/存量健康数，报告最大同时健康存活数、停止原因及失败类别。阈值规则是协议，不证明物理极限；配额、限流、启动超时和并发上限可能先触发。
2. **资源估算**：全套服务启动、零实例时取稳定宿主基线；在多个健康N上采样增量，以斜率或稳健成本估算。预先定义安全预留，内存模型可记 `floor((A0-reserve)/cost)`，结合CPU、磁盘、配额约束另给上限。估算值明确标为模型预测，不与实测健康实例数合并排序。

必须统一模板内容、预热/缓存、CPU/memory capacity、allocatable/startup策略、活动比例、存活窗口和exec负载；冻结、磁盘冷恢复、内存恢复、发布快照的副作用不同，不能仅凭API名字认为等价。

[agent-density README L3–7](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/agent-density/README.md#L3) 说明校验器不启动MicroVM，launcher是外部prepared BMS fixture；[L55–83](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/ba218fc5f20973621277484b9144a8e838ffe840/test/perf/agent-density/README.md#L55) 的3/16并发与24/48任务数据是特定负载历史记录，raw证据不在仓库。不能借作本次三沙箱统一实测结果。

## 2026-10-21 镜像完整路径补查

本地 `test/e2e/cases/builder.image.sh` 与 `test/e2e/lib/builder_client.sh` 证明公共 builder 路径：POST /v3/templates 注册 templateID/buildID，POST /v2/templates/{tid}/builds/{bid} 提交 fromImage，GET /templates/{tid}/builds/{bid}/status 等 building/ready/error；ready 返回持久 e2b-img templateID，再创建并等 running。注册 ID 与持久 ID 不混用，builder 规格与最终 sandbox 规格独立。完整适配设计见 [BCD-05](../../openspec/changes/sandbox-evaluation-workflow/benchmark-design.md)。未执行 fixture 或真实构建，不据源码存在宣称已跑通；原 fixture 的全套环境准备不得直接照搬到用户部署。
