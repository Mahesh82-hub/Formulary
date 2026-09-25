"use client";

import {
  Check,
  Copy,
  FileText,
  LoaderCircle,
  Pencil,
  RefreshCw,
  SearchCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { BrandMark } from "@/components/brand-mark";
import { ClarificationCard } from "@/components/chat/clarification-card";
import { MarkdownMessage } from "@/components/chat/markdown-message";
import { Button } from "@/components/ui/button";
import type { ClarificationBlock, Message, PdfAttachment } from "@/lib/types";
import { cn } from "@/lib/utils";

export type ToolActivity = {
  id: string;
  name: string;
  status: "running" | "completed" | "failed";
};

const STARTERS = [
  {
    icon: SearchCheck,
    title: "Research FDA data",
    prompt: "Search FDA sources and summarize the evidence for a drug with citations.",
  },
];

export function MessageList({
  messages,
  sending,
  assistantDraft,
  activities,
  onStarter,
  onEdit,
  onRegenerate,
  onClarificationSubmit,
}: {
  messages: Message[];
  sending: boolean;
  assistantDraft: string;
  activities: ToolActivity[];
  onStarter: (prompt: string) => void;
  onEdit: (message: Message, text: string) => void;
  onRegenerate: (message: Message) => void;
  onClarificationSubmit: (response: string) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, assistantDraft, activities, sending]);

  if (!messages.length && !sending) {
    return (
      <div className="flex flex-1 items-center overflow-y-auto px-5 py-10">
        <div className="chat-content-width mx-auto w-full animate-fade-up">
          <div className="mb-8 max-w-xl">
            <div className="mb-5 grid size-12 place-items-center rounded-2xl bg-[var(--primary-soft)] text-[var(--primary)]">
              <Sparkles className="size-5" />
            </div>
            <h2 className="text-3xl font-semibold tracking-[-0.04em] md:text-4xl">
              What are you researching?
            </h2>
            <p className="mt-3 max-w-lg text-sm leading-6 text-[var(--muted)]">
              Ask about FDA drug labels, approvals, adverse events, recalls, or shortages, and get
              answers grounded in cited sources.
            </p>
          </div>
          <div className="grid max-w-md gap-3">
            {STARTERS.map((starter) => (
              <button
                key={starter.title}
                onClick={() => onStarter(starter.prompt)}
                className="focus-ring group cursor-pointer rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:border-[var(--primary)]/35 hover:shadow-md"
              >
                <starter.icon className="mb-6 size-4 text-[var(--primary)]" />
                <span className="block text-sm font-medium">{starter.title}</span>
                <span className="mt-1.5 block text-xs leading-5 text-[var(--muted)]">
                  {starter.prompt}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="chat-content-width mx-auto w-full px-4 pb-10 pt-7 md:px-7 md:pt-10">
        {messages.map((message, index) => (
          <MessageRow
            key={message.id}
            message={message}
            clarificationActive={!sending && index === messages.length - 1}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
            onClarificationSubmit={onClarificationSubmit}
          />
        ))}
        {sending && (
          <div className="mb-7 flex gap-3.5 animate-fade-up">
            <BrandMark className="mt-0.5 size-8 rounded-lg" />
            <div className="min-w-0 flex-1 pt-1">
              {activities.length > 0 && (
                <div className="mb-3 space-y-1.5">
                  {activities.map((activity) => (
                    <div
                      key={activity.id}
                      className="flex w-fit items-center gap-2 rounded-lg bg-[var(--surface-soft)] px-2.5 py-1.5 text-[11px] text-[var(--muted)]"
                    >
                      {activity.status === "running" ? (
                        <LoaderCircle className="size-3 animate-spin text-[var(--primary)]" />
                      ) : activity.status === "completed" ? (
                        <Check className="size-3 text-[var(--primary)]" />
                      ) : (
                        <X className="size-3 text-[var(--danger)]" />
                      )}
                      {activity.status === "running" ? "Using" : "Used"} {toolLabel(activity.name)}
                    </div>
                  ))}
                </div>
              )}
              {assistantDraft ? (
                <MarkdownMessage>{assistantDraft}</MarkdownMessage>
              ) : (
                <div className="flex items-center gap-1.5 py-2 text-[var(--muted)]">
                  <span className="size-1.5 animate-pulse-soft rounded-full bg-current" />
                  <span className="size-1.5 animate-pulse-soft rounded-full bg-current [animation-delay:180ms]" />
                  <span className="size-1.5 animate-pulse-soft rounded-full bg-current [animation-delay:360ms]" />
                </div>
              )}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function MessageRow({
  message,
  clarificationActive,
  onEdit,
  onRegenerate,
  onClarificationSubmit,
}: {
  message: Message;
  clarificationActive: boolean;
  onEdit: (message: Message, text: string) => void;
  onRegenerate: (message: Message) => void;
  onClarificationSubmit: (response: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(message.plain_text);
  const isUser = message.role === "user";
  const pdfAttachments = message.content.filter(isPdfAttachment);
  const clarification = message.content.find(isClarificationBlock);

  async function copy() {
    await navigator.clipboard.writeText(message.plain_text);
    toast.success("Copied to clipboard");
  }

  return (
    <article className={cn("group mb-7 flex gap-3.5", isUser && "justify-end")}>
      {!isUser && <BrandMark className="mt-0.5 size-8 rounded-lg" />}
      <div className={cn("min-w-0", isUser ? "max-w-[82%]" : "max-w-[calc(100%-46px)] flex-1")}>
        {editing ? (
          <div className="rounded-2xl border border-[var(--primary)]/40 bg-[var(--surface)] p-2 shadow-sm">
            <textarea
              autoFocus
              rows={3}
              value={text}
              onChange={(event) => setText(event.target.value)}
              className="w-full resize-none bg-transparent px-2 py-1 text-sm leading-6 outline-none"
            />
            <div className="mt-1 flex justify-end gap-1.5">
              <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  onEdit(message, text);
                  setEditing(false);
                }}
                disabled={!text.trim() || text.trim() === message.plain_text}
              >
                Save & regenerate
              </Button>
            </div>
          </div>
        ) : (
          <>
            {isUser ? (
              <div className="rounded-[18px] rounded-br-md bg-[var(--primary-soft)] px-4 py-3">
                {pdfAttachments.map((attachment) => (
                  <div
                    key={`${attachment.filename}-${attachment.byte_size}`}
                    className="mb-2.5 flex min-w-0 items-center gap-2.5 rounded-xl border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_72%,transparent)] px-2.5 py-2"
                  >
                    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-[var(--surface)] text-[var(--primary)] shadow-sm">
                      <FileText className="size-4" />
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate text-xs font-medium">
                        {attachment.filename}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-[var(--muted)]">
                        PDF
                        {attachment.pages ? ` · ${attachment.pages} pages` : ""}
                        {attachment.truncated ? " · Context truncated" : ""}
                      </span>
                    </span>
                  </div>
                ))}
                <MarkdownMessage>{message.plain_text}</MarkdownMessage>
              </div>
            ) : (
              <>
                <MarkdownMessage>{message.plain_text}</MarkdownMessage>
                {clarification && (
                  <ClarificationCard
                    block={clarification}
                    disabled={!clarificationActive}
                    onSubmit={onClarificationSubmit}
                  />
                )}
              </>
            )}
          </>
        )}
        {!editing && (
          <div
            className={cn(
              "mt-1 flex items-center gap-0.5 opacity-0 transition group-hover:opacity-100 focus-within:opacity-100",
              isUser ? "justify-end" : "justify-start",
            )}
          >
            <Button variant="ghost" size="icon" className="size-7" onClick={copy} aria-label="Copy message">
              <Copy className="size-3.5" />
            </Button>
            {isUser && (
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => setEditing(true)}
                aria-label="Edit message"
              >
                <Pencil className="size-3.5" />
              </Button>
            )}
            {!isUser && (
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => onRegenerate(message)}
                aria-label="Regenerate response"
              >
                <RefreshCw className="size-3.5" />
              </Button>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

function isPdfAttachment(block: Record<string, unknown>): block is PdfAttachment {
  return (
    block.type === "document" &&
    block.media_type === "application/pdf" &&
    typeof block.filename === "string" &&
    typeof block.byte_size === "number"
  );
}

function isClarificationBlock(
  block: Record<string, unknown>,
): block is ClarificationBlock {
  return (
    block.type === "clarification" &&
    block.version === 1 &&
    block.status === "awaiting_user" &&
    Array.isArray(block.questions)
  );
}

const TOOL_LABELS: Record<string, string> = {
  search_all_sources: "every source",
  get_drug_composition: "FDA label ingredients",
  get_fda_drug_labels: "FDA drug labels",
  openfda_query: "openFDA",
  search_regulatory_events: "detected regulatory changes",
  analyze_fda_adverse_event_reactions: "FDA adverse-event reports",
  search_fda_drug_approvals: "Drugs@FDA approvals",
  search_fda_drug_shortages: "FDA drug shortages",
  search_fda_drug_recalls: "FDA recalls",
  search_fda_complete_response_letters: "FDA complete response letters",
  search_ingested_evidence: "previously retrieved evidence",
  read_ingested_document_chunks: "a retrieved document",
  ingest_openfda_query: "openFDA (full records)",
  ingest_fda_pdf_document: "an FDA PDF",
  request_user_clarification: "a clarifying question",
  web_search: "web search",
};

function toolLabel(name: string) {
  return TOOL_LABELS[name] ?? name.replaceAll("_", " ");
}
