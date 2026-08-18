import { SettingsPage } from "@/components/settings/SettingsPage";
import type { SettingsSectionKey } from "@/components/settings/contracts";
import { useSettingsController } from "@/components/settings/useSettingsController";
import type { ColorTheme } from "@/hooks/useTheme";
import type { SettingsPayload, SkillSummary } from "@/lib/types";

export type { SettingsSectionKey } from "@/components/settings/contracts";

interface SettingsViewProps {
  theme: "light" | "dark";
  colorTheme?: ColorTheme;
  initialSection?: SettingsSectionKey;
  initialSettings?: SettingsPayload | null;
  showSidebar?: boolean;
  onToggleTheme: () => void;
  onChangeColorTheme?: (colorTheme: ColorTheme) => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
  onSettingsChange?: (payload: SettingsPayload) => void;
  skills?: SkillSummary[];
  onSectionChange?: (section: SettingsSectionKey) => void;
  onLogout?: () => void;
  onRestart?: () => void;
  onNativeEngineRestart?: () => Promise<string>;
  isRestarting?: boolean;
  hostChromeInset?: boolean;
}

export function SettingsView({
  theme,
  colorTheme = "default",
  initialSection = "overview",
  initialSettings = null,
  showSidebar = true,
  onToggleTheme,
  onChangeColorTheme,
  onBackToChat,
  onModelNameChange,
  onSettingsChange,
  skills = [],
  onSectionChange,
  onLogout,
  onRestart,
  onNativeEngineRestart,
  isRestarting = false,
  hostChromeInset = false,
}: SettingsViewProps) {
  const controller = useSettingsController({
    initialSection,
    initialSettings,
    onModelNameChange,
    onSettingsChange,
    onSectionChange,
    onRestart,
    onNativeEngineRestart,
  });

  return (
    <SettingsPage
      controller={controller}
      theme={theme}
      colorTheme={colorTheme}
      showSidebar={showSidebar}
      onToggleTheme={onToggleTheme}
      onChangeColorTheme={onChangeColorTheme}
      onBackToChat={onBackToChat}
      skills={skills}
      onLogout={onLogout}
      isRestarting={isRestarting}
      hostChromeInset={hostChromeInset}
    />
  );
}
