"use client";

import { Bell, BellOff, Eye, Mail, MessageSquare, Plus, Trash2, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";

import { EVENT_TYPE_LABELS, SignificanceBadge } from "@/components/news/event-card";
import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api";
import type { RegulatoryEvent, Significance, Watch } from "@/lib/types";
import { cn, formatRelativeDate } from "@/lib/utils";

const fieldClass =
  "focus-ring h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 text-[13px] text-[var(--foreground)] outline-none placeholder:text-[var(--muted)] focus:border-[var(--primary)]";

export function WatchesPanel({
  watches,
  onChange,
}: {
  watches: Watch[];
  onChange: (watches: Watch[]) => void;
}) {
  const [creating, setCreating] = useState(watches.length === 0);

  return (
    <section aria-labelledby="watches-heading" className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 id="watches-heading" className="text-sm font-semibold tracking-tight">
            Watches
          </h2>
          <p className="text-[11px] text-[var(--muted)]">Alerts by email or Slack</p>
        </div>
        {!creating && (
          <Button size="sm" variant="secondary" onClick={() => setCreating(true)}>
            <Plus /> New
          </Button>
        )}
      </div>

      {creating && (
        <WatchForm
          onCancel={watches.length > 0 ? () => setCreating(false) : undefined}
          onCreated={(watch) => {
            onChange([watch, ...watches]);
            setCreating(false);
          }}
        />
      )}

      {watches.map((watch) => (
        <WatchItem
          key={watch.id}
          watch={watch}
          onUpdated={(next) => onChange(watches.map((item) => (item.id === next.id ? next : item)))}
          onDeleted={() => onChange(watches.filter((item) => item.id !== watch.id))}
        />
      ))}
    </section>
  );
}

function WatchForm({
  onCreated,
  onCancel,
}: {
  onCreated: (watch: Watch) => void;
  onCancel?: () => void;
}) {
  const [name, setName] = useState("");
  const [terms, setTerms] = useState("");
  const [minimum, setMinimum] = useState<Significance>("medium");
  const [email, setEmail] = useState(true);
  const [slack, setSlack] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const list = terms
      .split(",")
      .map((term) => term.trim())
      .filter(Boolean);
    if (list.length === 0) {
      toast.error("Add at least one drug, company, or condition.");
      return;
    }
    setSaving(true);
    try {
      const watch = await apiFetch<Watch>("/api/v1/intelligence/watches", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim() || list.slice(0, 2).join(", "),
          terms: list,
          min_significance: minimum,
          notify_email: email,
          slack_webhook_url: slack.trim() || null,
        }),
      });
      toast.success("Watch created. Current labels are being captured as a baseline.");
      onCreated(watch);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not create the watch");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="space-y-2.5 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4"
    >
      <div className="flex items-center justify-between">
        <p className="text-[13px] font-semibold">New watch</p>
        {onCancel && (
          <button
            type="button"
            className="focus-ring cursor-pointer rounded text-[var(--muted)] hover:text-[var(--foreground)]"
            onClick={onCancel}
            aria-label="Cancel"
          >
            <X className="size-4" />
          </button>
        )}
      </div>
      <label className="block space-y-1">
        <span className="text-[11px] font-medium text-[var(--muted)]">Drugs, companies, or conditions</span>
        <input
          className={fieldClass}
          value={terms}
          onChange={(event) => setTerms(event.target.value)}
          placeholder="semaglutide, tirzepatide, Novo Nordisk"
          required
        />
      </label>
      <label className="block space-y-1">
        <span className="text-[11px] font-medium text-[var(--muted)]">Name (optional)</span>
        <input
          className={fieldClass}
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="GLP-1 competitors"
          maxLength={120}
        />
      </label>
      <label className="block space-y-1">
        <span className="text-[11px] font-medium text-[var(--muted)]">Alert me for</span>
        <select
          className={fieldClass}
          value={minimum}
          onChange={(event) => setMinimum(event.target.value as Significance)}
        >
          <option value="high">High significance only</option>
          <option value="medium">Medium and high</option>
          <option value="low">Everything</option>
        </select>
      </label>
      <label className="flex items-center gap-2 text-[13px]">
        <input type="checkbox" checked={email} onChange={(event) => setEmail(event.target.checked)} />
        Email me
      </label>
      <label className="block space-y-1">
        <span className="text-[11px] font-medium text-[var(--muted)]">Slack incoming webhook (optional)</span>
        <input
          className={fieldClass}
          value={slack}
          onChange={(event) => setSlack(event.target.value)}
          placeholder="https://hooks.slack.com/services/…"
          type="url"
          autoComplete="off"
        />
      </label>
      <Button type="submit" size="sm" className="w-full" disabled={saving}>
        {saving ? "Creating…" : "Create watch"}
      </Button>
      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        You&apos;ll be alerted to changes detected from now on. Use preview to see what it would
        have caught in the last 30 days.
      </p>
    </form>
  );
}

