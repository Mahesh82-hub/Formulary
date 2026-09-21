"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Archive,
  ChevronDown,
  LogOut,
  MessageSquareText,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

import { BrandMark } from "@/components/brand-mark";
import type { Conversation, User } from "@/lib/types";
import { cn, formatRelativeDate, initials } from "@/lib/utils";

type SidebarProps = {
  conversations: Conversation[];
  selectedId: string | null;
  user: User;
  open: boolean;
  onOpenChange: (value: boolean) => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onArchive: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
  onLogout: () => void;
};

export function Sidebar({
  conversations,
  selectedId,
  user,
  open,
  onOpenChange,
  onNew,
  onSelect,
  onArchive,
  onDelete,
  onLogout,
}: SidebarProps) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const filtered = useMemo(
    () =>
      conversations.filter((conversation) =>
        (conversation.title ?? "New conversation").toLowerCase().includes(query.toLowerCase()),
      ),
    [conversations, query],
  );

  return (
    <>
      {open && (
        <button
          className="fixed inset-0 z-30 cursor-default bg-black/35 backdrop-blur-[2px] lg:hidden"
          aria-label="Close navigation"
          onClick={() => onOpenChange(false)}
        />
      )}
      <aside
        className={cn(
          "sidebar-shell fixed inset-y-0 left-0 z-40 flex w-[300px] flex-col border-r border-white/[0.08] text-white transition-[width,transform] duration-300 ease-out lg:static lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
          collapsed && "lg:w-[84px]",
        )}
      >
        <div
          className={cn(
            "flex h-[82px] shrink-0 items-center gap-3.5 px-5",
            collapsed && "lg:justify-center lg:px-3",
          )}
        >
          <BrandMark className="size-11 rounded-[15px]" />
          <div className={cn("min-w-0 flex-1", collapsed && "lg:hidden")}>
            <p className="truncate text-[15px] font-semibold tracking-[-0.02em]">Formulary</p>
            <p className="mt-0.5 text-[9px] font-semibold uppercase tracking-[0.18em] text-white/45">
              Research intelligence
            </p>
          </div>
          <button
            className="focus-ring grid size-9 shrink-0 cursor-pointer place-items-center rounded-xl text-white/55 transition hover:bg-white/[0.07] hover:text-white lg:hidden"
            onClick={() => onOpenChange(false)}
            aria-label="Close sidebar"
          >
            <X className="size-[18px]" />
          </button>
        </div>

        <div className={cn("px-3 pb-5", collapsed && "lg:px-3")}>
          <button
            className={cn(
              "focus-ring flex h-10 w-full cursor-pointer items-center gap-2.5 rounded-xl border border-white/[0.09] bg-white/[0.07] px-3.5 text-left text-[12px] font-medium text-white/85 transition-all duration-200 hover:border-white/[0.14] hover:bg-white/[0.11] hover:text-white active:scale-[0.985]",
              collapsed && "lg:justify-center lg:px-0",
            )}
            onClick={onNew}
          >
            <Plus className="size-4 shrink-0 text-[#c987ee]" strokeWidth={2} />
            <span className={cn(collapsed && "lg:hidden")}>New conversation</span>
          </button>

          <div className={cn("relative mt-3", collapsed && "lg:hidden")}>
            <Search className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-white/40" />
            <input
              type="search"
              autoComplete="off"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search conversations"
              className="sidebar-search focus-ring h-10 w-full rounded-xl border border-white/[0.07] bg-white/[0.055] pl-10 pr-3 text-[12px] text-white outline-none transition placeholder:text-white/35 hover:bg-white/[0.075] focus:border-white/20 focus:bg-white/[0.09]"
            />
          </div>
          {collapsed && (
            <button
              className="focus-ring mt-3 hidden size-11 cursor-pointer place-items-center rounded-xl text-white/55 transition hover:bg-white/[0.07] hover:text-white lg:grid"
              onClick={() => setCollapsed(false)}
              aria-label="Search conversations"
              title="Search conversations"
            >
              <Search className="size-[18px]" />
            </button>
          )}
        </div>

        <div className="sidebar-scroll flex-1 overflow-y-auto px-3 pb-4">
          <div
            className={cn(
              "flex items-center justify-between px-2 pb-2.5 pt-1",
              collapsed && "lg:justify-center lg:px-0",
            )}
          >
            <p
              className={cn(
                "text-[10px] font-semibold uppercase tracking-[0.17em] text-white/38",
                collapsed && "lg:hidden",
              )}
            >
              Recent conversations
            </p>
            <span
              className={cn(
                "rounded-md bg-white/[0.06] px-1.5 py-0.5 text-[9px] font-medium text-white/38",
                collapsed && "lg:hidden",
              )}
            >
              {filtered.length}
            </span>
          </div>
          <div className="space-y-1">
            {filtered.map((conversation) => (
              <div
                key={conversation.id}
                className={cn(
                  "group relative flex items-center rounded-xl transition",
                  selectedId === conversation.id
                    ? "bg-white/[0.11] text-white shadow-[inset_0_0_0_1px_rgb(255_255_255/0.06)]"
                    : "text-white/55 hover:bg-white/[0.065] hover:text-white/90",
                )}
              >
                {selectedId === conversation.id && (
                  <span className="absolute bottom-2.5 left-0 top-2.5 w-0.5 rounded-full bg-[#c47bf0]" />
                )}
                <button
                  className={cn(
                    "focus-ring flex min-w-0 flex-1 cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-left",
                    collapsed && "lg:justify-center lg:px-0",
                  )}
                  onClick={() => {
                    onSelect(conversation.id);
                    onOpenChange(false);
                  }}
                  title={collapsed ? (conversation.title ?? "New conversation") : undefined}
                >
                  <MessageSquareText className="size-[17px] shrink-0" strokeWidth={1.8} />
                  <span className={cn("min-w-0 flex-1", collapsed && "lg:hidden")}>
                    <span className="block truncate text-[12px] font-medium tracking-[-0.005em]">
                      {conversation.title ?? "New conversation"}
                    </span>
                    <span className="mt-0.5 block text-[9px] text-white/32">
                      {formatRelativeDate(conversation.updated_at)}
                    </span>
                  </span>
                </button>
                <DropdownMenu.Root>
                  <DropdownMenu.Trigger asChild>
                    <button
                      className={cn(
                        "focus-ring mr-1 grid size-8 cursor-pointer place-items-center rounded-lg text-white/45 opacity-0 transition hover:bg-white/10 hover:text-white group-hover:opacity-100 data-[state=open]:opacity-100",
                        collapsed && "lg:hidden",
                      )}
                      aria-label={`Options for ${conversation.title ?? "conversation"}`}
                    >
                      <MoreHorizontal className="size-4" />
                    </button>
                  </DropdownMenu.Trigger>
                  <DropdownMenu.Portal>
                    <DropdownMenu.Content
                      align="start"
                      sideOffset={5}
                      className="z-50 min-w-40 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-1.5 text-xs shadow-xl"
                    >
                      <DropdownMenu.Item
                        className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 outline-none hover:bg-[var(--surface-soft)]"
                        onSelect={() => onArchive(conversation)}
                      >
                        <Archive className="size-3.5" />
                        {conversation.status === "archived" ? "Restore" : "Archive"}
                      </DropdownMenu.Item>
                      <DropdownMenu.Item
                        className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-[var(--danger)] outline-none hover:bg-red-500/10"
                        onSelect={() => onDelete(conversation)}
                      >
                        <Trash2 className="size-3.5" /> Delete
                      </DropdownMenu.Item>
                    </DropdownMenu.Content>
                  </DropdownMenu.Portal>
                </DropdownMenu.Root>
              </div>
            ))}
            {!filtered.length && (
              <p
                className={cn(
                  "px-3 py-8 text-center text-[11px] leading-5 text-white/35",
                  collapsed && "lg:hidden",
                )}
              >
                {query ? "No matching conversations" : "Your conversations will appear here"}
              </p>
            )}
          </div>
        </div>

        <div className="relative hidden shrink-0 px-3 pb-3 lg:block">
          <button
            className={cn(
              "focus-ring flex h-10 cursor-pointer items-center gap-2 rounded-xl px-3 text-[11px] font-medium text-white/42 transition hover:bg-white/[0.06] hover:text-white/85",
              collapsed && "mx-auto justify-center px-0",
            )}
            onClick={() => setCollapsed((current) => !current)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? (
              <PanelLeftOpen className="size-[17px]" />
            ) : (
              <PanelLeftClose className="size-[17px]" />
            )}
            <span className={cn(collapsed && "hidden")}>Collapse sidebar</span>
          </button>
        </div>

        <div className="shrink-0 border-t border-white/[0.08] p-3">
          <div
            className={cn(
              "flex items-center gap-3 rounded-xl p-2",
              collapsed && "lg:flex-col lg:p-1.5",
            )}
          >
            <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-[#a350d9]/20 text-[11px] font-semibold text-[#dfa9f6] ring-1 ring-white/10">
              {initials(user.display_name ?? user.email)}
            </div>
            <div className={cn("min-w-0 flex-1", collapsed && "lg:hidden")}>
              <p className="truncate text-[12px] font-semibold tracking-[-0.005em] text-white/90">
                {user.display_name ?? user.email}
              </p>
              {user.display_name && (
                <p className="mt-0.5 truncate text-[10px] text-white/38">{user.email}</p>
              )}
            </div>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <button
                  className={cn(
                    "focus-ring grid size-9 shrink-0 cursor-pointer place-items-center rounded-xl text-white/45 transition hover:bg-white/[0.07] hover:text-white",
                    collapsed && "lg:hidden",
                  )}
                  aria-label="Account menu"
                >
                  <ChevronDown className="size-4" />
                </button>
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content
                  align="end"
                  side="top"
                  sideOffset={8}
                  className="z-50 min-w-44 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-1.5 text-xs shadow-xl"
                >
                  <DropdownMenu.Item
                    className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-[var(--danger)] outline-none hover:bg-red-500/10"
                    onSelect={onLogout}
                  >
                    <LogOut className="size-3.5" /> Sign out
                  </DropdownMenu.Item>
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
            {collapsed && (
              <button
                className="focus-ring hidden size-9 cursor-pointer place-items-center rounded-xl text-white/45 transition hover:bg-white/[0.07] hover:text-white lg:grid"
                onClick={onLogout}
                aria-label="Sign out"
                title="Sign out"
              >
                <LogOut className="size-4" />
              </button>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}
