"use client";

import { ArrowUp, FileText, Paperclip, Square, WandSparkles, X } from "lucide-react";
import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { ModelPicker, type ProviderChoice } from "@/components/chat/model-picker";
import { Button } from "@/components/ui/button";

const MAX_PDF_BYTES = 20_000_000;

export function Composer({
  disabled,
  sending,
  provider,
  model,
  onProviderChange,
  onModelChange,
  onSend,
  onStop,
}: {
  disabled?: boolean;
  sending: boolean;
  provider: ProviderChoice;
  model: string;
  onProviderChange: (provider: ProviderChoice) => void;
  onModelChange: (model: string) => void;
  onSend: (text: string, pdf?: File) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [pdf, setPdf] = useState<File>();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(element.scrollHeight, 180)}px`;
  }, [text]);

  function submit() {
    const value = text.trim() || (pdf ? "Please review the attached PDF." : "");
    if (!value || sending || disabled) return;
    const selectedPdf = pdf;
    setText("");
    setPdf(undefined);
    if (fileInputRef.current) fileInputRef.current.value = "";
    onSend(value, selectedPdf);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function selectPdf(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
    if (!isPdf) {
      toast.error("Only PDF attachments are supported");
      event.target.value = "";
      return;
    }
    if (file.size > MAX_PDF_BYTES) {
      toast.error("PDF attachments must be smaller than 20 MB");
      event.target.value = "";
      return;
    }
    setPdf(file);
  }

  function removePdf() {
    setPdf(undefined);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  return (
    <div className="chat-content-width mx-auto w-full px-3 pb-3 md:px-6 md:pb-5">
      <div className="rounded-[22px] border border-[var(--border)] bg-[var(--surface)] p-2 shadow-[var(--shadow)] transition focus-within:border-[var(--primary)]/50">
        {pdf && (
          <div className="mx-1 mb-1 flex items-center gap-2.5 rounded-xl border border-[var(--border)] bg-[var(--surface-soft)] px-3 py-2">
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-[var(--primary-soft)] text-[var(--primary)]">
              <FileText className="size-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-medium">{pdf.name}</span>
              <span className="mt-0.5 block text-[10px] text-[var(--muted)]">
                PDF · {formatFileSize(pdf.size)}
              </span>
            </span>
            <button
              type="button"
              className="focus-ring grid size-7 cursor-pointer place-items-center rounded-lg text-[var(--muted)] transition hover:bg-[var(--surface-strong)] hover:text-[var(--foreground)]"
              onClick={removePdf}
              aria-label={`Remove ${pdf.name}`}
            >
              <X className="size-3.5" />
            </button>
          </div>
        )}
        <textarea
          ref={textareaRef}
          rows={1}
          value={text}
          disabled={disabled || sending}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            disabled
              ? "This conversation is archived"
              : pdf
                ? "Ask a question about the attached PDF…"
                : "Ask about a drug, label, study, or mechanism…"
          }
          className="max-h-[180px] min-h-12 w-full resize-none bg-transparent px-3 py-2.5 text-[15px] leading-6 outline-none placeholder:text-[var(--muted)]/65 disabled:cursor-not-allowed"
        />
        <div className="flex items-center gap-1 px-1 pb-0.5">
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={selectPdf}
          />
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            disabled={disabled || sending}
            title="Attach PDF"
            aria-label="Attach PDF"
            onClick={() => fileInputRef.current?.click()}
          >
            <Paperclip />
          </Button>
          <ModelPicker
            provider={provider}
            model={model}
            onProviderChange={onProviderChange}
            onModelChange={onModelChange}
          />
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden items-center gap-1 text-[10px] text-[var(--muted)] sm:flex">
              <WandSparkles className="size-3" /> MCP tools enabled
            </span>
            {sending ? (
              <Button size="icon" className="size-9 rounded-xl" onClick={onStop} aria-label="Stop response">
                <Square className="size-3.5 fill-current" />
              </Button>
            ) : (
              <Button
                size="icon"
                className="size-9 rounded-xl"
                onClick={submit}
                disabled={disabled || (!text.trim() && !pdf)}
                aria-label="Send message"
              >
                <ArrowUp />
              </Button>
            )}
          </div>
        </div>
      </div>
      <p className="mt-2 text-center text-[10px] leading-4 text-[var(--muted)]">
        Formulary can make mistakes. Verify critical pharmaceutical and regulatory information.
      </p>
    </div>
  );
}

function formatFileSize(bytes: number) {
  if (bytes < 1_000_000) return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}
