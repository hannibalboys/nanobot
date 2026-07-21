# connector-tools 规格增量

## ADDED Requirements

### Requirement: 桌面会话工具
当 `connector.enabled` 与 `connector.allowDesktopControl` 均为 true 时，系统 SHALL 注册 Agent 工具 `connector_desktop_session(node_id, goal)`（开启会话、返回首屏截图）、`connector_desktop_act(session_id, action)`（注入一个动作、返回下一屏截图）、`connector_desktop_end(session_id)`（结束会话），共同驱动"截图→判断→动作→再截图"的 computer-use 闭环；沿用 v1 的归属隔离。任一开关为 false 时这些工具 MUST NOT 出现在 `ToolRegistry`。

> 设计说明：由 Agent 自身的多模态循环驱动（截图作为工具结果回给模型，模型据此产出下一动作），而非在服务端另起一个多模态 LLM 循环——复用 Agent 已有的多模态能力，避免重复的 LLM 集成。

#### Scenario: 双开关开启才注册
- **WHEN** `connector.enabled` 与 `connector.allowDesktopControl` 均为 true
- **THEN** `connector_desktop_session` / `connector_desktop_act` / `connector_desktop_end` 可用

#### Scenario: 归属隔离
- **WHEN** 用户尝试对非归属自己的设备开启桌面会话
- **THEN** 返回等同设备不存在的错误

### Requirement: 桌面工具错误映射
`connector_desktop_session` SHALL 将桌面相关错误（设备不支持桌面/未获本机授权/会话超时/被接管/被终止/系统权限缺失/敏感动作被拒）映射为可操作的 `ToolResult.error`，MUST NOT 抛未捕获异常。

#### Scenario: 未获本机授权
- **WHEN** 设备主人未在本机授权开启会话
- **THEN** 返回说明需设备主人在本机授权的可读错误
