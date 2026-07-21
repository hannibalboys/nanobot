# connector-webui 规格增量

## ADDED Requirements

### Requirement: 桌面控制会话管理
设备控制中心 SHALL 展示某设备的活跃桌面控制会话（发起者、目标、时长、是否录制），并提供**接管/终止**按钮；仅当 `connector.allowDesktopControl` 为 true（经 `/api/connector/desktop-sessions` 可达性判定）时展示。

> 实现说明：会话截图在本轮由 Agent 的多模态循环消费（截图作为工具结果回给模型），WebUI 侧的**实时画面流式预览**与**逐动作截图回放**为后续增量（后端 `desktop-audit.log` 已落地，可后续加只读回放路由）。

#### Scenario: 展示会话并接管
- **WHEN** 一个受控桌面会话活动
- **THEN** 展示该会话并提供接管/终止按钮，点击即终止会话

#### Scenario: 开关关闭隐藏
- **WHEN** `connector.allowDesktopControl` 为 false
- **THEN** 不展示桌面控制会话区

### Requirement: 敏感动作确认
桌面会话中的敏感动作 SHALL 经设备控制中心顶部的审批卡片确认（复用 v2 `webui` 审批横幅），未确认则动作不注入。

#### Scenario: 敏感动作确认
- **WHEN** 会话内触发敏感动作（`approval=webui` 等价路径）
- **THEN** 审批横幅展示待确认项，放行后动作才注入，超时/拒绝则不注入
