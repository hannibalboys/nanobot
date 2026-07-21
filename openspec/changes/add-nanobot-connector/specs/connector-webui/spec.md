# connector-webui 规格增量

## ADDED Requirements

### Requirement: 设备列表页
WebUI SHALL 提供设备管理页，展示每台已配对设备的名称、平台、在线状态、最后在线时间与共享目录摘要；数据来自 `GET /api/connector/nodes`。当 `connector.enabled` 为 false 时，该入口 SHALL 隐藏。

#### Scenario: 展示在线状态
- **WHEN** 一台设备在线、一台离线
- **THEN** 列表分别以在线/离线状态标识两台设备

### Requirement: 添加设备向导
WebUI SHALL 提供三步添加向导：下载连接器 → 生成配对码（显示剩余有效期倒计时）→ 等待设备上线（自动轮询刷新）。

#### Scenario: 配对完成自动确认
- **WHEN** 用户生成配对码后在本机完成 `pair`
- **THEN** 向导页在轮询周期内显示新设备已上线

#### Scenario: 配对码过期提示
- **WHEN** 配对码倒计时归零
- **THEN** 页面提示已过期并提供重新生成入口

### Requirement: 设备吊销操作
WebUI SHALL 在设备详情中提供吊销操作，MUST 二次确认后调用 `DELETE /api/connector/nodes/{id}`，成功后设备状态即时更新为已吊销。

#### Scenario: 吊销需二次确认
- **WHEN** 用户点击吊销
- **THEN** 出现确认对话框，确认后设备从可用列表移除
