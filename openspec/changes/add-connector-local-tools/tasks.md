# Tasks: add-connector-local-tools

## 0. 前置依赖

- [ ] 0.1 确认 `add-nanobot-connector`（v1）已通过安全评审并 `openspec archive`，其 `connector-*` 规格已落 `openspec/specs/`，作为本变更 Modified Capabilities 的基线

## 1. 协议 v2 与配置

- [x] 1.1 `nanobot/connector/protocol.py`：`PROTOCOL_VERSION` 1→2；新增 `tools.list`/`tools.call`/`tools.cancel` 到 `RPC_METHODS`；新增 `ExecOutputFrame`（stream/seq/data）、`ExecResultFrame`（exitCode/durationMs/timedOut/truncated）帧；`NodeInfo` 增加 `capabilities` 字段
- [x] 1.2 新增稳定执行错误码常量：`exec_unsupported`/`tool_not_found`/`invalid_args`/`exec_denied`/`approval_denied`/`approval_timeout`/`exec_limit`/`exec_timeout`/`exec_cancelled`
- [x] 1.3 `nanobot/config/schema.py`：启用 `allow_exec` 语义并新增字段（`maxConcurrentExecs`/`execTimeoutS`/`maxExecOutputBytes`/`approvalTtlS`/`execRatePerMinute`，camelCase 别名），默认关闭且保守取值
- [x] 1.4 协议单元测试：v2 帧编解码往返、`capabilities` 协商、v1↔v2 兼容降级、错误码常量（`tests/connector/test_protocol.py` 扩充）

## 2. 客户端工具注册表与执行器（connector-client / connector-local-exec）

- [x] 2.1 `nanobot_connector/tools.py`：`tools.json` 读写、工具定义模型（name/exec/参数模板/workdir/timeout/approval/凭据引用）、参数模板校验器（类型/枚举/正则/路径目录约束）
- [x] 2.2 `tool add/list/remove` CLI 子命令；工具凭据本机配置（`tool secret set`，实体只存本机、不入 `tools.json` 明文）；拒绝服务端远程写入工具定义或凭据
- [x] 2.3 `nanobot_connector/runner.py`：`shell=False` 的 argv 执行、按引用注入凭据环境变量、stdout/stderr 增量回传（`exec_output`）、结果帧（`exec_result`）、进程树终止（Windows 进程组 + POSIX setsid/killpg）、超时与输出上限截断
- [x] 2.4 客户端 `tools.list`/`tools.call`/`tools.cancel` 处理器接入读循环；能力集在 register 帧声明 `exec`
- [x] 2.5 `local` 审批：本机确认闸门（可插拔 hook；无 hook 时 fail-closed 拒绝）；守护进程经 arm 时间窗（`arm exec --for`）提供本机同意（`nanobot_connector/arm.py` + `build_daemon_client`）
- [x] 2.6 客户端本机执行审计日志（滚动，字段与服务端一致，参数脱敏）
- [x] 2.7 客户端测试：参数校验矩阵（注入/越权路径/未声明参数）、进程树终止、超时与截断、`tools.json`/凭据不可远程写、凭据只本机注入不外泄、缺失凭据可读失败、审批拒绝阻断（`connector/tests/`）

## 3. 服务端 Hub 与授权（connector-gateway / connector-authorization）

