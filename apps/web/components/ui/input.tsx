import type * as React from "react";

import { cn } from "@/lib/utils";

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "focus-ring h-11 w-full rounded-xl border border-[var(--border)] bg-[var(--surface)] px-3.5 text-sm text-[var(--foreground)] shadow-sm transition placeholder:text-[var(--muted)]/70 disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
