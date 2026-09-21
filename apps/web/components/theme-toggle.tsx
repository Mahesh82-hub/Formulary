"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";

export function ThemeToggle() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("formulary-theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const enabled = saved ? saved === "dark" : prefersDark;
    document.documentElement.classList.toggle("dark", enabled);
    queueMicrotask(() => setDark(enabled));
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("formulary-theme", next ? "dark" : "light");
  }

  return (
    <Tooltip label={dark ? "Use light theme" : "Use dark theme"}>
      <Button variant="ghost" size="icon" onClick={toggle} aria-label="Toggle theme">
        {dark ? <Sun /> : <Moon />}
      </Button>
    </Tooltip>
  );
}
