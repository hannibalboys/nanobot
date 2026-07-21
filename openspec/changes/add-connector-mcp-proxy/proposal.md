# Proposal: add-connector-mcp-proxy

## Why

v2（`add-connector-local-tools`）让设备主人**逐个登记**本地可执行程序。但很多本地能力已经以 MCP server 形式存在（浏览器控制、本地数据库、IDE、文件索引、笔记应用等），逐个登记为声明式工具既重复又丢失了 MCP 的结构化 schema。本变更让连接器把**用户本机运行的 MCP server** 通过连接器通道桥接到 nanobot server，Agent 经发现-调用工具使用它们——沿用 nanobot MCP 配置形态（`MCPServerConfig` 的 stdio/sse/streamableHttp）与命名/schema 约定，一次桥接即获得一整套结构化本地能力，而 server 本身无需能直连用户电脑。

## What Changes

- **协议扩展（v2 之上加法）**：新增 `mcp.list`（列出本机已登记 MCP server 的工具与健康）、`mcp.call`（转发一次 MCP 工具调用）方法；沿用 v2 的 request-id 机制。`capabilities` 增加 `mcp` 项。v2.5 仅代理 tools，不代理进度通知/resources/prompts。
- **客户端 MCP 桥接**：连接器新增 `mcp.json`，设备主人登记本机 MCP server（沿用 nanobot `MCPServerConfig` 的 stdio/sse/streamableHttp 三型）。连接器作为 MCP 客户端连本地 server，把其工具列表与调用经连接器通道转发。MCP server 进程生命周期由连接器在本机管理，绝不暴露端口到公网。
- **服务端经 Agent 工具暴露**：新增 `connector_list_mcp_tools` / `connector_call_mcp_tool`，让 Agent 发现并调用某设备桥接的 MCP 工具（与其余 `connector_*` 一致的发现-调用模式，天然适配设备动态上下线，避免灌爆 provider 词表）。
- **复用 v2 授权与审批**：桥接的 MCP 工具经协调器沿用 v2 的 per-device/per-tool 授权（键 `mcp:<server>:<tool>`）与 `auto`/`webui`/`local` 审批策略；设备主人主权不变。
- **WebUI**：设备控制中心"本地工具"标签下新增"本地 MCP server"分区，展示已桥接 server、其工具与连接健康。
- **开关**：受 `connector.enabled` + `connector.allowExec` + 新增 `connector.allowMcpProxy` 三重控制，默认全 false。
- **明确不做**：在服务端为用户自动发现/安装 MCP server（仍由设备主人本机登记）、把服务端侧 MCP server 反向暴露给连接器、桌面 GUI 控制（v3）。

## Capabilities

### New Capabilities

- `connector-mcp-proxy`: 本机 MCP server 经连接器通道的桥接——协议 `mcp.*` 方法、客户端 MCP 桥接与生命周期管理、服务端经 `connector_list_mcp_tools`/`connector_call_mcp_tool` 暴露、连接健康与重连。

### Modified Capabilities

- `connector-gateway`: `ConnectorHub` 新增 `list_mcp_tools`/`call_mcp_tool`/`mcp_status` 路由，桥接工具经协调器统一走 v2 授权/审批/审计；`/api/connector/mcp-tools` 管理路由。
- `connector-client`: 新增 `mcp.json` 与 `mcp` 系列 CLI 子命令、本机 MCP client 桥接与 server 进程管理。
- `connector-webui`: 控制中心新增"本地 MCP server"分区（已桥接列表、工具、连接健康）。

## Dependencies

- **依赖 `add-connector-local-tools`（v2）先行**：本变更复用 v2 建立的协议 v2、`capabilities` 协商、request-id + 流式机制、per-device/per-tool 授权与 `auto`/`webui`/`local` 审批、操作者与设备主人分离、设备定向与审计。须在 v2 归档后实施。
- **间接依赖 v1**：经 v2 传递（v1 的配对/令牌/归属/归档基线）。

## Impact

- **新增代码**：`nanobot/connector/mcp_proxy.py`（服务端桥接注册）、连接器子项目 `nanobot_connector/mcp_bridge.py`（本机 MCP client 桥接）、WebUI MCP 分区组件。
- **修改代码**：`nanobot/connector/protocol.py`（`mcp.*` 帧/方法）、`hub.py`（mcp 路由）、`nanobot/agent/tools/mcp.py`（复用其封装以适配"经连接器通道"的 transport）、`nanobot/config/schema.py`（`allowMcpProxy` 开关）。
- **复用**：最大化复用 `MCPServerConfig` 与 `mcp.py` 的工具封装/名称清洗/重连逻辑，仅替换底层 transport 为"经连接器通道"。
- **安全面**：桥接工具等同远程执行能力，沿用 v2 全部授权/审批/审计防线；额外注意 MCP server 自身权限（其能访问的本机资源）需在登记文档中提示设备主人。
- **依赖**：客户端新增 MCP 协议客户端依赖（与主项目 MCP 栈一致）；服务端零新增。
