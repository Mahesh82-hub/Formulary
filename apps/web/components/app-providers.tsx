"use client";

import * as TooltipPrimitive from "@radix-ui/react-tooltip";

export function AppProviders({ children }: { children: React.ReactNode }) {
  return <TooltipPrimitive.Provider delayDuration={350}>{children}</TooltipPrimitive.Provider>;
}
