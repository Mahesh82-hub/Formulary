"use client";

import { ArrowLeft, Loader2, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { EventCard } from "@/components/news/event-card";
import { WatchesPanel } from "@/components/news/watches-panel";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { ApiError, apiFetch } from "@/lib/api";
import type {
  EventFeed,
  MonitorRun,
  RegulatoryEvent,
  RegulatoryEventType,
  Significance,
  User,
  Watch,
} from "@/lib/types";
import { cn, formatRelativeDate } from "@/lib/utils";

const SOURCES = ["openFDA", "ClinicalTrials.gov"] as const;
const TYPE_GROUPS: Array<{ label: string; types: RegulatoryEventType[] }> = [
  { label: "All changes", types: [] },
  { label: "Approvals", types: ["new_drug_approval", "generic_approval", "new_indication"] },
  {
    label: "Label & formulation",
    types: ["label_change", "manufacturing_change", "labeling_supplement"],
  },
  { label: "Safety programs", types: ["safety_program_change"] },
  { label: "Clinical trials", types: ["trial_status_change", "trial_results_posted", "trial_registered"] },
];

type Filters = {
  minimum: Significance;
  group: number;
  source: (typeof SOURCES)[number] | null;
  query: string;
};

const INITIAL_FILTERS: Filters = { minimum: "medium", group: 0, source: null, query: "" };

function feedPath(filters: Filters, cursor?: string | null) {
  const params = new URLSearchParams({ min_significance: filters.minimum, limit: "30" });
  for (const type of TYPE_GROUPS[filters.group].types) params.append("event_type", type);
  if (filters.source) params.append("source", filters.source);
  if (filters.query.trim()) params.set("q", filters.query.trim());
  if (cursor) params.set("cursor", cursor);
  return `/api/v1/intelligence/events?${params.toString()}`;
}

function dayHeading(value: string) {
  const day = new Date(`${value}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const difference = Math.round((today.getTime() - day.getTime()) / 86_400_000);
  if (difference === 0) return "Today";
  if (difference === 1) return "Yesterday";
  return day.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

export function NewsApp() {
  const router = useRouter();
  const [filters, setFilters] = useState<Filters>(INITIAL_FILTERS);
  const [draftQuery, setDraftQuery] = useState("");
  const [feed, setFeed] = useState<EventFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [watches, setWatches] = useState<Watch[] | null>(null);
  const [runs, setRuns] = useState<MonitorRun[]>([]);
  const [checking, setChecking] = useState(false);
  const pollRef = useRef<number | null>(null);

  const handleError = useCallback(
    (error: unknown, fallback: string) => {
      if (error instanceof ApiError && error.status === 401) {
        router.replace("/login");
        return;
      }
      toast.error(error instanceof Error ? error.message : fallback);
    },
    [router],
  );

  const loadFeed = useCallback(
    async (next: Filters) => {
      try {
        setFeed(await apiFetch<EventFeed>(feedPath(next)));
      } catch (error) {
        handleError(error, "Could not load the news feed");
      } finally {
        setLoading(false);
      }
    },
    [handleError],
  );

  // Filters are applied from the handlers that change them, not from an effect, so each
  // change costs exactly one render and one request.
  function updateFilters(next: Filters) {
    setFilters(next);
    setLoading(true);
    void loadFeed(next);
  }

  const loadRuns = useCallback(async () => {
    const latest = await apiFetch<MonitorRun[]>("/api/v1/intelligence/runs");
    setRuns(latest);
    return latest;
  }, []);

  useEffect(() => {
    apiFetch<User>("/api/v1/auth/me")
      .then(() =>
        Promise.all([
          apiFetch<EventFeed>(feedPath(INITIAL_FILTERS)).then(setFeed),
          apiFetch<Watch[]>("/api/v1/intelligence/watches").then(setWatches),
          loadRuns(),
        ]),
      )
      .catch((error) => handleError(error, "Could not load regulatory news"))
      .finally(() => setLoading(false));
  }, [handleError, loadRuns]);

  useEffect(
    () => () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    },
    [],
  );

  async function loadMore() {
    if (!feed?.next_cursor) return;
    setLoadingMore(true);
    try {
      const page = await apiFetch<EventFeed>(feedPath(filters, feed.next_cursor));
      setFeed({ ...page, events: [...feed.events, ...page.events] });
    } catch (error) {
      handleError(error, "Could not load more events");
    } finally {
      setLoadingMore(false);
    }
  }

  async function checkNow() {
    setChecking(true);
    const startedAt = Date.now();
    try {
      await apiFetch("/api/v1/intelligence/runs", { method: "POST" });
      toast.message("Checking FDA and ClinicalTrials.gov for changes…");
      pollRef.current = window.setInterval(async () => {
        try {
          const latest = await loadRuns();
          const fresh = latest.filter((run) => new Date(run.started_at).getTime() >= startedAt - 5_000);
          const finished = fresh.length > 0 && fresh.every((run) => run.status !== "running");
          const timedOut = Date.now() - startedAt > 5 * 60_000;
          if (finished || timedOut) {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            setChecking(false);
            const created = fresh.reduce((total, run) => total + run.events_created, 0);
            const failed = fresh.filter((run) => run.status === "failed");
            if (failed.length) {
              toast.error(`${failed.length} source check failed: ${failed[0].error ?? "unknown error"}`);
            } else if (!timedOut) {
              toast.success(created ? `${created} new changes found` : "No new changes since the last check");
            }
            void loadFeed(filters);
          }
        } catch {
          // A transient polling failure is retried on the next tick.
        }
      }, 4_000);
    } catch (error) {
      setChecking(false);
      handleError(error, "Could not start a check");
    }
  }

  const lastCompleted = runs.find((run) => run.status === "completed");
  const grouped = groupByDay(feed?.events ?? []);

  return (
    <div className="min-h-dvh bg-[var(--background)]">
      <header className="glass sticky top-0 z-20 border-b border-[var(--border)]">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-3 px-4 sm:px-6">
          <Button asChild variant="ghost" size="icon" aria-label="Back to chat">
            <Link href="/chat">
              <ArrowLeft />
            </Link>
          </Button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-sm font-semibold tracking-tight">Regulatory news</h1>
            <p className="truncate text-[11px] text-[var(--muted)]">
              {lastCompleted?.completed_at
                ? `Sources last checked ${formatRelativeDate(lastCompleted.completed_at)}`
                : "Changes detected from FDA and ClinicalTrials.gov"}
            </p>
          </div>
          <Button size="sm" variant="secondary" onClick={checkNow} disabled={checking}>
            {checking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            <span className="hidden sm:inline">{checking ? "Checking…" : "Check sources now"}</span>
          </Button>
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl gap-6 px-4 py-6 sm:px-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          <div className="grid grid-cols-3 gap-2 sm:gap-3">
            {(["high", "medium", "low"] as const).map((level) => (
              <button
                key={level}
                className={cn(
                  "focus-ring cursor-pointer rounded-2xl border bg-[var(--surface)] p-3 text-left transition",
                  filters.minimum === level
                    ? "border-[var(--primary)]"
                    : "border-[var(--border)] hover:border-[var(--surface-strong)]",
                )}
                onClick={() => updateFilters({ ...filters, minimum: level })}
              >
                <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--muted)]">
                  {level === "low" ? "All" : level === "medium" ? "Medium +" : "High"}
                </p>
                <p className="mt-1 text-xl font-semibold tabular-nums tracking-tight">
                  {feed
                    ? level === "high"
                      ? feed.last_7_days.high
                      : level === "medium"
                        ? feed.last_7_days.high + feed.last_7_days.medium
                        : feed.last_7_days.high + feed.last_7_days.medium + feed.last_7_days.low
                    : "–"}
                </p>
                <p className="text-[11px] text-[var(--muted)]">past 7 days</p>
              </button>
            ))}
          </div>

          <div className="space-y-2.5 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <form
              className="relative"
              onSubmit={(event) => {
                event.preventDefault();
                updateFilters({ ...filters, query: draftQuery });
              }}
            >
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--muted)]" />
              <input
                type="search"
                value={draftQuery}
                onChange={(event) => {
                  setDraftQuery(event.target.value);
                  if (!event.target.value) updateFilters({ ...filters, query: "" });
                }}
                placeholder="Search a drug, company, or condition"
                className="focus-ring h-10 w-full rounded-xl border border-[var(--border)] bg-[var(--background)] pl-9 pr-3 text-[13px] outline-none placeholder:text-[var(--muted)] focus:border-[var(--primary)]"
              />
            </form>
            <div className="flex flex-wrap gap-1.5">
              {TYPE_GROUPS.map((group, index) => (
                <Chip
                  key={group.label}
                  active={filters.group === index}
                  onClick={() => updateFilters({ ...filters, group: index })}
                >
                  {group.label}
                </Chip>
              ))}
              <span className="mx-1 w-px self-stretch bg-[var(--border)]" aria-hidden />
              {SOURCES.map((source) => (
                <Chip
                  key={source}
                  active={filters.source === source}
                  onClick={() =>
                    updateFilters({ ...filters, source: filters.source === source ? null : source })
                  }
                >
                  {source}
                </Chip>
              ))}
            </div>
          </div>

          {loading && !feed ? (
            <FeedSkeleton />
          ) : grouped.length === 0 ? (
            <EmptyFeed hasRuns={runs.length > 0} checking={checking} onCheck={checkNow} />
          ) : (
            <div className={cn("space-y-6 transition-opacity", loading && "opacity-60")}>
              {grouped.map(([day, events]) => (
                <section key={day} aria-label={dayHeading(day)}>
                  <h2 className="mb-2.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--muted)]">
                    {dayHeading(day)}
                  </h2>
                  <div className="space-y-2.5">
                    {events.map((event) => (
                      <EventCard key={event.id} event={event} />
                    ))}
                  </div>
                </section>
              ))}
              {feed?.next_cursor && (
                <Button
                  variant="secondary"
                  className="w-full"
                  onClick={loadMore}
                  disabled={loadingMore}
                >
                  {loadingMore ? "Loading…" : "Load older changes"}
                </Button>
              )}
            </div>
          )}
        </div>

        <aside className="space-y-6 lg:sticky lg:top-22 lg:self-start">
          {watches ? (
            <WatchesPanel watches={watches} onChange={setWatches} />
          ) : (
            <div className="h-40 animate-pulse rounded-2xl bg-[var(--surface-soft)]" />
          )}
          <SourceHealth runs={runs} />
        </aside>
      </main>
    </div>
  );
}

function groupByDay(events: RegulatoryEvent[]): Array<[string, RegulatoryEvent[]]> {
  const groups = new Map<string, RegulatoryEvent[]>();
  for (const event of events) {
    const list = groups.get(event.occurred_on) ?? [];
    list.push(event);
    groups.set(event.occurred_on, list);
  }
  return [...groups.entries()];
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      className={cn(
        "focus-ring h-7 cursor-pointer rounded-lg px-2.5 text-[12px] font-medium transition",
        active
          ? "bg-[var(--primary)] text-white"
          : "bg-[var(--surface-soft)] text-[var(--muted)] hover:text-[var(--foreground)]",
      )}
      onClick={onClick}
      aria-pressed={active}
    >
      {children}
    </button>
  );
}

const DETECTOR_LABELS: Record<string, string> = {
  drugsfda_approvals: "FDA approvals",
  drug_label_changes: "FDA label revisions",
  clinical_trial_changes: "ClinicalTrials.gov",
};

function SourceHealth({ runs }: { runs: MonitorRun[] }) {
  const latest = Object.keys(DETECTOR_LABELS).map((detector) => ({
    detector,
    run: runs.find((run) => run.detector === detector),
  }));
  return (
    <section aria-labelledby="sources-heading" className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
      <h2 id="sources-heading" className="text-sm font-semibold tracking-tight">
        Sources
      </h2>
      <ul className="mt-2.5 space-y-2">
        {latest.map(({ detector, run }) => (
          <li key={detector} className="flex items-start justify-between gap-2 text-[12px]">
            <span>{DETECTOR_LABELS[detector]}</span>
            <span
              className={cn(
                "text-right",
                run?.status === "failed" ? "text-[var(--danger)]" : "text-[var(--muted)]",
              )}
              title={run?.error ?? undefined}
            >
              {!run
                ? "Not checked yet"
                : run.status === "running"
                  ? "Checking…"
                  : run.status === "failed"
                    ? "Last check failed"
                    : `${run.records_scanned.toLocaleString()} scanned · ${formatRelativeDate(run.completed_at ?? run.started_at)}`}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-[11px] leading-relaxed text-[var(--muted)]">
        Label revisions are reported once a label has been seen twice; the first check records a
        baseline. New approvals, trial registrations, and results appear immediately.
      </p>
    </section>
  );
}

function FeedSkeleton() {
  return (
    <div className="space-y-2.5" aria-hidden>
      {[0, 1, 2].map((item) => (
        <div key={item} className="h-36 animate-pulse rounded-2xl bg-[var(--surface-soft)]" />
      ))}
    </div>
  );
}

function EmptyFeed({
  hasRuns,
  checking,
  onCheck,
}: {
  hasRuns: boolean;
  checking: boolean;
  onCheck: () => void;
}) {
  return (
    <div className="rounded-2xl border border-dashed border-[var(--border)] bg-[var(--surface)] px-6 py-12 text-center">
      <p className="text-sm font-semibold">
        {hasRuns ? "No changes match these filters" : "No changes detected yet"}
      </p>
      <p className="mx-auto mt-1.5 max-w-sm text-[13px] text-[var(--muted)]">
        {hasRuns
          ? "Try lowering the significance filter or clearing the search."
          : "Run the first check to scan the last week of FDA approvals, label revisions, and clinical-trial updates."}
      </p>
      {!hasRuns && (
        <Button className="mt-4" size="sm" onClick={onCheck} disabled={checking}>
          {checking ? "Checking…" : "Run the first check"}
        </Button>
      )}
    </div>
  );
}

