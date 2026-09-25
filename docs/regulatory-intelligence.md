# Regulatory intelligence

Formulary watches FDA and ClinicalTrials.gov for changes and routes them to the people who
care: a news feed for everyone, and email or Slack alerts for watched drugs and companies.

```text
openFDA drug/drugsfda ─┐
openFDA drug/label ────┼─► detectors ─► regulatory_events ─┬─► News tab (/news)
ClinicalTrials.gov ────┘       ▲                           ├─► Watches ─► email / Slack
                               │                           └─► Chat (search_regulatory_events)
                        source_snapshots
                        (baselines)
```

## What is detected

| Detector | Source | Event types | Needs a baseline? |
|---|---|---|---|
| `drugsfda_approvals` | openFDA `drug/drugsfda` | new approval, generic approval, new indication, manufacturing change, REMS update, bioequivalence and labeling supplements | No: approvals carry their own date |
| `drug_label_changes` | openFDA `drug/label`, prescription only | label revision (boxed warning, indications, contraindications, warnings, dosage forms, ingredients) | **Yes**: openFDA serves only the current label |
| `clinical_trial_changes` | ClinicalTrials.gov, drug trials in phases 2 to 4 | trial registered, results posted, status change | Only for status changes |

### Significance

Significance ranks how much a regulatory or competitive team would care, not clinical
importance.

- **High**: new drug approval (flagged when it is a new molecular entity), new indication,
  boxed warning or indications revised, results posted for a Phase 3 trial, a Phase 3 trial
  terminated, suspended, or withdrawn.
- **Medium**: generic approval, manufacturing or formulation change, REMS modification,
  contraindication, warning, dosage-form, or ingredient changes, a new Phase 3 trial, results
  for other phases, other trials stopping early.
- **Low**: routine labeling and bioequivalence supplements, trial registrations outside Phase 3,
  other status transitions.

## Design decisions, and the measurements behind them

**Composition is read from `spl_product_data_elements`.** It appears on every prescription
label and lists active and inactive ingredients. The dedicated `active_ingredient` and
`inactive_ingredient` fields are OTC-oriented. Sampled in September 2026 they appeared on 0%
and 2% of prescription labels, so tracking them would have silently missed every formulation
change. Ingredients are diffed as a set of words, so reordering or re-spacing is not reported
as a change, and alerts can say "Added: CROSPOVIDONE · Removed: POVIDONE".

**Identical label revisions are grouped.** When FDA requires a class-wide safety change, every
manufacturer of the drug revises its label at once. Revisions with the same drug and the same
set of changed sections become one event listing every affected label.

**Only prescription labels are scanned.** Between 1 and 24 September 2026, 1,113 labels changed,
398 of them prescription. The OTC majority is mostly repackaging and relabeling.

**Trials are limited to interventional drug studies in phases 2 to 4.** In the week to
23 September 2026 that was 1,357 of the registry's 4,919 updates: the part a regulatory team
acts on, and two requests at the maximum page size.

**Windows overlap, and deduplication makes that free.** openFDA publishes with a lag: an
approval dated the 17th can appear on the 22nd. Each detector re-reads `overlap_days` before
its last successful window (14 days for openFDA, 3 for ClinicalTrials.gov). Every event has a
unique `dedup_key`, so a re-read never duplicates the feed.

## Guarantees

- **Failure isolation.** Detectors run concurrently. One source failing records a failed run
  for that detector only.
- **No lost changes.** A detector's events, its baseline updates, and its run record commit in
  one transaction. Otherwise a baseline could advance without its event, and the next run would
  compare against the new baseline and see nothing.
- **Failed windows are retried.** The next window starts from the last *successful* run.
- **One runner at a time.** A PostgreSQL advisory lock turns a concurrent run (a second API
  process, or a manual trigger during a scheduled one) into a no-op.
- **Exactly-once alerts.** A `watch_deliveries` row is written only after a channel accepts the
  digest. A failed channel is retried next cycle and never blocks another watch.
- **No history floods.** A watch is alerted only to events detected after it was created. Its
  last 30 days are available as a preview instead.

## Watches

A watch follows up to 25 terms (drugs, companies, or conditions), matched as whole words
against the event's drug names, subject, sponsor, and headline. Whole-word matching stops
"insulin" from matching "insulinoma".

Creating a watch captures label baselines for its terms straight away, so the next revision of
a watched drug's label is reported rather than silently becoming its first baseline.

**Slack webhooks are credentials and an SSRF boundary.** Only
`https://hooks.slack.com/services/...` is accepted, because the server POSTs to the URL. The
API never returns the webhook, only a masked hint. Webhooks are stored unencrypted in
`watches.slack_webhook_url`; encrypting them at rest is a known follow-up.

## Running it

```bash
cd apps/api
python -m scripts.run_monitor                    # detect since last run, then notify
python -m scripts.run_monitor --no-notify        # detect only
python -m scripts.run_monitor --since 2026-09-01 # rescan an explicit window
python -m scripts.run_monitor --baseline Ozempic # capture label baselines for a drug
```

Or set `INTELLIGENCE_MONITOR_ENABLED=true` to run cycles inside the API process every
`INTELLIGENCE_POLL_INTERVAL_MINUTES`. The News page's "Check sources now" button triggers a
cycle through `POST /api/v1/intelligence/runs`.

## Known limitations

- **Coverage starts when monitoring starts.** The first run looks back
  `INTELLIGENCE_INITIAL_LOOKBACK_DAYS`. Label revisions are only reported for labels seen at
  least twice.
- **This is regulatory data, not press news.** Company announcements, marketing relaunches,
  and press releases are not in these sources.
- **PubMed has no detector.** New literature is better served by a watch-driven query than a
  global feed; not yet built.
- **Label section excerpts are truncated** to 1,500 characters in baselines, so a long section's
  before-and-after view may not show the changed passage itself. The change is still detected,
  because detection compares full-text hashes.
