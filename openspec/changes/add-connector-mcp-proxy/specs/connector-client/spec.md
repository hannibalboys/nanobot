# connector-client 规格增量

## ADDED Requirements

### Requirement: 本机 MCP server 登记 CLI
连接器 SHALL 提供 `mcp add` / `mcp list` / `mcp remove` CLI 子命令，管理本机 `mcp.json` 中登记的 MCP server（沿用 stdio/sse/streamableHttp 配置形态）。登记 MUST 只能在本机完成，MUST NOT 可由服务端远程写入。

#### Scenario: 本机登记 MCP server
- **WHEN** 设备主人执行 `mcp add` 登记一个本机 stdio server
- **THEN** 该 server 写入 `mcp.json`，桥接开启后其工具可被 `mcp.list` 枚举

#### Scenario: 服务端无法远程登记
- **WHEN** 服务端通过协议尝试新增 MCP server 登记
- **THEN** 连接器拒绝，`mcp.json` 不被改动

### Requirement: 本机 MCP client 桥接
连接器 SHALL 作为 MCP client 连接本机登记的 server，把 `list_tools`/`call_tool`/通知经连接器通道转发；stdio server 进程生命周期由连接器管理，端口 MUST NOT 暴露到公网。

#### Scenario: 桥接转发调用
- **WHEN** 服务端经通道发来某 MCP 工具调用
- **THEN** 连接器把它转发给对应本机 server 并回传结果

#### Scenario: 端口不外暴露
- **WHEN** 桥接一个 stdio MCP server
- **THEN** 该 server 仅经连接器出站通道被访问，不监听任何公网端口