- [x] 3.1 `ConnectorHub`：`list_tools()` / `call_tool()`（复用 request-id + queue 转发 `exec_output`，`exec_result` 终止，断连清理）、`cancel` 终止执行
- [x] 3.2 `nanobot/connector/exec.py`：并发上限 `maxConcurrentExecs`、单次 `execTimeoutS`、输出累计 `maxExecOutputBytes` 与背压/截断；per-operator/per-device 限流 `execRatePerMinute`（命中返回 `exec_limit`）
- [x] 3.3 授权（`nanobot/connector/authz.py`：per-device/per-tool 授予/收回/查询、操作者与设备主人分离、跨人访问请求/限时授予、活跃使用者视图、持久化）+ `DeviceStore` 设备可读别名
- [x] 3.4 审批编排：`auto` 直通、`webui` 弹卡片等待放行（`approvalTtlS` 到期默认拒绝，返回 `approval_denied`）、`local` 委托客户端确认；审批结果记入审计
- [x] 3.5 `/api/connector/*` 路由：列设备工具、授权查询/授予/收回、跨人访问请求处理、活跃使用者查看/收回、设备别名设置、响应 WebUI 审批（会话归属过滤）
- [x] 3.6 服务端执行审计日志（时间/操作者/node_id/工具/参数摘要/审批方式/退出码/耗时/结果，`exec-audit.log`）
- [x] 3.7 执行可观测性指标：次数/时长分布(p50/p95)/失败率/审批拒绝率/限流命中（按设备维度），`/api/connector/exec-metrics`
- [x] 3.8 集成测试：内存双工假连接器跑通 list→授权→审批→exec→cancel；审批 TTL 默认拒绝；限流 `exec_limit`；超时取消；超并发拒绝；输出截断；断连中止；跨人授予后可用/收回后拒绝（`tests/connector/test_exec_integration.py`、`test_gateway_exec_http.py`、`test_authz.py`）

## 4. Agent 工具（connector-tools）

- [x] 4.1 `nanobot/agent/tools/connector.py`：新增 `connector_list_tools` / `connector_call_tool`，受 `enabled` + `allowExec` 双开关注册；`node_id` 必填，`connector_list_nodes` 返回设备别名
- [x] 4.2 错误映射：设备不支持执行/工具不存在/参数校验失败/未授权/审批拒绝或超时/超时/超并发或限流/被取消 → 可操作 `ToolResult.error`
- [x] 4.3 归属隔离：跨用户调用返回等同 `not_found`；`node_id` 必填由 schema 强制（不隐式任选）
- [x] 4.4 工具测试：双开关注册行为、参数透传、错误映射、别名定向、无 coordinator 降级（`tests/agent/tools/test_connector.py` 扩充）

## 5. WebUI 设备控制中心（connector-webui）

- [x] 5.1 设备控制中心：设备详情弹窗（本地工具/授权/审计 标签）+ 设备别名编辑，按 `enabled`/`allowExec` 显隐（`ConnectorDeviceManager.tsx`）
- [x] 5.2 本地工具标签：展示某设备已登记工具、审批策略徽章与参数模式（只读）
- [x] 5.3 授权标签：per-tool 授予/收回、活跃使用者、设备别名设置（仅归属主人）
- [x] 5.4 审批卡片：`approval=webui` 弹待处理审批卡片（工具+脱敏参数摘要+发起者）+ 放行/拒绝
- [x] 5.5 审计标签：按设备时间倒序展示执行记录（经新增 `/api/connector/exec-audit`）
- [x] 5.6 i18n：新增 23 键覆盖 10 个语言包；组件测试 `connector-device-manager.test.tsx`（`bun run test` / build 通过）
- [~] 5.7 实时输出流与执行中取消（agent 调用为一次性返回；WebUI 实时流建议后续增量，当前审批卡片展示摘要）

## 6. 端到端与收尾

- [x] 6.1 端到端验证："列工具 → 请求/授予 → 调用（auto/webui 审批）→ 结果 → 取消 → 审计/指标" 全链路走真实网关路由（`tests/connector/test_exec_e2e.py`，5 例）
- [x] 6.2 可靠性：执行中断连中止、超时取消进程树、审批 TTL 默认拒绝、限流 `exec_limit`、跨人授予/收回（覆盖于 e2e + `test_exec_integration.py`）
- [x] 6.3 文档：用户手册新增"受控执行本地工具/审批/密钥/跨人授权"；运维手册新增"allowExec 开关与配置/授权治理/执行监控指标/exec-audit/Runbook"
- [x] 6.4 `ruff check nanobot/ connector/` 零告警；新增代码单测覆盖率 ≥ 80%（服务端连接器 90%、客户端 tools/runner 91%）
- [ ] 6.5 安全评审与渗透测试（参数注入/越权路径/服务端提权/绕过 local 审批/越权调用他人设备/凭据是否泄露到任何服务端可见面/审批 TTL 绕过/限流绕过/资源耗尽）通过后 `openspec archive add-connector-local-tools` —— **外部门禁，待安全团队评审 + v1 先归档**
