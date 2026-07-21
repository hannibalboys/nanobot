import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

type Theme = "light" | "dark";
export type ColorTheme = "default" | "forest";
const STORAGE_KEY = "nanobot-webui.theme";
const COLOR_THEME_STORAGE_KEY = "nanobot-webui.color-theme";
const ThemeContext = createContext<Theme>("light");

function readStored(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function readStoredColorTheme(): ColorTheme {
  try {
    const v = localStorage.getItem(COLOR_THEME_STORAGE_KEY);
    return v === "forest" ? v : "default";
  } catch {
    return "default";
  }
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "dark") root.classList.add("dark");
  else root.classList.remove("dark");
}

function applyColorTheme(colorTheme: ColorTheme): void {
  const root = document.documentElement;
  if (colorTheme === "default") delete root.dataset.theme;
  else root.dataset.theme = colorTheme;
}

export function useTheme(): {
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
  colorTheme: ColorTheme;
  setColorTheme: (t: ColorTheme) => void;
} {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = readStored();
    if (stored) return stored;
    if (typeof window !== "undefined" && window.matchMedia) {
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    return "light";
  });

  const [colorTheme, setColorThemeState] = useState<ColorTheme>(readStoredColorTheme);

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // ignore
    }
  }, [theme]);

  useEffect(() => {
    applyColorTheme(colorTheme);
    try {
      localStorage.setItem(COLOR_THEME_STORAGE_KEY, colorTheme);
    } catch {
      // ignore
    }
  }, [colorTheme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(
    () => setThemeState((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  const setColorTheme = useCallback((t: ColorTheme) => setColorThemeState(t), []);
  return { theme, toggle, setTheme, colorTheme, setColorTheme };
}

export function ThemeProvider({ theme, children }: { theme: Theme; children: ReactNode }) {
  return createElement(ThemeContext.Provider, { value: theme }, children);
}

export function useThemeValue(): Theme {
  return useContext(ThemeContext);
}
