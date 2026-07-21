# webui-theming 规格增量

## ADDED Requirements

### Requirement: 主题切换控件
WebUI 的「设置 → 外观 → 界面」分区 SHALL 在浅色/深色外观切换行之后提供「主题」切换行，以胶囊按钮呈现 `默认 | 森语`（Default | Forest）两个选项，当前生效主题 MUST 高亮显示。原有的浅色/深色切换行 SHALL 更名为「外观」（Appearance），其切换行为保持不变。

#### Scenario: 切换到 Forest 主题
- **WHEN** 用户在主题行点击「森语」
- **THEN** 整个 WebUI 立即应用 Forest 绿色调色板，无需刷新页面

#### Scenario: 切回默认主题
- **WHEN** 用户在主题行点击「默认」
- **THEN** WebUI 立即恢复现有中性灰调色板

### Requirement: Forest 主题调色板
Forest 主题 SHALL 提供浅色（E-Ink Terminal 米绿纸感）与深色（Deep Forest 墨绿）两套调色板，取值移植自 CerebraBot 的绿色主题。主题与外观 MUST 正交：Forest 生效时，浅色/深色外观切换 SHALL 分别应用 Forest 的浅色/深色变体。调色板 MUST 覆盖现有全部语义色变量（background/foreground/card/popover/primary/secondary/muted/accent/destructive/border/input/ring/sidebar 系列及滚动条），不得遗留默认灰色残留。

#### Scenario: Forest 下切换外观
- **WHEN** Forest 主题生效且用户将外观从浅色切到深色
- **THEN** 界面从 E-Ink 米绿浅色变为 Deep Forest 墨绿深色，主题保持 Forest

#### Scenario: 默认主题不受影响
- **WHEN** 主题为「默认」
- **THEN** 浅色/深色外观下的配色与本变更之前完全一致

### Requirement: 主题持久化
主题选择 SHALL 持久化到浏览器 localStorage（键 `nanobot-webui.color-theme`），页面加载时 MUST 恢复上次选择，且 MUST 在 React 挂载前（预挂载脚本阶段）应用，避免首帧默认主题闪烁；localStorage 无该键或值非法时 SHALL 回退为「默认」主题。持久化 MUST 不经过后端设置 API。

#### Scenario: 刷新后保持主题
- **WHEN** 用户选择 Forest 主题后刷新页面
- **THEN** 页面加载完成时即为 Forest 主题，无闪烁回退

#### Scenario: 存量用户无感知
- **WHEN** 存量用户（localStorage 无主题键）升级后打开 WebUI
- **THEN** 界面为默认主题，与升级前外观一致

### Requirement: 多语言文案
主题行标题、选项名与帮助文案 SHALL 在全部 10 个语言包（en、zh-CN、zh-TW、ja、ko、es、fr、pt-BR、vi、id）中提供翻译；外观行更名涉及的既有文案键 SHALL 同步调整，MUST 不出现缺键回退到键名的情况。

#### Scenario: 中文界面文案
- **WHEN** 界面语言为 zh-CN
- **THEN** 外观行显示「外观」（浅色/深色），主题行显示「主题」（默认/森语）