function WatchItem({
  watch,
  onUpdated,
  onDeleted,
}: {
  watch: Watch;
  onUpdated: (watch: Watch) => void;
  onDeleted: () => void;
}) {
  const [preview, setPreview] = useState<RegulatoryEvent[] | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  async function togglePreview() {
    if (preview) {
      setPreview(null);
      return;
    }
    setLoadingPreview(true);
    try {
      setPreview(
        await apiFetch<RegulatoryEvent[]>(`/api/v1/intelligence/watches/${watch.id}/matches`),
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not load the preview");
    } finally {
      setLoadingPreview(false);
    }
  }

  async function toggleActive() {
    try {
      onUpdated(
        await apiFetch<Watch>(`/api/v1/intelligence/watches/${watch.id}`, {
          method: "PATCH",
          body: JSON.stringify({ active: !watch.active }),
        }),
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not update the watch");
    }
  }

  async function remove() {
    if (!window.confirm(`Delete the watch “${watch.name}”?`)) return;
    try {
      await apiFetch<void>(`/api/v1/intelligence/watches/${watch.id}`, { method: "DELETE" });
      onDeleted();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not delete the watch");
    }
  }

  return (
    <div
      className={cn(
        "rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4",
        !watch.active && "opacity-60",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[13px] font-semibold">{watch.name}</p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {watch.terms.map((term) => (
              <span
                key={term}
                className="rounded-md bg-[var(--primary-soft)] px-1.5 py-0.5 text-[11px] text-[var(--primary)]"
              >
                {term}
              </span>
            ))}
          </div>
        </div>
        <div className="flex shrink-0 items-center">
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={toggleActive}
            aria-label={watch.active ? "Pause watch" : "Resume watch"}
            title={watch.active ? "Pause" : "Resume"}
          >
            {watch.active ? <Bell /> : <BellOff />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={remove}
            aria-label="Delete watch"
            title="Delete"
          >
            <Trash2 />
          </Button>
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--muted)]">
        <span className="inline-flex items-center gap-1">
          <SignificanceBadge level={watch.min_significance} /> and above
        </span>
        {watch.notify_email && (
          <span className="inline-flex items-center gap-1">
            <Mail className="size-3" /> Email
          </span>
        )}
        {watch.slack_configured && (
          <span className="inline-flex items-center gap-1" title={watch.slack_webhook_hint ?? ""}>
            <MessageSquare className="size-3" /> Slack
          </span>
        )}
        <span>
          {watch.last_notified_at
            ? `Last alert ${formatRelativeDate(watch.last_notified_at)}`
            : "No alerts sent yet"}
        </span>
      </div>

      {watch.last_delivery_error && (
        <p className="significance-high mt-2 rounded-lg px-2.5 py-1.5 text-[11px]">
          Last delivery failed: {watch.last_delivery_error}. It will be retried.
        </p>
      )}

      <button
        className="focus-ring mt-3 inline-flex cursor-pointer items-center gap-1 rounded text-[12px] font-medium text-[var(--primary)] hover:underline"
        onClick={togglePreview}
        disabled={loadingPreview}
      >
        <Eye className="size-3.5" />
        {loadingPreview ? "Loading…" : preview ? "Hide preview" : "Preview last 30 days"}
      </button>

      {preview && (
        <ul className="mt-2 space-y-2 border-t border-[var(--border)] pt-2">
          {preview.length === 0 && (
            <li className="text-[12px] text-[var(--muted)]">
              Nothing matched in the last 30 days.
            </li>
          )}
          {preview.slice(0, 6).map((event) => (
            <li key={event.id} className="text-[12px] leading-snug">
              <span className="text-[var(--muted)]">{EVENT_TYPE_LABELS[event.event_type]} · </span>
              <a
                href={event.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-[var(--foreground)] hover:text-[var(--primary)]"
              >
                {event.headline}
              </a>
            </li>
          ))}
          {preview.length > 6 && (
            <li className="text-[11px] text-[var(--muted)]">and {preview.length - 6} more</li>
          )}
        </ul>
      )}
    </div>
  );
}
