# Proposal: add-nanobot-connector

## Why

nanobot 以服务端形态部署时，Agent 的文件工具只能访问服务器文件系统，用户本地电脑上的资料（如做 PPT 的素材）无法被 Agent 使用，核心场景无法闭环。业界成熟解法是 CMDB 采集器式的反向连接轻量 Agent，本变更为 nanobot 引入同模式的 **Connector（本地连接器）**。

## What Changes

- 新增连接器客户端子项目 `connector/`（包名 `nanobot-connector`）：安装在用户电脑上的守护进程，通过出站 WSS 长连接接入网关，暴露**目录白名单内只读文件能力**（列目录/搜索/读取/拉取），PyInstaller 三平台打包。
- 网关新增连接器接入端点 `/connector/ws` 与配对/管理 HTTP 路由，复用现有 `websockets.serve(process_request=...)` 分发链。
- 新增 `ConnectorHub`（节点注册/心跳/RPC 路由/分块文件接收）挂载到 `GatewayServices`。
- 新增 `DeviceStore`：一次性配对码 + 设备令牌（sha256 存储、可吊销、JSON 原子持久化）。
- 新增 Agent 工具 `connector_list_nodes` / `connector_list_files` / `connector_search_files` / `connector_read_file` / `connector_fetch_file`（pkgutil 自动发现，受配置开关控制）。
- `nanobot/config/schema.py` 新增 `ConnectorConfig`（camelCase 别名），默认 `enabled: false`，对存量部署零影响。
- WebUI 新增"设备"管理页：设备列表/在线状态、配对码生成向导、吊销。
- v1 明确不做：本地写入/删除、远程执行（`allowExec` 仅预留恒为 false）、实时同步、移动端。

## Capabilities

### New Capabilities

- `connector-gateway`: 网关侧连接器接入——WS 端点、注册/心跳、RPC 路由、分块文件接收与工作区落盘、在线节点管理。
- `connector-pairing`: 设备配对与令牌生命周期——一次性配对码签发/兑换、设备令牌校验、吊销即时生效、持久化。
- `connector-tools`: Agent 侧 `connector_*` 工具集——节点发现与白名单内文件的列取/搜索/读取/拉取，错误到 `ToolResult` 的映射。
- `connector-client`: 连接器客户端——CLI（pair/allow/start/service/status）、出站重连、白名单路径强制、分块传输、开机自启与打包。
- `connector-webui`: WebUI 设备管理——设备列表/在线状态、配对向导、吊销操作。

### Modified Capabilities

（无 —— `openspec/specs/` 当前为空，现有行为的规格未建档，且本变更通过默认关闭的配置开关接入，不改变既有能力的需求。）

## Impact

- **新增代码**：`nanobot/connector/`（protocol/hub/devices/transfer/http_routes）、`nanobot/agent/tools/connector.py`、`connector/` 客户端子项目、WebUI 设备页。
- **修改代码**：`nanobot/config/schema.py`（新增 ConnectorConfig）、`nanobot/webui/gateway_services.py`（挂载 hub/devices）、网关 HTTP 路由注册。
- **依赖**：服务端零新增；客户端子项目依赖 websockets/pydantic/typer（与主项目一致），打包引入 PyInstaller（仅构建期）。
- **安全面**：新增设备令牌与文件外拉通道，需通过威胁建模与渗透测试门禁（见 docs/nanobot-connector/01-项目计划.md §3 G-S4）。
- **运维**：网关需可被用户电脑出站访问（WSS 端口）；新增设备审计日志与监控指标。
