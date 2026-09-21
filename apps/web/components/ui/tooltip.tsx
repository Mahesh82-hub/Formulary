"use client";

import * as TooltipPrimitive from "@radix-ui/react-tooltip";

export function Tooltip({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={7}
          className="z-50 rounded-lg bg-[#15201b] px-2.5 py-1.5 text-xs text-white shadow-lg"
        >
          {label}
          <TooltipPrimitive.Arrow className="fill-[#15201b]" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
