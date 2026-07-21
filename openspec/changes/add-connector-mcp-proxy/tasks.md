# Tasks: add-connector-mcp-proxy

## 0. 前置依赖

- [ ] 0.1 确认 `add-connector-local-tools`（v2）已归档，其协议 v2、授权/审批、操作者-主人分离、审计地基可用

## 1. 协议与配置

- [x] 1.1 `nanobot/connector/protocol.py`：新增 `mcp.list`/`mcp.call` 方法；`capabilities` 支持 `mcp`；新增 `mcp_unsupported`/`mcp_unavailable` 错误码（`mcp.notify` 未做——v2.5 仅 tools，不代理进度/通知）
- [x] 1.2 `nanobot/config/schema.py`：新增 `allow_mcp_proxy` 开关（默认 false，camelCase 别名）
- [x] 1.3 协议单元测试：`mcp.*` 方法集、`mcp` 能力协商、错误码（`tests/connector/test_protocol.py`、`test_config.py`）

## 2. 客户端 MCP 桥接（connector-client / connector-mcp-proxy）

- [x] 2.1 `nanobot_connector/mcp_bridge.py`：`mcp.json` 读写（stdio/sse/streamableHttp）、server 凭据只存本机并注入子进程、本机 MCP client 连接（可注入 session 工厂）、工具枚举与调用转发
- [x] 2.2 `mcp add/list/remove` CLI 子命令；`mcp.json` 仅本机可写（协议无写入路径）
- [x] 2.3 单任务持有 SDK 上下文（AnyIO 取消域）、健康监控 + 自动重连、`stop()` 清理；设备下线经 client `run_forever` finally 停止桥接
- [x] 2.4 客户端测试：桥接转发、enabledTools 过滤、不可用报错、失败后重连、注册表往返、`local` 审批 fail-closed（`test_mcp_bridge.py`、`test_mcp_dispatch.py`）

## 3. 服务端桥接（connector-gateway / connector-mcp-proxy）

- [x] 3.1 `ConnectorHub`：`list_mcp_tools`/`call_mcp_tool`/`mcp_status`（复用 v2 request-id/超时/断连清理，能力校验 `mcp_unsupported`）
- [x] 3.2 `ExecutionCoordinator`：`list_mcp_tools`（跨人按授权过滤）/`call_mcp_tool`；桥接工具授权键 `mcp:<server>:<tool>`，与本地工具键隔离；Agent `connector_list_mcp_tools`/`connector_call_mcp_tool`（受三重开关注册）
- [x] 3.3 生命周期：设备下线其在线记录消失 → 路由/调用返回不可用（工具随设备在线性动态可见）；`enabled`+`allowExec`+`allowMcpProxy` 三重门禁（含 `/api/connector/mcp-tools` 路由）
- [x] 3.4 桥接工具复用 v2 授权/审批（webui 默认拒）/审计/限流/指标；调用计入 exec 指标与 exec-audit
- [x] 3.5 集成测试：假连接器桥接假 MCP server 跑通 list→授权→调用；不支持/不可用/未知工具/跨人隔离/webui 默认拒（`tests/connector/test_mcp_proxy.py`、`test_gateway_exec_http.py`、`test_mcp_proxy_e2e.py`）

> 设计偏差说明：采用"连接器做 MCP client + 服务端结构化转发"（设计决策 1）而非"透明 JSON-RPC 隧道复用 mcp.py transport"（决策 2）。原因：前者把 MCP 往返留在本机、只跨 WAN 传工具清单与调用结果，延迟更低且不必把 `ClientSession` 塞进隧道；复用了命名/schema/enabledTools 约定，服务端桥接工具经协调器统一走 v2 授权/审批/审计。命名用授权键 `mcp:<server>:<tool>`（未用 `mcp_<device>_<server>_<tool>` provider 名——桥接工具不进 provider 词表，而是经 `connector_call_mcp_tool` 调用，避免名称长度问题）。

## 4. WebUI（connector-webui）

- [x] 4.1 设备控制中心"本地工具"标签新增"本地 MCP server"分区（已桥接 server/工具/连接健康），经 `/api/connector/mcp-tools` 404 探测显隐（等价 `allowMcpProxy` 门禁）
- [x] 4.2 桥接工具沿用 v2 授权界面（授权键 `mcp:<server>:<tool>`，可经授权表单跨人授予/收回）
- [x] 4.3 i18n 覆盖 10 语言包（+4 键）；WebUI 组件测试 `connector-device-manager.test.tsx`（`bun run test` / build 通过）

## 5. 收尾

- [x] 5.1 端到端："本机 MCP server → 桥接 → 列表(HTTP) → 跨人请求/授予 → Agent 调用 → 设备下线不可见" 全链路（`test_mcp_proxy_e2e.py`）
- [x] 5.2 文档：用户手册"桥接本地 MCP server"、运维手册"allowMcpProxy 治理与监控"
- [x] 5.3 `ruff check nanobot/ connector/` 零告警；mcp_bridge 覆盖率 86%
- [ ] 5.4 安全评审（桥接工具权限、越权调用、server 凭据是否外泄、server 进程泄漏）通过后 `openspec archive add-connector-mcp-proxy` —— **外部门禁，待安全团队评审 + v2 先归档**
