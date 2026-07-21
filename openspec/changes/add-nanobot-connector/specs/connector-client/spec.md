# connector-client 规格增量

## ADDED Requirements

### Requirement: CLI 命令集
连接器 SHALL 提供 CLI（typer）：`pair --server <url> --code <配对码>`（配对并保存令牌）、`allow <目录>`（追加共享目录）、`remove <目录>`、`start`（前台运行）、`service install|uninstall`（开机自启）、`status`（连接状态与共享目录）。配置 SHALL 持久化在 `~/.nanobot-connector/config.json`（pydantic，camelCase）。

#### Scenario: 配对保存令牌
- **WHEN** 用户执行 `pair` 且配对码有效
- **THEN** 设备令牌与服务器地址写入本地配置，命令报告成功

#### Scenario: 状态查询
- **WHEN** 用户执行 `status`
- **THEN** 输出连接状态、服务器地址、共享目录列表与客户端版本

### Requirement: 服务器证书信任（自签场景）
连接器 MUST 默认执行完整 TLS 证书校验，MUST NOT 静默跳过。针对内网自签证书部署，`pair` SHALL 支持 `--fingerprint <sha256>` 进行证书指纹固定（pinning），此后每次连接校验指纹一致；`--insecure` 仅作为显式选项且每次启动 SHALL 输出警告。

#### Scenario: 指纹固定连接自签服务器
- **WHEN** 用户以 `pair --server wss://192.168.90.100:8765 --fingerprint <sha256>` 配对
- **THEN** 配对与后续连接均校验服务器证书指纹，指纹不符立即断开并报错

#### Scenario: 证书校验失败给出可操作提示
- **WHEN** 未提供指纹且服务器证书无法通过系统信任链校验
- **THEN** 连接失败，错误信息提示可用 `--fingerprint` 固定证书并给出获取指纹的方法

### Requirement: 出站连接与自动重连
连接器 SHALL 仅发起出站 WSS 连接，MUST NOT 监听任何本机端口。连接断开后 SHALL 以指数退避（1 秒起、上限 60 秒、含随机抖动）自动重连；收到 `revoked` 帧后 MUST 停止重连并提示用户重新配对。

#### Scenario: 断网自动恢复
- **WHEN** 网络中断后恢复
- **THEN** 连接器在退避窗口内自动重连并重新注册，全程无需人工干预

#### Scenario: 吊销后停止重连
- **WHEN** 连接器收到 `revoked` 帧
- **THEN** 停止重连循环，`status` 显示"设备已被吊销，需重新配对"

### Requirement: 目录白名单强制执行
连接器 MUST 将服务端请求视为不可信输入：所有 `fs.*` 方法的路径参数 SHALL 经 `resolve()`（消解 `..` 与符号链接）后校验落在用户显式添加的共享根目录内，否则返回 `path_denied`。`allow` 命令 MUST 拒绝添加文件系统根目录（如 `C:\`、`/`）。v1 SHALL 只实现只读方法（list/search/read/fetch/stat），不实现任何写入或执行方法。

#### Scenario: 路径逃逸被拦截
- **WHEN** 服务端请求 `D:/PPT资料/../../Windows/system.ini`
- **THEN** 连接器解析真实路径后返回 `path_denied`

#### Scenario: 符号链接逃逸被拦截
- **WHEN** 白名单目录内的符号链接指向白名单外
- **THEN** 解析后的目标路径校验失败，返回 `path_denied`

#### Scenario: 拒绝共享根目录
- **WHEN** 用户执行 `allow C:\`
- **THEN** 命令拒绝并说明原因

### Requirement: 分块文件上传
对 `fs.fetch` 请求，连接器 SHALL 按 `chunkBytes` 分块读取文件并以带序号的 `file_chunk` 帧发送，尾帧携带 sha256 与总字节数；超过服务端声明的 `maxFileBytes` 的文件 SHALL 直接返回 `too_large` 而不开始传输；收到对应 `id` 的 `cancel` 帧后 SHALL 立即停止发送。

#### Scenario: 完整分块传输
- **WHEN** 服务端请求拉取一个 10MB 文件
- **THEN** 连接器按块发送并以含 sha256 的尾帧结束

#### Scenario: 超大文件拒绝
- **WHEN** 目标文件大小超过 `maxFileBytes`
- **THEN** 返回 `too_large`，不发送任何 `file_chunk`

#### Scenario: 收到取消即停止
- **WHEN** 传输中途收到该 `id` 的 `cancel` 帧
- **THEN** 连接器停止读取与发送，不再产生该 `id` 的任何帧

### Requirement: 搜索资源上限
`fs.search` SHALL 受资源上限保护：结果数上限（默认 50）、扫描时间上限（默认 10 秒）、目录深度上限（默认 12 层）；达到任一上限时返回已收集的部分结果并标记 `truncated: true`，MUST NOT 使连接器长时间不可响应。

#### Scenario: 大目录搜索及时返回
- **WHEN** 白名单目录包含数十万文件且搜索在 10 秒内未扫完
- **THEN** 返回已找到的结果并标记截断，期间心跳与其他 RPC 不受阻塞

### Requirement: 三平台分发与本地审计
连接器 SHALL 以 PyInstaller 单文件形式分发 Windows/macOS/Linux 三平台产物，并支持开机自启（Windows 服务 / launchd / systemd）。每次 `fs.*` 调用 SHALL 记录本地审计日志（`~/.nanobot-connector/logs/`，滚动保留 30 天）。

#### Scenario: 文件访问留痕
- **WHEN** 服务端读取了本机任一文件
- **THEN** 本地审计日志新增包含时间、方法、路径、结果的记录
