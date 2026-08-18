# Tasks: merge-upstream-main

## 1. 前置准备

- [x] 1.1 确认 `dev` 工作区干净、与 `origin/dev` 同步（本地 `main` 与 `origin/main` 的偏差另行处理，不阻塞本提案）
- [x] 1.2 `git -c http.proxy=http://127.0.0.1:7897 fetch upstream --tags`，确认 upstream/main 头为 `2bdb11ee`（或更新，记录实际值）
- [x] 1.3 记录合并前 `dev` 头提交（回滚锚点），确认基线 `cf1e801a`、落后 387、领先 9

## 2. 执行合并

- [x] 2.1 `git checkout dev && git merge upstream/main --no-ff`，收集冲突文件清单
- [x] 2.2 解冲突：共享 Python 文件（`config/schema.py`、`config/loader.py`、`agent/loop.py`、`agent/tools/context.py`、`channels/manager.py`、`channels/websocket/runtime.py`、`cli/commands.py`）——以上游为骨架重放 fork 增量
- [x] 2.3 解冲突：webui（`App.tsx`、`index.html`、`lib/api.ts`、`lib/types.ts`、`SettingsView.tsx`、`globals.css`）——保留 fork 的 connector GUI/主题增量，接收上游其余演进
- [x] 2.4 解冲突：i18n locale——以上游为准，补回 fork 的 connector 文案 key
- [x] 2.5 解冲突：`pyproject.toml`、`README.md`、`CONTRIBUTING.md`、`THIRD_PARTY_NOTICES.md`、`.gitignore`、`tests/` 重叠文件
- [x] 2.6 全量过一遍 fork 独有资产（`connector/`、`nanobot/connector/`、`openspec/`）确认未被合并触碰

## 3. 验证门禁

- [x] 3.1 `ruff check nanobot/`
- [x] 3.2 `pytest` 全量通过（重点 `tests/connector/`、`tests/config/`、`tests/channels/`、`tests/webui/`）（45 个失败经纯上游基线对比确认为环境固有，清单在 temp/full-failures-merged.txt）
- [x] 3.3 `connector/` 子项目测试通过
- [x] 3.4 `cd webui && bun run build && bun run test`
- [x] 3.5 冒烟：`nanobot gateway` 启动 + connector 注册/心跳/工具调用链路手工验证

## 4. 收尾

- [x] 4.1 验证通过后推送 `origin dev`；失败则按 design.md 回滚方案处置并记录
- [x] 4.2 在 `AGENTS.md` 或 docs 中记录新的上游同步基线提交
- [x] 4.3 归档本提案（`openspec archive merge-upstream-main`）
