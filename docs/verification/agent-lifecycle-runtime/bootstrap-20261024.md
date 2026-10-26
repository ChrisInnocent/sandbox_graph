# Helper 初始化与离线资产投递实施记录

日期：2026-10-24。对应 `implement-sandbox-environment-adapters` 的 EAD-29、D18、EAT-36 和任务 11.1—11.4。使用方法见 [初始化手册](../../environment-bootstrap.md)。

## 已交付

- 固定标准库接收器核对机器身份和完整资产摘要，在独立版本目录离线安装 wheel、执行 pip check 与实际 helper 配置自检，然后 CAS 切换入口；缺依赖不在线回退。
- 私有 bundle、分块 SSH 投递、步骤日志/回执及 CLI；资产可供产品 recipe 使用，但投递不执行产品安装或 Docker load。
- 原请求绑定、最多三次独立构建、宿主锁继承、ready 目录完整性校验、入口切换后缺回执的恢复、完成后入口漂移拒绝与禁止隐式 grant key 轮换。
- runtime 的 helper_paths 同时绑定普通探测及 grant 协议；版本私有配置根接入现有 remote_agent，原默认入口保留兼容。

## 验证证据

证据目录为 `artifacts/reviews/bootstrap-20261024/`。新增定向测试结果：**38 passed、1 skipped**（`contracts.xml`）。实际运行临时 venv/pip/子进程，覆盖控制进程 SIGKILL 后宿主锁继续持有、离线 wheel 安装、目录/CAS 漂移及输入边界。安装夹具使用合成 wheel；本项目真实配置自检单独验证，不能将合成包称为发布包部署验收。

TDD 先观察到缺 bootstrap 模块、缺 helper_path 参数、已完成入口被删除后错误重建的失败，再实现修复。中间 wheel 校验路径拼接错误导致四例失败，已修复并包含在上述最终通过结果中；原始日志保留，失败运行不计为通过。

环境 unit/protocol 回归 **276 passed**（`environment.xml`，18.26 秒）。固定开发门禁结果另写入此目录的 `development/summary.json`；以实际生成的结果为准，不用收集数量代替执行通过。固定开发 profile 覆盖普通编排、环境 unit/protocol 和既有本地测试，不证明真实产品或 PostgreSQL 验收。

## 未完成边界

真实 SSH 验收入口已提供，缺少 `SANDBOX_GRAPH_BOOTSTRAP_CONTRACT` 时明确 skip。任务 11.4 保留未完成：需要授权的 Linux SSH 目标、完整兼容 wheel 集合，并完成现场断连/主机崩溃恢复验收。没有使用本地子进程硬杀代替真实 SSH 断连验收。

其他代码缺口仍在原任务账本中，包括动态产品 sandbox/cgroup/generation 发现与刷新、部分角色有效参数探测、完整 PostgreSQL 故障组合、产品镜像加载与部分安装恢复、受限私有日志导出及候选 worker bundle 启动入口。此次交付没有把它们归为纯环境阻塞，也没有宣称项目全部完成。
