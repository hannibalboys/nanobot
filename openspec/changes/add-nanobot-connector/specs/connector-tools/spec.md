# connector-tools 规格增量

## ADDED Requirements

### Requirement: 工具注册受配置开关控制
当 `connector.enabled` 为 true 时，系统 SHALL 通过既有工具自动发现机制（pkgutil 扫描 `nanobot/agent/tools/`）注册 `connector_list_nodes`、`connector_list_files`、`connector_search_files`、`connector_read_file`、`connector_fetch_file` 五个工具；为 false 时这些工具 MUST 完全不出现在 `ToolRegistry` 中。

#### Scenario: 开关开启注册全部工具
- **WHEN** `connector.enabled` 为 true 且 AgentLoop 构建工具集
- **THEN** 五个 `connector_*` 工具全部可用

#### Scenario: 开关关闭不注册
- **WHEN** `connector.enabled` 为 false
- **THEN** `ToolRegistry` 不包含任何 `connector_*` 工具

### Requirement: 节点发现工具
`connector_list_nodes` SHALL 返回当前在线设备列表，每项至少包含 `node_id`、设备名称、平台、共享根目录列表。

#### Scenario: 列出在线节点
- **WHEN** 存在一台已注册在线的设备且 Agent 调用 `connector_list_nodes`
- **THEN** 结果包含该设备的 `node_id`、名称、平台与共享根目录

### Requirement: 设备可见性按归属隔离
当部署启用多用户鉴权时，`connector_*` 工具在某一会话中 SHALL 只能发现并访问归属于该会话用户的设备；访问他人设备 MUST 返回等同于 `not_found` 的错误（不泄露设备存在性）。单用户部署（未启用多用户鉴权）不受此限制。

#### Scenario: 跨用户访问被拒绝
- **WHEN** 多用户实例中，用户 A 的会话尝试 `connector_list_files` 用户 B 的设备
- **THEN** 工具返回设备不存在的错误，且不泄露该设备任何信息

### Requirement: 文件列取与搜索工具
`connector_list_files(node_id, path)` SHALL 返回白名单目录内指定路径的条目（名称、大小、修改时间、类型）；`connector_search_files(node_id, query)` SHALL 在白名单内按文件名模糊匹配并返回 TopN 结果。

#### Scenario: 列出目录内容
- **WHEN** Agent 对在线节点的白名单内路径调用 `connector_list_files`
- **THEN** 返回该目录的文件与子目录清单

#### Scenario: 白名单外路径被拒绝
- **WHEN** Agent 请求白名单外的路径
- **THEN** 工具返回包含 `path_denied` 语义的 `ToolResult.error`，并提示需在连接器上授权该目录

### Requirement: 文件读取与拉取工具
`connector_read_file(node_id, path)` SHALL 将不超过 `connector.maxInlineReadBytes`（默认 256KB）的文本文件内容内联返回；`connector_fetch_file(node_id, path)` SHALL 将文件传输落盘到 `<workspace>/connector/<node_id>/` 并返回服务器端绝对路径，供后续既有文件工具使用。

#### Scenario: 拉取文件供后续处理
- **WHEN** Agent 调用 `connector_fetch_file` 拉取一个白名单内的文档
- **THEN** 工具返回服务器端路径，且该路径可被既有 `read_file` 工具读取

#### Scenario: 内联读取超限
- **WHEN** `connector_read_file` 目标文件超过 `maxInlineReadBytes`
- **THEN** 返回错误并提示改用 `connector_fetch_file`

#### Scenario: 非文本文件内联读取
- **WHEN** `connector_read_file` 目标文件无法以 UTF-8 解码（二进制/其他编码）
- **THEN** 返回错误并提示改用 `connector_fetch_file` 拉取后由服务器端工具处理

### Requirement: 错误到 ToolResult 的映射
工具层 SHALL 将 `node_offline`、`rpc_timeout`、`path_denied`、`too_large`、`not_found` 等错误映射为面向 LLM 的可操作 `ToolResult.error` 文案（说明原因与建议动作），MUST NOT 抛出未捕获异常导致回合中断。

#### Scenario: 设备离线的可读错误
- **WHEN** Agent 调用工具时目标设备离线
- **THEN** 返回的错误文案说明设备离线并建议提示用户启动连接器
