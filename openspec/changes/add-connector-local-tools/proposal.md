# Proposal: add-connector-local-tools

## Why

连接器 v1 让 Agent 能读取用户本地文件，但"手"只能看不能动。用户的核心诉求是把 OpenClaw 那种"在本机调用程序/工具"的能力搬到服务端——由 nanobot server 做大脑，通过部署在各人电脑上的轻量连接器充当"手"，按需执行本地程序（打开软件、跑脚本、调用命令行工具）。v1 已把 `allowExec` 预留为恒 false 的配置位，本变更把它落地为**受控的、声明式的本地工具执行**：不是给服务端开一个任意命令 shell（那等同远程后门），而是让**设备主人在本机显式登记允许被远程调用的工具**，服务端只能在授权+审批边界内调用它们。

## What Changes

- **协议升级到 v2**：新增 `tools.list` / `tools.call` / `tools.cancel` 三个 RPC 方法与 `exec_output`（stdout/stderr 增量流）、`exec_result`（退出码/耗时/截断标记）帧；`PROTOCOL_VERSION` 提升为 2，注册时协商，v1 连接器与 v2 服务端互相兼容降级（老客户端不暴露工具能力）。
- **客户端声明式工具注册表**：连接器新增 `tools.json`（`~/.nanobot-connector/tools.json`），设备主人用 CLI `tool add/list/remove` 登记可被远程调用的工具（名称、可执行文件路径、参数模板/白名单、工作目录、超时、是否需要本机二次确认）。服务端**不能**下发任意命令，只能按 `name` + 结构化参数调用已登记工具；参数经模板校验，拒绝 shell 注入。
- **服务端执行路由**：`ConnectorHub` 新增 `list_tools` / `call_tool`，把 `tools.call` 路由到目标节点，转发流式输出，施加资源上限（并发数、单次超时、输出字节上限）。
- **双层审批**：每次执行前经过（1）服务端**授权检查**（该会话用户是否拥有该设备、该工具是否被授予该会话/用户）；（2）可选的**执行审批**——WebUI 审批卡片或连接器本机托盘/CLI 确认，二者按工具的 `approval` 策略决定（`auto` / `webui` / `local`）。
- **Agent 工具**：新增 `connector_list_tools`(node_id) 与 `connector_call_tool`(node_id, tool, args)，受 `connector.enabled` 与新增 `connector.allowExec` 双开关控制（默认仍 false）。
- **设备定向与会话绑定**：多设备场景下 Agent 需能明确定位目标设备——设备支持可读别名（如"张三的笔记本"），会话可绑定一台默认设备，未指定 `node_id` 时用户须先选择而非任选，避免"在错误的电脑上执行"。
- **操作者与设备主人分离（核心多用户场景）**：nanobot server 的使用者（操作者）与被控设备的所有者（设备主人）可以是不同人。跨人调用须经设备主人**发起邀请/接受访问请求**的显式同意流程；设备主人对"谁在用我的电脑、用了哪些工具"有实时可见性与随时收回权。
- **工具密钥与运行环境**：`tools.json` 可为工具声明所需环境变量/密钥**引用**（凭据实体只存设备本机，永不经协议下发或回传），保证真实工具（需 token/路径的 CLI）可用而不外泄凭据。
- **执行限流与可观测性**：per-session / per-device 执行速率限制防滥用；网关暴露执行指标（次数/时长/失败率/审批拒绝率/限流命中）供监控告警。
- **设备授权模型**：`DeviceStore` 增加 per-device / per-tool 的授权授予与吊销（哪些工具允许被哪些操作者/会话调用），归属主人始终可见并可一键收回。
- **WebUI 设备控制中心**：把 v1 的"设备"列表页扩展为多标签"设备控制中心"——共享目录 / 本地工具 / 授权与审批 / 审计历史；工具执行时展示实时输出与取消按钮。
- **审计**：执行事件（时间/会话/node_id/工具/参数摘要/退出码/耗时/审批人）双端落审计日志。
- **明确不做**：任意 shell 命令直通、桌面 GUI 控制（键鼠/截屏，属 v3）、本地 MCP server 代理（属 v2.5）、非交互式长驻后台进程托管。

