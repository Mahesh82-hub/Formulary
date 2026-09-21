"use client";

import { Menu, MoreHorizontal, ShieldCheck } from "lucide-react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import type { ConversationDetail } from "@/lib/types";

export function ChatHeader({
  conversation,
  onOpenSidebar,
}: {
  conversation: ConversationDetail | null;
  onOpenSidebar: () => void;
}) {
  return (
    <header className="glass z-20 flex h-16 shrink-0 items-center border-b border-[var(--border)] px-3 md:px-5">
      <Button
        variant="ghost"
        size="icon"
        className="mr-1 lg:hidden"
        onClick={onOpenSidebar}
        aria-label="Open sidebar"
      >
        <Menu />
      </Button>
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-sm font-semibold tracking-tight">
          {conversation?.title ?? "New conversation"}
        </h1>
        <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-[var(--muted)]">
          <ShieldCheck className="size-3 text-[var(--primary)]" />
          Evidence-aware research assistant
        </div>
      </div>
      <div className="flex items-center gap-1">
        <ThemeToggle />
        <Button variant="ghost" size="icon" aria-label="Conversation options">
          <MoreHorizontal />
        </Button>
      </div>
    </header>
  );
}
