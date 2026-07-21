# connector-desktop 规格增量

## ADDED Requirements

### Requirement: 协议桌面方法与能力
系统 SHALL 新增 RPC 方法 `desktop.session.start`、`desktop.session.end`、`desktop.capture`、`desktop.input`，及屏幕图像帧与输入事件帧；`capabilities` 增加 `desktop` 项。仅声明 `desktop` 能力且 `connector.allowDesktopControl` 为 true 的节点 SHALL 参与桌面控制。

#### Scenario: 桌面能力协商
- **WHEN** 连接器 register 声明 `capabilities` 含 `desktop` 且服务端开启 `allowDesktopControl`
- **THEN** 服务端允许对其发起桌面会话相关方法

#### Scenario: 未声明能力被拒
- **WHEN** 对不声明 `desktop` 能力的节点尝试开启桌面会话
- **THEN** 返回该设备不支持桌面控制的可读错误

### Requirement: 受控桌面会话生命周期
桌面截屏与键鼠注入 SHALL 只能在一个已开启的受控会话内进行。会话 SHALL 由设备主人在本机显式授权后开启且限时；会话结束、超时或被接管终止后，捕获与注入 MUST 立即停止。

#### Scenario: 会话外禁止捕获
- **WHEN** 没有活动受控会话时收到 `desktop.capture` 或 `desktop.input`
- **THEN** 连接器拒绝，不截屏也不注入任何事件

#### Scenario: 会话需本机授权开启
- **WHEN** 服务端请求 `desktop.session.start`
- **THEN** 连接器要求设备主人在本机显式授权（限时），未授权则会话不开启

#### Scenario: 超时自动结束
- **WHEN** 会话达到最长时长或空闲超时
- **THEN** 会话自动结束，捕获停止、系统资源释放

### Requirement: 人类在环与接管
桌面控制 MUST NOT 提供 `auto` 审批。会话期间设备主人 SHALL 能随时一键接管（暂停 Agent 控制）或终止会话，且即时生效。

#### Scenario: 随时接管
- **WHEN** 会话进行中设备主人点击接管
- **THEN** Agent 的键鼠注入立即暂停，控制权交回设备主人

#### Scenario: 随时终止
- **WHEN** 会话进行中设备主人点击终止
- **THEN** 会话结束，捕获与注入立即停止

### Requirement: 敏感动作二次确认
系统 SHALL 对疑似敏感动作（在密码类输入框输入、点击含支付/确认/删除/授权等语义的控件、系统弹窗）触发额外确认；未确认 SHALL 阻止该动作。

#### Scenario: 敏感动作拦截
- **WHEN** 模型动作被判定为敏感（如向密码框输入）
- **THEN** 该动作在确认前不被注入，并记入审计

### Requirement: 隐私保护
屏幕帧 SHALL 默认仅在内存流转、不落盘；仅当设备主人显式开启录制才留存并在 UI 与审计中标注。捕获期间连接器本机 SHALL 显示醒目指示。

#### Scenario: 帧默认不落盘
- **WHEN** 会话未开启录制
- **THEN** 屏幕帧不被持久化，会话结束后无残留帧文件

#### Scenario: 捕获指示
- **WHEN** 会话正在捕获屏幕
- **THEN** 连接器本机显示醒目的正在捕获指示

### Requirement: 录制留存与删除策略
当设备主人显式开启录制时，系统 SHALL 按可配置的保留期留存录制并到期自动删除；设备主人 SHALL 能随时手动删除某会话录制。录制存储位置、保留期与删除操作 SHALL 记入审计。未开启录制时 MUST 无任何帧留存。

#### Scenario: 到期自动删除
- **WHEN** 一段录制超过配置的保留期
- **THEN** 系统自动删除该录制并记入审计

#### Scenario: 主人手动删除
- **WHEN** 设备主人删除某会话录制
- **THEN** 录制被删除且操作记入审计

### Requirement: 跨人桌面会话的知情同意
当操作者非设备主人时，开启受控桌面会话 SHALL 要求设备主人本机显式同意且留存同意记录；会话期间设备主人 SHALL 持续可见"谁在控制、正在做什么"并可随时接管/终止。跨人会话 MUST NOT 在无设备主人在场同意的情况下开启。

#### Scenario: 跨人会话需在场同意
- **WHEN** 操作者非设备主人且请求开启桌面会话
- **THEN** 须设备主人在本机同意后方可开启，同意记录被留存

#### Scenario: 会话中持续可见与可控
- **WHEN** 跨人会话进行中
- **THEN** 设备主人可见当前操作者与实时画面，并可随时接管或终止

### Requirement: 逐动作审计
系统 SHALL 对每个注入动作记录审计：时间、会话、node_id、动作类型与参数、触发它的截图上下文引用、是否敏感/是否经确认、结果。审计 MUST NOT 可关闭。

#### Scenario: 动作留痕可回放
- **WHEN** 会话内注入任一键鼠动作
- **THEN** 审计新增一条含动作与其截图上下文的记录，支持回放

### Requirement: 资源与边界校验
系统 SHALL 施加帧率上限、分辨率上限、会话最长时长与空闲超时；动作翻译层 SHALL 校验注入事件（坐标在屏内、事件类型白名单）。越界事件 MUST 被拒绝。

#### Scenario: 越界坐标被拒
- **WHEN** 模型输出的点击坐标超出屏幕范围
- **THEN** 该事件被拒绝注入并记入审计
