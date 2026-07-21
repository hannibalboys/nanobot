# connector-tools 规格增量

## ADDED Requirements

### Requirement: 本地工具枚举与调用工具
当 `connector.enabled` 与 `connector.allowExec` 同时为 true 时，系统 SHALL 注册 Agent 工具 `connector_list_tools(node_id)` 与 `connector_call_tool(node_id, tool, args)`；两者 SHALL 沿用 v1 的设备归属隔离（跨用户返回等同 `not_found`）。当 `connector.allowExec` 为 false 时，这两个工具 MUST NOT 出现在 `ToolRegistry` 中。

#### Scenario: 双开关开启才注册执行工具
- **WHEN** `connector.enabled` 与 `connector.allowExec` 均为 true 且 AgentLoop 构建工具集
- **THEN** `connector_list_tools` 与 `connector_call_tool` 均可用

#### Scenario: 执行开关关闭不注册
- **WHEN** `connector.allowExec` 为 false
- **THEN** `ToolRegistry` 不包含 `connector_list_tools` / `connector_call_tool`（其余 v1 只读工具不受影响）

#### Scenario: 枚举远程工具
- **WHEN** Agent 对一台支持执行的在线设备调用 `connector_list_tools`
- **THEN** 返回该设备已登记工具的名称、描述与参数模式

### Requirement: 设备定向与会话默认绑定
`connector_call_tool` 的 `node_id` SHALL 为必填。`connector_list_nodes` SHALL 返回设备可读别名供选择；会话 MAY 绑定一台默认设备。当未提供 `node_id` 且会话无默认设备时，工具 SHALL 返回"请先指定设备"的可读错误，MUST NOT 隐式任选任一在线设备。

#### Scenario: 未指定设备不隐式任选
- **WHEN** 存在多台在线设备且 Agent 调用 `connector_call_tool` 未提供 `node_id` 且会话无默认设备
- **THEN** 返回要求先指定设备的可读错误，不在任一设备上执行

#### Scenario: 别名辅助定向
- **WHEN** Agent 调用 `connector_list_nodes`
- **THEN** 每台设备返回其可读别名与 `node_id`，供后续调用精确定向

### Requirement: 调用工具的错误映射
`connector_call_tool` SHALL 将执行相关错误（设备不支持执行、工具不存在、参数校验失败、未授权、审批被拒/超时、超时、超并发、被取消）映射为面向 LLM 的可操作 `ToolResult.error` 文案，MUST NOT 抛出未捕获异常中断回合。

#### Scenario: 审批被拒的可读错误
- **WHEN** Agent 调用的工具执行审批被拒绝
- **THEN** 返回说明"执行被设备主人拒绝"的可读错误，Agent 可据此回复用户

#### Scenario: 设备不支持执行
- **WHEN** 目标设备为 v1 连接器（无 exec 能力）
- **THEN** 返回说明该设备不支持远程执行、建议升级连接器的可读错误
