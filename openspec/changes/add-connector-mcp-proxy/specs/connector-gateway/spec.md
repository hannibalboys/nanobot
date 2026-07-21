# connector-gateway 规格增量

## ADDED Requirements

### Requirement: 桥接 MCP 工具的枚举与调用路由
`ConnectorHub` SHALL 提供 `list_mcp_tools`（枚举）、`call_mcp_tool`（调用转发）与 `mcp_status`（工具 + 每 server 健康，供 WebUI），复用 v2 的 request-id、超时与断连清理，并校验目标节点声明了 `mcp` 能力（否则返回 `mcp_unsupported`）。桥接工具随设备在线性动态可见：设备下线后对其发起的枚举/调用 SHALL 返回可读的不可用错误（等同注销）。

#### Scenario: 上线可枚举下线不可用
- **WHEN** 一台桥接设备上线随后下线
- **THEN** 上线时 `list_mcp_tools` 返回其工具，下线后枚举/调用返回设备离线/不支持的可读错误

#### Scenario: 无 mcp 能力被拒
- **WHEN** 对不声明 `mcp` 能力的节点发起 `mcp.list`
- **THEN** 返回 `mcp_unsupported` 错误

### Requirement: 桥接工具经协调器统一治理
网关 SHALL 让桥接工具的调用经 `ExecutionCoordinator` 施加与本地工具一致的授权（授权键 `mcp:<server>:<tool>`）、`webui` 审批、限流、指标与执行审计。网关 SHALL 提供 `/api/connector/mcp-tools` 路由返回某设备的桥接工具与 server 健康（按归属过滤，仅 `allowMcpProxy` 开启时存在）。

#### Scenario: 调用经协调器治理
- **WHEN** Agent 调用一个桥接 MCP 工具
- **THEN** 调用先经授权/审批/限流，再经连接器通道转发到目标设备并回传结果，且计入指标与审计

#### Scenario: 管理路由按开关与归属
- **WHEN** `allowMcpProxy` 关闭，或请求非归属主人的设备
- **THEN** `/api/connector/mcp-tools` 分别返回 404 / 设备不存在
