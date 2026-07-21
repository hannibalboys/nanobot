# Tasks: add-forest-theme

## 1. 调色板与主题应用机制

- [x] 1.1 按 design.md D3 映射表将 CerebraBot hex 转换为 HSL 三元组，在 `webui/src/globals.css` 追加 `[data-theme="forest"]`（浅色）与 `[data-theme="forest"].dark`（深色）两个变量块（含 `color-scheme`）
- [x] 1.2 扩展 `webui/src/hooks/useTheme.ts`：新增 `colorTheme: "default" | "forest"` 状态与 `setColorTheme`，localStorage 键 `nanobot-webui.color-theme`（非法值回退 `default`），应用/移除 `<html>` 的 `data-theme` 属性；`ThemeProvider` / `useThemeValue` 签名保持不变（design.md D4）
- [x] 1.3 扩展 `webui/index.html` 预挂载内联脚本：读取 `nanobot-webui.color-theme` 并在 React 挂载前设置 `data-theme="forest"`；为 boot splash 追加 `html[data-theme="forest"]` 底色覆盖（浅 `#e7ebe5` / 深 `#091610`，design.md D6）

## 2. 设置界面

- [x] 2.1 `webui/src/components/settings/SettingsView.tsx` 的 `AppearanceSettings`：原「主题」行更名为「外观」（保留现有浅色/深色切换按钮），在其后新增「主题」行，复用既有 `SegmentedControl`（`默认 | 森语`），接线 `colorTheme` / `setColorTheme` props
- [x] 2.2 `webui/src/App.tsx`：从 `useTheme` 取出 `colorTheme` / `setColorTheme` 并传入 `SettingsView`
- [x] 2.3 i18n：新增 `settings.rows.appearance`、`settings.help.appearance`、`settings.values.themeDefault`、`settings.values.themeForest`，改写 `settings.help.theme`，10 个语言包（en、zh-CN、zh-TW、ja、ko、es、fr、pt-BR、vi、id）全部补齐
- [x] 2.4 更新 `webui/src/tests/i18n.test.tsx` 的必需键清单，纳入 2.3 的新键

## 3. 验证与走查

- [x] 3.1 `cd webui && bun run test` 通过：为 useTheme 的 colorTheme 持久化/回退行为补充测试（`src/tests/useTheme.test.tsx`）；适配 mock 了 `useTheme` 模块的 `app-layout.test.tsx`（注：`thread-shell.test.tsx` 有一个与本变更无关的既有全量并发抖动失败，基线亦复现）
- [ ] 3.2 视觉走查：Forest 浅色/深色下检查会话视图、侧栏、设置页、Markdown/代码渲染、原生宿主玻璃效果（`.host-window-shell` / `.host-sidebar-glass`），修正明显的硬编码颜色冲突
      （已完成代码级审计：产物 CSS 中 `:root` → `.dark` → `[data-theme=forest]` → `[data-theme=forest].dark` 级联顺序正确；全库硬编码颜色扫描确认主表面全部走语义 token，仅剩弹层遮罩/代码块 chrome/PromptRail 圆点等次要固定色，符合 design.md 已接受的取舍。浏览器截图走查因本机无头浏览器全部被拦（chrome-devtools MCP 加载失败、Edge/Chrome headless 启动即退）未能自动化，待人工在 WebUI 目验后勾选）
- [x] 3.3 回归确认：默认主题浅色/深色与改动前逐像素一致（`:root`/`.dark` 变量块零删改，git diff 仅有追加）；存量用户（无 `nanobot-webui.color-theme` 键）加载为默认主题（单测覆盖）
- [x] 3.4 `cd webui && bun run build` 成功，产物正常打入 `nanobot/web/dist`（CSS 含 forest 选择器，index.html 含预挂载脚本）
- [ ] 3.5 全部任务完成并验收后执行 `openspec archive add-forest-theme`，将 `webui-theming` 规格并入 `openspec/specs/`
