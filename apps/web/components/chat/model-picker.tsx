"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Cpu, Server } from "lucide-react";

import { Input } from "@/components/ui/input";

export type ProviderChoice = "default" | "groq" | "openai";

export function ModelPicker({
  provider,
  model,
  onProviderChange,
  onModelChange,
}: {
  provider: ProviderChoice;
  model: string;
  onProviderChange: (provider: ProviderChoice) => void;
  onModelChange: (model: string) => void;
}) {
  const label = provider === "default" ? "Server default" : provider === "groq" ? "Groq" : "OpenAI";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="focus-ring flex h-8 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-[11px] font-medium text-[var(--muted)] hover:bg-[var(--surface-soft)] hover:text-[var(--foreground)]">
          <Cpu className="size-3.5" /> {label} <ChevronDown className="size-3" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={8}
          className="z-50 w-64 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-2 shadow-2xl"
        >
          <p className="px-2 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--muted)]">
            Model provider
          </p>
          {(["default", "groq", "openai"] as const).map((choice) => (
            <DropdownMenu.Item
              key={choice}
              className="flex cursor-pointer items-center gap-2 rounded-xl px-2.5 py-2 text-xs outline-none hover:bg-[var(--surface-soft)]"
              onSelect={() => onProviderChange(choice)}
            >
              <Server className="size-3.5 text-[var(--muted)]" />
              <span className="flex-1 capitalize">{choice === "default" ? "Server default" : choice}</span>
              {provider === choice && <Check className="size-3.5 text-[var(--primary)]" />}
            </DropdownMenu.Item>
          ))}
          <div className="mt-2 border-t border-[var(--border)] px-1 pt-2">
            <label className="space-y-1.5">
              <span className="px-1 text-[10px] font-medium text-[var(--muted)]">
                Model override (optional)
              </span>
              <Input
                value={model}
                onChange={(event) => onModelChange(event.target.value)}
                onKeyDown={(event) => event.stopPropagation()}
                placeholder="Use configured model"
                className="h-9 text-xs"
              />
            </label>
          </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
