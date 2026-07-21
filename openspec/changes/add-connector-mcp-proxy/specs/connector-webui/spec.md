# connector-webui 规格增量

## ADDED Requirements

### Requirement: 本地 MCP server 分区
设备控制中心的"本地工具"标签 SHALL 新增"本地 MCP server"分区，展示某设备已桥接的 MCP server、其工具列表与连接健康状态。仅当 `connector.allowMcpProxy` 为 true 时可见。

#### Scenario: 展示桥接 server
- **WHEN** 设备主人打开某已桥接设备的本地 MCP server 分区
- **THEN** 展示该设备的 MCP server、工具与连接健康

#### Scenario: 开关关闭隐藏分区
- **WHEN** `connector.allowMcpProxy` 为 false
- **THEN** 不展示本地 MCP server 分区

### Requirement: 桥接工具授权与裁剪
本地 MCP server 分区 SHALL 允许设备归属主人对桥接工具进行 per-tool 授权与启用/停用裁剪（复用 v2 授权界面）。非归属主人 MUST NOT 见到这些操作。

#### Scenario: 裁剪桥接工具
- **WHEN** 设备主人停用某桥接工具
- **THEN** 该工具从 `ToolRegistry` 注销，不再可被调用
