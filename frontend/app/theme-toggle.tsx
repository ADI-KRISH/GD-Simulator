"use client";

import { useEffect, useState } from "react";  // state and effects

import { Icon } from "./icons";

type Theme = "light" | "dark";

/** Light / dark switch. The choice is saved in the browser and applied before first paint. */
export function ThemeToggle() {
  // Read after mount: the server cannot know the saved choice.
  const [theme, setTheme] = useState<Theme>("light");
  useEffect(() => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  }, []);

  /** Flip the theme, apply it to the page and remember it. */
  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("gd-theme", next);
    } catch {
      // Private mode: the switch still works for this visit.
    }
    setTheme(next);
  }

  const dark = theme === "dark";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggle}
      role="switch"
      aria-checked={dark}
      aria-label="Dark mode"
      title={dark ? "Switch to light mode" : "Switch to dark mode"}
    >
      <Icon name={dark ? "sun" : "moon"} />
    </button>
  );
}
