# nanobot 完整前后端启动命令

## 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/)（用于 WebUI 开发）

## 1. 克隆并进入项目

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
```

## 2. 安装后端依赖

方式一：使用 `uv` 直接运行（推荐，自动解析依赖）

```bash
uv pip install -e ".[dev]"
```

方式二：创建虚拟环境后安装

```bash
uv venv
uv pip install -e ".[dev]"
```

## 3. 初始化配置

```bash
uv run nanobot onboard
```

或者交互式向导：

```bash
uv run nanobot onboard --wizard
```

配置会写入 `~/.nanobot/config.json`，请根据提示设置 Provider 和 Model Preset。

## 4. 启动后端

```bash
uv run nanobot webui
```

说明：
- 启动网关，默认监听 `http://127.0.0.1:8765`。
- 会自动打开浏览器访问 WebUI。
- 如需后台运行：`uv run nanobot webui --background`
- 仅 CLI 测试：
  - `uv run nanobot status`
  - `uv run nanobot agent -m "Hello!"`

## 5. 安装并启动前端

打开一个新的终端窗口：

```bash
cd webui
bun install
bun run dev
```

说明：
- Vite 开发服务器默认会代理 `/api`、`/webui`、`/auth` 和 WebSocket 到后端网关 `8765`。
- 开发服务器地址一般为 `http://localhost:5173`（以终端输出为准）。
- 生产构建：`bun run build`

## 6. 访问 WebUI

- 通过后端直接访问：`http://127.0.0.1:8765`
- 通过前端开发服务器访问：`http://localhost:5173`

## 7. 生产运行

### 方式一：本地/服务器直接部署

**安装发行版：**

```bash
uv tool install nanobot-ai
```

或：

```bash
pip install nanobot-ai
```

**初始化并配置：**

```bash
nanobot onboard
# 编辑 ~/.nanobot/config.json 添加 API 密钥等配置
```

**构建前端静态资源：**

```bash
cd webui
bun install
bun run build
```

构建产物输出到 `nanobot/web/dist/`，会被后端网关自动挂载为静态站点。

**启动生产服务：**

```bash
nanobot webui
```

后台运行：

```bash
nanobot webui --background
```

仅启动网关（无自动打开浏览器）：

```bash
nanobot gateway
```

管理后台进程：

```bash
nanobot gateway status
nanobot gateway logs
nanobot gateway restart
nanobot gateway stop
```

### 方式二：注册为系统服务（推荐长期运行）

**Linux systemd：**

```bash
nanobot gateway install-service --manager systemd
systemctl --user status nanobot-gateway
systemctl --user restart nanobot-gateway
journalctl --user -u nanobot-gateway -f
```

如需退出登录后仍保持运行：

```bash
loginctl enable-linger $USER
```

**macOS LaunchAgent：**

```bash
nanobot gateway install-service --manager launchd
launchctl list | grep ai.nanobot.gateway
nanobot gateway uninstall-service --manager launchd
```

### 方式三：Docker 部署

```bash
# 首次初始化配置
docker compose run --rm nanobot-cli onboard
vim ~/.nanobot/config.json

# 启动网关
docker compose up -d nanobot-gateway

# 查看日志/停止
docker compose logs -f nanobot-gateway
docker compose down
```

> 注意：Docker 中若要从宿主机或局域网访问，需将 `config.json` 中的 `gateway.host` 和 `channels.websocket.host` 设为 `0.0.0.0`，并为 WebSocket 通道配置 `token` 或 `tokenIssueSecret`。

## 常用命令速查

| 用途 | 命令 |
|---|---|
| 后端启动 | `uv run nanobot webui` |
| 后端后台启动 | `uv run nanobot webui --background` |
| 查看状态 | `uv run nanobot status` |
| CLI 单条消息 | `uv run nanobot agent -m "Hello!"` |
| 交互式 CLI | `uv run nanobot agent` |
| 前端开发 | `cd webui && bun run dev` |
| 前端构建 | `cd webui && bun run build` |
| 前端测试 | `cd webui && bun run test` |
| Python 测试 | `uv run pytest tests/test_openai_api.py -v` |
| Python 代码检查 | `uv run ruff check nanobot/` |
