import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useTheme, type ColorTheme } from "@/hooks/useTheme";

const THEME_KEY = "nanobot-webui.theme";
const COLOR_THEME_KEY = "nanobot-webui.color-theme";

let setColorThemeRef: ((t: ColorTheme) => void) | null = null;

function ColorThemeProbe() {
  const { colorTheme, setColorTheme } = useTheme();
  setColorThemeRef = setColorTheme;
  return <div data-testid="color-theme">{colorTheme}</div>;
}

describe("useTheme color theme", () => {
  afterEach(() => {
    localStorage.removeItem(THEME_KEY);
    localStorage.removeItem(COLOR_THEME_KEY);
    delete document.documentElement.dataset.theme;
    setColorThemeRef = null;
  });

  it("defaults to the default theme when nothing is stored", () => {
    render(<ColorThemeProbe />);
    expect(screen.getByTestId("color-theme")).toHaveTextContent("default");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("restores a stored forest theme and applies data-theme", () => {
    localStorage.setItem(COLOR_THEME_KEY, "forest");
    render(<ColorThemeProbe />);
    expect(screen.getByTestId("color-theme")).toHaveTextContent("forest");
    expect(document.documentElement.dataset.theme).toBe("forest");
  });

  it("falls back to default for an invalid stored value", () => {
    localStorage.setItem(COLOR_THEME_KEY, "neon");
    render(<ColorThemeProbe />);
    expect(screen.getByTestId("color-theme")).toHaveTextContent("default");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("persists changes and toggles the data-theme attribute", () => {
    render(<ColorThemeProbe />);

    act(() => setColorThemeRef?.("forest"));
    expect(document.documentElement.dataset.theme).toBe("forest");
    expect(localStorage.getItem(COLOR_THEME_KEY)).toBe("forest");

    act(() => setColorThemeRef?.("default"));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem(COLOR_THEME_KEY)).toBe("default");
  });
});
