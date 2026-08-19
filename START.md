<!--
 * @Author: xuqw xuqw@shait.com.cn
 * @Date: 2026-07-23 15:03:46
 * @LastEditors: xuqw xuqw@shait.com.cn
 * @LastEditTime: 2026-08-19 10:16:43
 * @FilePath: \nanobot\START.md
 * @Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
-->
# nanobot 启动指南

## 后端

```powershell
# 1. 安装依赖（首次）
uv pip install -e ".[dev]"

# 2. API 服务
uv run nanobot serve

# 3. 启动并打开 WebUI（会自动启动 gateway，已运行则直接附着）
uv run nanobot webui
```

> 想前台看 gateway 实时日志，或当常驻服务管理时，才单独执行
> `uv run nanobot gateway`（改完 Python 代码需重启 gateway 生效）。

## 前端（可选，改 WebUI 界面时）

```powershell
cd webui
bun install
bun run dev      # 开发服务器，代理到 gateway :8765

bun run build    # 生产构建，输出到 ../nanobot/web/dist
```

## 连接器（可选，控制远程电脑时）

```powershell
# 在远程电脑上首次安装后：
nanobot-connector init
nanobot-connector pair --server wss://服务器:8765 --code 配对码
nanobot-connector start
```

## 常用命令

```powershell
uv run nanobot gateway --background   # 后台启动（关终端继续跑）
uv run nanobot gateway status         # 查看状态
uv run nanobot gateway logs           # 后台模式日志
uv run nanobot gateway stop           # 停止
uv run nanobot agent -m "你好"        # CLI 单条消息测试
```

> 生产部署与配置迁移详见 [docs/connector-deployment.md](./docs/connector-deployment.md)。