## Capabilities

### New Capabilities

- `connector-local-exec`: 声明式本地工具的执行通道——协议 v2 exec 方法与流式帧、客户端工具注册表与参数模板校验、工具密钥/环境变量本机注入、服务端执行路由与资源上限、流式输出与取消、稳定错误码。
- `connector-authorization`: 设备与工具的授权及执行审批——per-device/per-tool 授权授予与吊销、`auto`/`webui`/`local` 三种审批策略（审批 TTL 到期默认拒绝）、操作者与设备主人分离的跨人访问同意流程、设备主人主权（随时收回、审批优先级最高、实时可见谁在用）。

### Modified Capabilities

- `connector-tools`: 新增 `connector_list_tools` / `connector_call_tool` 两个 Agent 工具，受 `connector.allowExec` 开关控制，沿用 v1 的归属隔离与错误映射，并支持设备别名定向与会话默认设备绑定。
- `connector-gateway`: Hub 新增工具枚举/调用路由、流式输出转发与执行资源上限、per-session/per-device 限流与执行指标；`/api/connector/*` 新增工具、授权、跨人访问请求与审批管理路由。
- `connector-client`: 新增工具注册表（`tools.json`）、`tool` 系列 CLI 子命令、执行器（参数模板校验、子进程生命周期、流式回传、本机审批确认）。
- `connector-webui`: 设备页升级为"设备控制中心"（本地工具/授权与审批/审计标签、实时执行输出与取消、审批卡片）。

## Dependencies

- **依赖 `add-nanobot-connector`（v1）先行归档**：本变更的 `connector-tools`/`connector-gateway`/`connector-client`/`connector-webui` 均为对 v1 能力的增量。v1 当前为 change（未 archive，其规格尚未落 `openspec/specs/`），因此本变更须在 v1 完成安全评审并 `openspec archive` 后再实施，否则"Modified Capabilities"缺少基线。
- **被 `add-connector-mcp-proxy`（v2.5）与 `add-connector-desktop-control`（v3）依赖**：协议 v2、授权/审批模型、设备主人主权、审计地基由本变更建立，二者在其上叠加，须晚于本变更。

## Impact

- **新增代码**：`nanobot/connector/exec.py`（执行路由/资源限制/限流/指标）、`nanobot/agent/tools/connector.py`（新增两个工具 + 设备定向）、连接器子项目 `nanobot_connector/tools.py`（注册表 + 密钥引用）与 `runner.py`（执行器）、WebUI 控制中心组件。
- **修改代码**：`nanobot/connector/protocol.py`（v2 帧与方法集 + 新增执行错误码）、`hub.py`（工具路由/流式转发/限流）、`devices.py`（授权模型 + 设备别名 + 跨人访问请求）、`nanobot/config/schema.py`（`allowExec` 语义启用 + 执行资源上限/审批 TTL/限流字段）、`nanobot/webui/gateway_services.py`、网关 HTTP 路由。
- **协议兼容**：`PROTOCOL_VERSION` 1→2，注册协商；v1 客户端连 v2 服务端时不注册执行能力，v2 客户端连 v1 服务端时执行方法降级不可用。对存量部署仍零影响（`allowExec` 默认 false）。
- **安全面**：这是连接器首次具备"在他人电脑上执行"的能力，是最高风险变更。核心防线：命令不由服务端构造（只调已登记工具）、参数模板白名单、凭据只存本机、操作者与设备主人分离的跨人同意、设备主人主权与审批（TTL 到期默认拒绝）、资源上限与限流、双端审计。需专项威胁建模与渗透测试门禁。
- **运维**：新增执行审计日志与指标（执行次数/时长/失败率/审批拒绝率/限流命中）；文档需新增"登记本地工具"、"工具密钥配置"、"审批与跨人授权设置"章节。
