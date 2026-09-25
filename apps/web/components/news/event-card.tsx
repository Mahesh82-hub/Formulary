"use client";

import { ChevronDown, ExternalLink } from "lucide-react";
import { useState } from "react";

import type { RegulatoryEvent, Significance } from "@/lib/types";
import { cn } from "@/lib/utils";

export const EVENT_TYPE_LABELS: Record<RegulatoryEvent["event_type"], string> = {
  new_drug_approval: "New approval",
  generic_approval: "Generic approval",
  new_indication: "New indication",
  manufacturing_change: "Manufacturing change",
  safety_program_change: "REMS update",
  bioequivalence_supplement: "Bioequivalence",
  labeling_supplement: "Labeling supplement",
  other_supplement: "Supplement",
  label_change: "Label revision",
  trial_status_change: "Trial status",
  trial_registered: "New trial",
  trial_results_posted: "Trial results",
};

export const SIGNIFICANCE_LABELS: Record<Significance, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

function formatDay(value: string) {
  return new Date(`${value}T00:00:00`).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function SignificanceBadge({ level }: { level: Significance }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-md px-1.5 text-[10px] font-semibold uppercase tracking-[0.08em]",
        `significance-${level}`,
      )}
    >
      {SIGNIFICANCE_LABELS[level]}
    </span>
  );
}

export function EventCard({ event }: { event: RegulatoryEvent }) {
  const [open, setOpen] = useState(false);
  const changes = event.details.changes ?? [];
  const labels = event.details.labels ?? [];
  const documents = event.details.documents ?? [];
  const hasDetail = changes.length > 0 || labels.length > 1 || documents.length > 0;

  return (
    <article className="relative overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-[0_1px_2px_rgb(0_0_0/0.03)]">
      <span
        aria-hidden
        className={cn("absolute inset-y-0 left-0 w-1", `significance-rail-${event.significance}`)}
      />
      <div className="py-4 pl-5 pr-4 sm:pl-6 sm:pr-5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-[var(--muted)]">
          <SignificanceBadge level={event.significance} />
          <span className="font-medium text-[var(--foreground)]">
            {EVENT_TYPE_LABELS[event.event_type]}
          </span>
          <span aria-hidden>·</span>
          <time dateTime={event.occurred_on}>{formatDay(event.occurred_on)}</time>
          <span aria-hidden>·</span>
          <span>{event.source}</span>
        </div>

        <h3 className="mt-2 text-[15px] font-semibold leading-snug tracking-[-0.01em] text-[var(--foreground)]">
          <a
            href={event.source_url}
            target="_blank"
            rel="noreferrer"
            className="focus-ring rounded hover:text-[var(--primary)]"
          >
            {event.headline}
          </a>
        </h3>
        <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--muted)]">{event.summary}</p>

        {event.details.why_stopped && (
          <p className="mt-2 rounded-lg bg-[var(--surface-soft)] px-3 py-2 text-[12px] text-[var(--foreground)]">
            <span className="font-medium">Reason given: </span>
            {event.details.why_stopped}
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {event.drug_names.slice(0, 4).map((name) => (
            <span
              key={name}
              className="rounded-md bg-[var(--primary-soft)] px-2 py-0.5 text-[11px] font-medium text-[var(--primary)]"
            >
              {name}
            </span>
          ))}
          {event.sponsor && (
            <span className="rounded-md border border-[var(--border)] px-2 py-0.5 text-[11px] text-[var(--muted)]">
              {event.sponsor}
            </span>
          )}
        </div>

        {open && hasDetail && (
          <div className="mt-4 space-y-3 border-t border-[var(--border)] pt-4">
            {changes.map((change) => (
              <div key={change.section}>
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)]">
                  {change.label} · {change.change}
                </p>
                {(change.added_terms.length > 0 || change.removed_terms.length > 0) && (
                  <div className="mt-1.5 flex flex-wrap gap-1.5 text-[11px]">
                    {change.added_terms.map((term) => (
                      <span key={`+${term}`} className="significance-low rounded px-1.5 py-0.5 font-mono">
                        + {term}
                      </span>
                    ))}
                    {change.removed_terms.map((term) => (
                      <span key={`-${term}`} className="significance-high rounded px-1.5 py-0.5 font-mono">
                        − {term}
                      </span>
                    ))}
                  </div>
                )}
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <Excerpt title="Before" text={change.before} />
                  <Excerpt title="After" text={change.after} />
                </div>
              </div>
            ))}
            {labels.length > 1 && (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)]">
                  {labels.length} affected labels
                </p>
                <ul className="mt-1.5 space-y-1 text-[12px]">
                  {labels.map((label) => (
                    <li key={label.set_id}>
                      <a
                        href={label.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[var(--primary)] hover:underline"
                      >
                        {label.manufacturer ?? label.set_id}
                      </a>
                      <span className="text-[var(--muted)]"> · version {label.version}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {documents.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {documents.map((document) => (
                  <a
                    key={document.url}
                    href={document.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--border)] px-2.5 py-1 text-[12px] text-[var(--foreground)] hover:bg-[var(--surface-soft)]"
                  >
                    {document.type ?? "Document"} <ExternalLink className="size-3" />
                  </a>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <a
            href={event.source_url}
            target="_blank"
            rel="noreferrer"
            className="focus-ring inline-flex items-center gap-1 rounded text-[12px] font-medium text-[var(--primary)] hover:underline"
          >
            View official record <ExternalLink className="size-3" />
          </a>
          {hasDetail && (
            <button
              className="focus-ring inline-flex cursor-pointer items-center gap-1 rounded text-[12px] text-[var(--muted)] hover:text-[var(--foreground)]"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
            >
              {open ? "Hide details" : "What changed"}
              <ChevronDown className={cn("size-3.5 transition", open && "rotate-180")} />
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

function Excerpt({ title, text }: { title: string; text: string | null }) {
  return (
    <div className="rounded-lg bg-[var(--surface-soft)] p-2.5">
      <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)]">
        {title}
      </p>
      <p className="mt-1 line-clamp-6 text-[12px] leading-relaxed text-[var(--foreground)]">
        {text ?? <span className="italic text-[var(--muted)]">Not present</span>}
      </p>
    </div>
  );
}
