import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

const STORAGE_KEY = "edge-console:theme";

export type Theme = "light" | "dark";

/** 已保存的主题（默认**浅色**；只有显式选过深色才进深色）。 */
export function readTheme(): Theme {
    return localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
}

/** 把主题写到 `<html class>`（Tailwind 的 `dark` variant 依赖它）并持久化。 */
export function applyTheme(theme: Theme): void {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem(STORAGE_KEY, theme);
}

interface ThemeState {
    theme: Theme;
    setTheme: (theme: Theme) => void;
    toggle: () => void;
}

const Ctx = createContext<ThemeState | null>(null);

/** 主题上下文（浅色默认 + 深色切换）：顶栏按钮与 3D 视口共用同一份状态。 */
export function ThemeProvider({ children }: { children: ReactNode }) {
    const [theme, setTheme] = useState<Theme>(readTheme);

    useEffect(() => {
        applyTheme(theme);
    }, [theme]);

    const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);

    return <Ctx.Provider value={{ theme, setTheme, toggle }}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeState {
    const value = useContext(Ctx);
    if (!value) throw new Error("useTheme 必须在 <ThemeProvider> 内使用");
    return value;
}
