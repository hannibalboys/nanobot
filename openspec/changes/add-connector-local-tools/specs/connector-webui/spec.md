# connector-webui 规格增量

## ADDED Requirements

### Requirement: 设备控制中心
WebUI SHALL 将 v1 的设备列表页扩展为"设备控制中心"，含共享目录、本地工具、授权与审批、审计历史等标签。仅当 `connector.enabled` 为 true 时展示；本地工具与授权标签仅当 `connector.allowExec` 为 true 时可见。

#### Scenario: 按开关显示能力标签
- **WHEN** `connector.enabled` 为 true 但 `connector.allowExec` 为 false
- **THEN** 展示共享目录与设备信息，但不展示本地工具与授权标签

### Requirement: 工具授权管理界面
控制中心 SHALL 展示某设备已登记的工具列表及其每工具授权状态，允许设备归属主人授予/收回授权。非归属主人 MUST NOT 看到授权管理操作。

#### Scenario: 主人管理授权
- **WHEN** 设备归属主人打开某设备的授权标签
- **THEN** 可见工具列表与授权开关，并可授予/收回

### Requirement: 执行审批与实时输出
当工具以 `approval=webui` 被调用时，WebUI SHALL 展示含工具名、参数摘要与发起上下文的审批卡片；放行后 SHALL 展示实时执行输出与取消按钮。

#### Scenario: 审批并观察执行
- **WHEN** 一个 `approval=webui` 工具被调用且用户在卡片上放行
- **THEN** WebUI 显示实时 stdout/stderr 并提供取消按钮，取消即终止执行

### Requirement: 跨人访问与活跃使用者管理
控制中心 SHALL 让设备归属主人处理其他操作者的访问请求/邀请（接受、拒绝、按工具限时授予），并展示"当前哪些操作者在用本设备、调用了哪些工具"的活跃使用者视图，支持一键收回。设备主人 SHALL 能为设备设置可读别名。

#### Scenario: 处理访问请求
- **WHEN** 有操作者请求访问某设备且设备主人打开控制中心
- **THEN** 展示待处理请求，主人可接受并按工具授予或拒绝

#### Scenario: 查看并收回活跃使用者
- **WHEN** 设备主人查看活跃使用者视图
- **THEN** 展示各操作者及其调用的工具，主人可一键收回其授权

### Requirement: 审计历史查看
控制中心 SHALL 提供审计历史标签，按设备展示执行记录（时间、发起者、工具、参数摘要、审批方式、结果）。

#### Scenario: 查看执行历史
- **WHEN** 用户打开某设备的审计历史标签
- **THEN** 按时间倒序展示该设备的执行记录
