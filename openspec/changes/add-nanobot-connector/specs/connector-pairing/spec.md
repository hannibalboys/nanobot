# connector-pairing 规格增量

## ADDED Requirements

### Requirement: 一次性配对码签发
系统 SHALL 允许经 WebUI 会话鉴权的用户通过 `POST /api/connector/pairing-codes` 生成一次性配对码。配对码 MUST 为 8 位大写字母数字，有效期由 `connector.pairingCodeTtlS`（默认 600 秒）控制，且兑换一次后立即失效。

#### Scenario: 生成配对码
- **WHEN** 已鉴权用户请求生成配对码
- **THEN** 返回 8 位配对码及其过期时间

#### Scenario: 配对码过期
- **WHEN** 使用超过 TTL 的配对码兑换
- **THEN** 兑换请求被拒绝

#### Scenario: 配对码不可复用
- **WHEN** 同一配对码被第二次兑换
- **THEN** 第二次兑换被拒绝

### Requirement: 配对端点防暴力破解
`POST /connector/pair` 为无鉴权端点，MUST 施加速率限制：同一来源 IP 连续兑换失败 5 次后 SHALL 锁定该来源 15 分钟；全局兑换失败频率超过阈值时 SHALL 使当前所有未兑换配对码作废并告警。配对码比较 MUST 使用常量时间比较。

#### Scenario: 来源锁定
- **WHEN** 同一 IP 第 6 次提交错误配对码
- **THEN** 请求被拒绝（429），15 分钟内该 IP 的兑换请求不再校验配对码

#### Scenario: 全局暴力尝试熔断
- **WHEN** 全局兑换失败频率超过阈值
- **THEN** 所有未兑换配对码立即作废，审计日志记录告警事件

### Requirement: 设备令牌兑换与存储
连接器 SHALL 通过 `POST /connector/pair` 以有效配对码兑换长期设备令牌。令牌 MUST 以密码学安全随机源生成（不少于 256 位熵）；服务端 MUST 仅持久化令牌的 sha256 哈希，明文令牌仅在兑换响应中出现一次。设备记录（node_id、名称、平台、机器指纹、**归属用户**（生成该配对码的 WebUI 会话身份）、哈希、时间戳、吊销标记）SHALL 以原子写方式持久化到 `<workspace>/connector/devices.json`。

#### Scenario: 设备记录归属可追溯
- **WHEN** 用户 A 生成的配对码被兑换
- **THEN** 该设备记录的归属为用户 A，管理路由与工具可据此过滤

### Requirement: 同一设备重新配对替换旧记录
兑换请求 SHALL 携带机器指纹（主机名 + 稳定机器标识哈希）。同一指纹再次配对时，服务端 SHALL 吊销旧令牌并复用/更新原设备记录，避免产生僵尸设备。

#### Scenario: 重装后重新配对
- **WHEN** 同一台电脑重装连接器并用新配对码兑换
- **THEN** 设备列表仍只有一条该设备记录，旧令牌立即失效

#### Scenario: 兑换成功
- **WHEN** 连接器提交有效配对码及设备元信息（名称、平台）
- **THEN** 返回明文设备令牌与 `node_id`，服务端仅存哈希

#### Scenario: 持久化重启存活
- **WHEN** 网关重启
- **THEN** 已配对设备的令牌仍可用于握手，设备列表完整

### Requirement: 设备吊销即时生效
系统 SHALL 允许经鉴权的用户通过 `DELETE /api/connector/nodes/{id}` 吊销设备。吊销 MUST 立即断开该设备的在线连接（发送 `revoked` 帧后关闭），且被吊销令牌此后的一切握手 SHALL 返回 401。

#### Scenario: 吊销在线设备
- **WHEN** 用户吊销一台在线设备
- **THEN** 该设备 10 秒内被断开，且用原令牌重连收到 401

#### Scenario: 吊销后重新接入需重新配对
- **WHEN** 被吊销设备希望重新接入
- **THEN** 必须使用新的配对码重新兑换令牌
