# connector-mcp-proxy 规格增量

## ADDED Requirements

### Requirement: 协议 MCP 代理方法与能力
系统 SHALL 新增 RPC 方法 `mcp.list`（枚举桥接工具与 server 健康）、`mcp.call`（转发一次工具调用），并在 `capabilities` 中支持 `mcp` 项。仅声明 `mcp` 能力的连接器 SHALL 参与 MCP 桥接；服务端 MUST NOT 直接连接用户本机的 MCP server。v2.5 仅代理 tools；MCP 的进度通知（notifications）、resources、prompts 不在范围内。

#### Scenario: 桥接能力协商
- **WHEN** 连接器 register 声明 `capabilities` 含 `mcp`
- **THEN** 服务端允许对其发起 `mcp.list` / `mcp.call`

#### Scenario: 服务端不直连本机 server
- **WHEN** 桥接一个本机 stdio MCP server
- **THEN** 服务端仅经连接器通道收发工具枚举/调用，不与该 server 建立任何直接连接

### Requirement: 客户端 MCP 桥接与生命周期
连接器 SHALL 从本机 `mcp.json` 读取设备主人登记的 MCP server（stdio/sse/streamableHttp），作为 MCP client 连接它们，并把工具枚举与调用经连接器通道转发。stdio server 进程 SHALL 由连接器 spawn/kill；设备下线时连接器 MUST 清理其 spawn 的 server 进程。

#### Scenario: 枚举本机 MCP 工具
- **WHEN** 服务端对已桥接节点发起 `mcp.list`
- **THEN** 连接器返回本机各 MCP server 的工具列表与 schema

#### Scenario: 设备下线清理 server
- **WHEN** 已桥接设备断线
- **THEN** 连接器终止其 spawn 的 stdio server 进程，服务端注销该设备的桥接工具

### Requirement: 服务端经 Agent 工具暴露桥接工具
当 `connector.enabled`、`connector.allowExec` 与 `connector.allowMcpProxy` 均为 true 时，系统 SHALL 注册 Agent 工具 `connector_list_mcp_tools`（枚举某设备桥接的 MCP 工具）与 `connector_call_mcp_tool`（按 server+tool 调用），使桥接工具可被 Agent 发现与调用。任一开关为 false 时这两个工具 MUST NOT 出现在 `ToolRegistry` 中。此发现-调用方式与连接器其余 `connector_*` 工具一致，天然适配设备动态上下线，且避免把大量桥接工具灌入 provider 词表。

> 设计说明：不把每个桥接工具注册为独立 provider 原生工具（`mcp_<device>_<server>_<tool>`），因为连接器设备会动态上下线、且工具数可能很大；改用与 `connector_list_tools`/`connector_call_tool` 一致的发现-调用模式，工具按设备在线性动态可见。

#### Scenario: 三重开关开启才注册
- **WHEN** 三个开关均为 true 且 AgentLoop 构建工具集
- **THEN** `connector_list_mcp_tools` 与 `connector_call_mcp_tool` 均可用，`connector_list_mcp_tools` 返回在线设备桥接的工具

#### Scenario: 开关关闭不注册
- **WHEN** `connector.allowMcpProxy` 为 false
- **THEN** `ToolRegistry` 不包含 `connector_list_mcp_tools` / `connector_call_mcp_tool`

### Requirement: 桥接工具沿用授权与审批
桥接的 MCP 工具 SHALL 沿用 v2 的 per-device/per-tool 授权与 `auto`/`webui`/`local` 审批策略及设备主人主权；未授权调用 SHALL 返回等同 `not_found`，审批被拒 SHALL 阻止调用并记入审计。

#### Scenario: 桥接工具需授权
- **WHEN** 未获授予的用户调用某桥接 MCP 工具
- **THEN** 返回等同工具不存在的错误，不泄露存在性

#### Scenario: 桥接工具审批
- **WHEN** 一个 `approval=local` 的桥接工具被调用
- **THEN** 连接器本机确认后调用才转发给 MCP server

### Requirement: MCP server 凭据只存本机
`mcp.json` 中 MCP server 的启动环境变量、token、密钥等凭据 SHALL 只存于设备本机；连接器 SHALL 在本机启动/连接 server 时使用它们。这些凭据 MUST NOT 经协议传输、MUST NOT 出现在服务端状态或审计中；服务端与其他操作者 MUST NOT 能读取它们。

#### Scenario: server 凭据不外泄
- **WHEN** 一个需要 API token 的本机 MCP server 被桥接
- **THEN** token 由连接器在本机注入 server，协议与服务端仅见工具调用，不接触 token

### Requirement: 桥接工具指标
系统 SHALL 暴露桥接工具的调用指标（次数、时长、失败率、按设备/server 维度）供监控。

#### Scenario: 桥接调用可观测
- **WHEN** 桥接工具被调用若干次
- **THEN** 对应次数、时长与失败率指标被记录并可读取

### Requirement: 连接健康与重连
连接器 SHALL 监控每个本机 MCP server 的连接健康并在其重启/断开后重连；服务端 SHALL 在桥接工具暂不可用时对 Agent 返回可读的"工具暂不可用"错误，MUST NOT 静默失败。

#### Scenario: server 重启后恢复
- **WHEN** 一个本机 MCP server 重启
- **THEN** 连接器重连并重新枚举，工具恢复可用

#### Scenario: 不可用时可读反馈
- **WHEN** 桥接工具因 server 未就绪而不可调用
- **THEN** Agent 收到说明工具暂不可用、建议稍后重试的错误
