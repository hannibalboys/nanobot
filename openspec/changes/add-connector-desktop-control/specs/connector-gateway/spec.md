# connector-gateway 规格增量

## ADDED Requirements

### Requirement: 桌面会话路由与帧转发
`ConnectorHub` SHALL 提供桌面会话的开启/结束路由、屏幕帧向发起方的转发与键鼠事件向目标节点的下发，复用 v2 的 request-id、超时与断连清理。断连或会话结束时 MUST 停止转发并释放资源。

#### Scenario: 帧与事件双向转发
- **WHEN** 一个受控桌面会话进行中
- **THEN** Hub 把目标节点的屏幕帧转发给发起方，把发起方的动作事件下发给目标节点

#### Scenario: 断连结束会话
- **WHEN** 会话进行中目标节点断线
- **THEN** Hub 结束会话、停止转发并以可读错误通知发起方

### Requirement: 桌面会话资源上限
网关 SHALL 施加帧率上限、分辨率上限、会话最长时长与空闲超时；超限/超时 SHALL 自动结束会话。

#### Scenario: 会话超时自动结束
- **WHEN** 会话超过配置的最长时长
- **THEN** 网关结束会话并停止帧转发
