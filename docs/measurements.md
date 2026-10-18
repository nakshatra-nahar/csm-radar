# Measurements

Measured on 2026-10-18 on an Apple Silicon laptop (Apple M4, 10 cores, 32 GB RAM, macOS 26.2)
against a local PostgreSQL 16.15. The world is the default one: `radar sim generate --scale 1.0
--seed 7`. The full-world database is `radar_full`, built by `radar bootstrap`. The README's
"what breaks first at 10x" claims come from the last section of this page and from nowhere else.

## Full world, per tenant

Generator counts come from `radar sim generate --scale 1.0 --seed 7` (14.6 s for all three
tenants). A history record is a feed record delivered on or before history end, 2026-10-17
00:00 UTC. Database counts come from `radar_full` after the bootstrap. An account row exists
once its Salesforce record has arrived. Active accounts are the ones the bootstrap sweep scored.

| | hearthline | tallyworks | kitewire |
|---|---|---|---|
| accounts in the world | 2,600 | 2,000 | 400 |
| account rows in the database | 2,555 | 1,970 | 375 |
| active accounts (scored) | 1,769 | 1,070 | 318 |
| active CSMs | 16 | 12 | 3 |
| active accounts per CSM | 110.6 | 89.2 | 106.0 |
| history records | 141,046 | 83,780 | 7,479 |
| feed records in total (history and later) | 172,225 | 102,291 | 13,491 |

History records per source:

| source | hearthline | tallyworks | kitewire |
|---|---|---|---|
| salesforce | 30,667 | 14,459 | 1,338 |
| stripe | 33,120 | 23,185 | 2,436 |
| zendesk | 20,613 | 11,675 | 889 |
| gmail | 15,736 | 9,308 | 802 |
| product | 40,910 | 25,153 | 2,014 |

## Bootstrap

Command: `radar bootstrap` with
`RADAR_OWNER_DSN=postgresql://postgres:postgres@localhost:54329/radar_full`, on an empty
database. An earlier rebuild (no Anthropic key set, before the evidence-query fix) took **374 s**. It applied 7 migrations and
ran 298 jobs. It ingested 140,130 hearthline records, 83,268 tallyworks records and 7,450
kitewire records, and buffered 31,067 / 18,458 / 5,983 for later delivery.

2026-10-18, final rebuild of `radar_full` on the final code (no Anthropic key set): **193.8 s**,
9 migrations, 289 jobs. The brief step is faster since the evidence-query fix.

Per-step times are from the previous rebuild, timed step by step. That rebuild had an Anthropic
key with no credit, so all 280 LLM calls failed (HTTP 400). Each failure took 440 ms on average
(603 ms at most), which added about 123 s to the brief step. Its total was 612.1 s.

| step | hearthline | tallyworks | kitewire |
|---|---|---|---|
| migrate (7 migrations) | 0.1 s (all) | | |
| seed tenants and users | 0.0 s | | |
| replay feed | 147.1 s | 46.1 s | 2.4 s |
| replay rate (accepted records per second) | 953 | 1,806 | 3,103 |
| train and promote | 18.8 s | 12.3 s | no model |
| backfill 12 Monday score runs | 24.4 s | 16.0 s | 3.0 s |
| first worker drain (sweeps, rules, SLA, feed ticks) | 83.4 s (all) | | |
| freeze this week | 0.1 s | 0.0 s | 0.0 s |
| second worker drain (280 briefs) | 256.8 s (all) | | |
| load feed buffer | 0.9 s | 0.5 s | 0.2 s |

The replay rate falls as the tenant grows: hearthline ingests at about half tallyworks' rate.

## Database size and Neon headroom

`pg_database_size('radar_full')` after the bootstrap: **584,449,047 bytes (557 MiB)**. Neon
Free allows 1 GB, so there is about 416 MB (decimal) of headroom. The figures below are
`pg_total_relation_size` (table, indexes and TOAST), in MiB like every size table on this
page. The prose gives decimal MB where it says so.

| table | size | rows |
|---|---|---|
| raw_records | 228 MiB | 230,848 |
| events | 196 MiB | 229,760 |
| account_scores | 55 MiB | 39,980 |
| feed_buffer | 43 MiB | 55,508 |
| quarantine | 8.9 MiB | 25,644 |
| identity_links | 8.4 MiB | 27,405 |
| contacts | 4.8 MiB | 11,232 |
| accounts | 2.4 MiB | 4,900 |
| briefs | 0.9 MiB | 268 |
| audit_log | 0.6 MiB | 860 |
| everything else | under 0.3 MiB each | |

Per tenant (each table's bytes split by its share of rows): hearthline 326.9 MiB, tallyworks
194.8 MiB, kitewire 25.0 MiB.

`account_scores` costs 1,446 bytes per row (57,819,136 bytes / 39,980 rows). One daily sweep
writes a row per active account: 3,157 rows, about 4.4 MiB (4.6 MB) a day for the three tenants.
Without `radar prune`, a year of daily sweeps adds about 1.7 GB. That is more than Neon Free's 1 GB,
so a deployment runs `radar prune` (docs/deploy.md, Ruling AM).

Local WAL reached about 656 MB during the bootstrap (max_wal_size 1 GB). WAL is not part of the
database size, but it does take local disk.

## Band distribution

The bootstrap's actionable sweep run in `radar_full`, `account_scores` grouped by band. ARR is
in major units of the tenant's currency.

| band | hearthline (USD) | tallyworks (GBP) | kitewire (USD) |
|---|---|---|---|
| red | 35 (224,364) | 15 (20,490) | 18 (160,283) |
| yellow | 72 (467,245) | 73 (88,148) | 44 (275,327) |
| green | 522 (3,667,660) | 592 (912,213) | 157 (1,292,188) |
| unknown | 112 (705,125) | 72 (100,964) | 38 (282,458) |
| not_in_window | 1,028 (7,378,130) | 318 (406,718) | 61 (288,007) |

Red is 5.3% of eligible accounts for hearthline (35 of 658), 2.1% for tallyworks (15 of 722)
and 7.1% for kitewire (18 of 254).

The bootstrap froze one list per active CSM for the week of 2026-10-12: 16 for hearthline,
12 for tallyworks and 3 for kitewire. Each has 10 to 12 items, median 10. Model slots filled:
56, 46 and 5, which is every slot the must-acts and check-ins left free. The slot floor
shortened no list.

## Evaluation

Commands: `radar eval --tenant hearthline --out docs` (58 s) and `radar eval --tenant
tallyworks --out docs` (41 s), on `radar_full`. Kitewire has no model. The numbers are from
`eval/hearthline.json` and `eval/tallyworks.json`; the model cards define each metric.
Recall@K is ARR-weighted, computed each week across every CSM's book of up to K = 10 and
averaged over test weeks. The 95% intervals are a paired cluster bootstrap over accounts with
1,000 resamples.

| policy | hearthline recall@K | 95% CI | tallyworks recall@K | 95% CI |
|---|---|---|---|---|
| random | 26.1% | 25.3 to 26.8 | 19.5% | 19.0 to 20.1 |
| renewal-date sort | 14.7% | 10.7 to 18.7 | 18.4% | 15.0 to 21.7 |
| ARR sort | 42.1% | 32.6 to 50.9 | 50.7% | 38.1 to 60.5 |
| rules heuristic | 53.5% | 44.9 to 61.0 | 49.7% | 39.8 to 56.9 |
| **model** | **61.6%** | **53.1 to 68.1** | **59.6%** | **49.8 to 67.9** |
| oracle (latent state) | 77.7% | 71.2 to 82.9 | 67.9% | 59.3 to 74.3 |
| GBM (diagnostic) | 48.8% | 41.6 to 56.1 | 38.1% | 29.7 to 46.3 |

| | hearthline | tallyworks |
|---|---|---|
| model minus renewal sort | +46.8 points (+37.2 to +55.3) | +41.1 points (+30.5 to +49.5) |
| model minus rules | +8.1 points (+3.2 to +13.1) | +9.8 points (+2.1 to +18.2) |
| model precision@K | 19.7% | 13.9% |
| model ROC-AUC (point in time) | 0.767 | 0.665 |
| hindsight ROC-AUC | 0.768 | 0.666 |
| naive current-state ROC-AUC (leakage row) | 0.981 | 0.968 |
| lead time: decisions flagged (within the 90-day window) | 124 of 190 | 65 of 174 |
| lead time: median weeks | 11.4 | 11.2 |
| unforecastable share of voluntary churn | 17.7% | 10.8% |

`acceptance()` passes for both tenants. Oracle AUC is 0.848 (hearthline) and 0.843
(tallyworks). The best single feature is seat_utilization: AUC 0.717 and 0.695.

Lead time under the earlier floor on p (Ruling AP, superseded by the key floor of Ruling AZ):
tallyworks flagged **90 of 174** churn decisions ahead (median 10.8 weeks), against 70 of 174
under the key floor, both counted before the lead-time fix (any earlier flag counted, not
only flags inside the decision's 90-day window) and before training waited for settled
labels. Source: `eval/tallyworks.json` `lead_time` at commit f517b6c, read with
`git show f517b6c:eval/tallyworks.json`. The key floor lists large accounts over small high-p
ones, so it flags fewer, larger decisions; recall@K, which is ARR-weighted, went up (42.6% to
59.1%).

## Quarantine and identity

`radar_full` after the bootstrap. A record goes to quarantine when no identity link maps it to
an account. It is resolved when a later link maps its reference (`resolved_at` is set).
**25,644 rows: 14,995 resolved, 10,649 unresolved.**

| source | reason | rows | resolved | unresolved |
|---|---|---|---|---|
| stripe | unknown_external_id | 11,269 | 10,746 | 523 |
| stripe | no_key | 527 | 527 | 0 |
| stripe | free_mail_domain | 63 | 12 | 51 |
| product | no_key | 6,982 | 0 | 6,982 |
| product | unknown_external_id | 1,069 | 612 | 457 |
| product | future_timestamp | 5 | 0 | 5 |
| zendesk | no_org | 2,631 | 0 | 2,631 |
| zendesk | unknown_external_id | 2,190 | 2,190 | 0 |
| zendesk | no_key | 896 | 896 | 0 |
| gmail | no_key | 12 | 12 | 0 |

Per tenant: hearthline 15,454 rows, tallyworks 8,632, kitewire 1,558.

- **Stripe unknown_external_id (11,269; 523 unresolved).** The customer's metadata names a
  Salesforce account that had not arrived yet. 10,746 resolved when it did: 9,768 by
  `stripe_metadata` and 978 by `domain`, a median of 0.5 days after the Stripe record. So
  out-of-order arrival is handled. The 523 left come from 48 Stripe customers whose customer
  record did arrive: 46 carry no Salesforce id in their metadata and matched no domain, and 2
  name a Salesforce account that is still in the feed buffer (it arrives after bootstrap).
- **Product no_key (6,982, none resolved).** These usage summaries come from 385 workspaces
  whose records always carry `"sf_account_id": null`. None of those 385 workspaces has an
  identity link, and none of their product records ever carries a key. Matching them by exact
  key cannot work, by design. Their accounts show no telemetry; Radar does not guess a match.
- **Zendesk no_org (2,631, none resolved).** These tickets have no organisation, so there is
  nothing to link.
- **Product unknown_external_id (1,069; 457 unresolved).** The workspace names an account
  not seen yet. 612 resolved by `product_metadata`, a median of 12.9 days later.
- Every resolved row has its `resolved_link_id` set.

Identity links by method, from the final rebuild (27,600 in all; the quarantine figures above
are from the previous rebuild, and the final one resolved 6 more Stripe rows: 15,001 resolved):

| source | method | links |
|---|---|---|
| salesforce | sf_id | 4,902 |
| salesforce | sf_opportunity | 6,055 |
| salesforce | merge | 33 |
| gmail | contact_email | 5,041 |
| stripe | stripe_metadata | 4,340 |
| stripe | domain | 658 |
| product | product_metadata | 3,216 |
| zendesk | zendesk_external_id | 2,323 |
| zendesk | domain | 1,032 |

The model cards score these links against the generator's identity truth. Precision is 1.000
for every method, except tallyworks `sf_id` at 0.999. A Stripe ref first linked by domain gets an
appended `stripe_metadata` link once its customer record names the account (commit 40481cd).
The domain row stays, so this table counts both, while the model cards count each ref's
current link: hearthline 241 domain and 2,269 stripe_metadata, tallyworks 184 and 1,744.

## Daily sweep duration

The job lease is 300 s. Every sweep took under 4 s.

In the soak (next section), 21 sweeps ran at full scale. Wall time per sweep, in local-day
order:

| tenant | sweeps | seconds |
|---|---|---|
| hearthline | 7 | 2.9, 2.8, 3.5, 3.5, 3.6, 3.6, 3.8 |
| tallyworks | 7 | 1.8, 2.0, 2.2, 2.3, 2.3, 2.3, 2.3 |
| kitewire | 7 | 0.3, 0.3, 0.5, 0.5, 0.4, 0.4, 0.4 |

From about the second simulated day on, the 10x generator and the tiling script (see 10x
below) ran on the same machine. That likely explains the later, slower sweeps.

So each sweep was run once more, on its own, after the soak and on the same copy. The clock
was at Monday 2026-10-26 12:00 UTC, so hearthline and tallyworks froze their lists. Command:
`uv run python scripts/measure_10x.py sweep --dsn <the copy>`, which calls
`handlers.daily_sweep` and times its steps:

| tenant | total | score_tenant | rule facts | the rest (cases, freeze, digests, freshness) |
|---|---|---|---|---|
| hearthline | 3.01 s | 1.82 s | 1.10 s | 0.09 s |
| tallyworks | 1.73 s | 1.07 s | 0.60 s | 0.06 s |
| kitewire | 0.32 s | 0.23 s | 0.08 s | 0.01 s |

Both `score_tenant` and the rule facts load the tenant's whole event history (`load_frames`,
1.06 s for hearthline), so a sweep loads it twice.

## Unattended soak

Command, on `radar_soak` (a copy: `CREATE DATABASE radar_soak TEMPLATE radar_full STRATEGY
FILE_COPY`), without an Anthropic key, so every brief is a template:

    RADAR_WORKER_PASSWORD=radar_worker_test uv run python scripts/soak.py \
        --dsn postgresql://postgres:postgres@localhost:54329/radar_soak

The script freezes `radar.clock` at the bootstrap's last finished job, 2026-10-18 07:50:42 UTC
(a Sunday). It then advances the clock in 672 steps of 15 minutes, to 2026-10-25 07:50:42 UTC.
At each step it calls `worker.run_for` with the 240 s tick budget, as the GitHub Actions schedule
calls `/internal/tick`. The window crosses Monday 2026-10-19 and the end of British Summer Time
(2026-10-25 01:00 UTC). **Wall time: 519.8 s for 7 simulated days.** Peak RSS was 1.07 GiB.
`radar_soak` was dropped afterwards.

Feed records delivered (new `raw_records` rows):

| source | hearthline | tallyworks | kitewire |
|---|---|---|---|
| salesforce | 226 | 82 | 55 |
| stripe | 217 | 164 | 85 |
| zendesk | 113 | 62 | 34 |
| gmail | 205 | 117 | 31 |
| product | 15 | 13 | 10 |
| total | 776 | 438 | 215 |

Jobs (all done; **0 dead** in the whole jobs table):

| job | count | handler time, total | mean | max |
|---|---|---|---|---|
| feed_tick | 2,016 (one per tenant per step) | 4.5 s | 0.002 s | 0.1 s |
| sla_check | 504 (one per tenant per hour) | 0.3 s | 0.001 s | 0.0 s |
| evaluate_rules | 262 | 250.2 s | 0.95 s | 1.5 s |
| daily_sweep | 21 | 41.8 s | 2.0 s | 3.8 s |
| generate_brief | 135 | 218.2 s | 1.62 s | 3.2 s |

- **Daily sweeps: exactly one per tenant per local day.** Hearthline and kitewire swept
  2026-10-18 to 2026-10-24 (New York and Los Angeles; the window ends before 06:00 local on the
  25th). Tallyworks swept 2026-10-19 to 2026-10-25 (London; its 18th had been swept by the
  bootstrap, and the 25th, the day the clocks went back, swept once). The bootstrap's own sweep
  ran at 03:47 New York and 00:47 Los Angeles time on the 18th, before the 06:00 sweep hour, so
  those two tenants had the bootstrap sweep and the scheduled sweep that day. Since Ruling BC
  the bootstrap sweep counts as that local day's sweep, so a new bootstrap sweeps once.
- **Monday freeze:** on 2026-10-19 each tenant froze one list per active CSM for that week: 16
  (hearthline), 12 (tallyworks), 3 (kitewire). No other week froze.
- **SLA escalations:** 148 (hearthline), 114 (tallyworks), 31 (kitewire). Nobody worked the
  cases, so the bootstrap backlog and the new cases went past their SLA.
- **source_stale incidents:** 4 opened, 2 closed, all Gmail. Tallyworks opened at 2026-10-19
  03:05 and closed at 09:05, then opened again at 2026-10-24 19:05. Kitewire opened at
  2026-10-19 07:05 and closed at 18:05, then opened again at 2026-10-25 00:05. Each opened at the
  first hourly check after its `expected_by` (last delivery + 24 h), 4 to 50 minutes
  later. These are real gaps in the generated Gmail feed: no mail over the weekends. The two
  later incidents were still open at the end, because Gmail resumes on Monday.
- **score_runs left running: 0.**
- **Briefs generated:** 67 (hearthline), 58 (tallyworks), 5 (kitewire). 135 brief jobs made 130
  new briefs.
- **DB growth:** 584.4 MB → 622.3 MB (`pg_database_size`, decimal MB), +37.8 MB in 7 days.
  Per simulated day: +5.3, +6.4, +5.6, +5.1, +5.3, +5.3, +5.0 MB. About 4.6 MB a day is the
  sweep's `account_scores` rows. At this rate, without pruning, the database reaches Neon Free's
  1 GB about 77 days after bootstrap.

## 10x

These are measured at 1x and on tiled 10x frames; DB-level 10x numbers are projected from 1x.

Measured: the generated 10x world's counts, and feature build, scoring and RAM on 10x
frames tiled from hearthline's 1x frames (not the generated 10x world). Projected from 1x:
the 10x database size, `load_frames` time and RAM, bootstrap replay time and Postgres plan
quality. No 10x world was loaded into Postgres. This laptop had 3.0 GiB of disk free, and a
loaded 10x world needs about 5.7 GB of database plus about 1.8 GB of feed files (10 × the
180 MB `data/` at 1x). Hearthline alone needs about 3.2 GB of database.

### World size

Command: `uv run python scripts/measure_10x.py world <slug>`, one process per tenant. It calls
`generate(out_dir, seed=7, scale=10, tenants=[slug], count_only=True)` and writes nothing.
Before a fix in this task, hearthline at 10x never finished: it needs 26,000 company names and
its surname × noun × place space holds 24,480, so the name picker retried forever. Names now
take a number once 100 draws in a row collide; the 1x world is byte-identical.

| | hearthline | tallyworks | kitewire |
|---|---|---|---|
| accounts | 26,000 | 20,000 | 4,000 |
| history records | 1,379,646 (9.8x) | 865,783 (10.3x) | 73,272 (9.8x) |
| all feed records | 1,694,863 | 1,059,049 | 132,085 |
| generator wall time | 68.7 s | 53.7 s | 5.1 s |
| generator peak RSS | 3,826 MiB | 2,306 MiB | 482 MiB |

### Feature build and scoring for hearthline, in memory

Command: `uv run python scripts/measure_10x.py tile`. It loads hearthline's Frames from
`radar_full` with `load_frames`, then tiles them 10 times. Copy k gets new ids: event and link
ids plus k × 10⁹, account and contact uuids `uuid5(id:k)`, and an `xk.` inserted after the
prefix of every key string (`sf:x3.001…`, `email:x3.ann@…`, `stripe_sub:x3.sub_…`), so copies
never join to each other. Users are not tiled, so each book is 10 times larger. Then time
`compute_features` and `scoring.score_frame` for one as-of, 2026-10-18 07:50:42 UTC, with the
active model, and `rule_facts` (the must-act rules). Check: copy 3 of every account has exactly
the 1x features.

| | 1x | 10x |
|---|---|---|
| events / identity links / accounts | 139,487 / 16,240 / 2,555 | 1,394,870 / 162,400 / 25,550 |
| active accounts scored (eligible) | 1,769 (658) | 17,690 (6,580) |
| `compute_features`, one as-of | 0.33 s | 2.91 s |
| `score_frame` (features, model, bands, ranks) | 0.31 s | 2.89 s |
| `rule_facts` | 0.07 s | 0.62 s |
| peak RSS of the process | 783 MiB | 2,172 MiB (1x and 10x frames both held) |

Feature build and scoring scale linearly and stay under 3 s. The model, bands and ranks add
almost nothing to the feature build.

What does not scale is the load. `load_frames` builds a list of dict rows before the
DataFrame. Measured at 1x in a fresh process (baseline RSS 99 MiB, with radar.features and
radar.scoring imported):

| tenant | events | load time | peak RSS | per event | events DataFrame |
|---|---|---|---|---|---|
| hearthline | 139,487 | 1.06 s | 780 MiB | 5.0 KB | 85 MiB |
| tallyworks | 82,854 | 0.61 s | 512 MiB | 5.1 KB | 49 MiB |
| kitewire | 7,419 | 0.07 s | 145 MiB | 6.4 KB | 5 MiB |

At 5.0 KB per event, a 10x hearthline (1.39 M events) would peak near 6,900 MiB in `load_frames`
and take about 10.6 s (linear: 7.6 µs per event at 1x). The DataFrame it keeps is only
0.6 KB per event.

### Hot queries as the app roles (1x, `radar_full`)

Command: `uv run python scripts/measure_10x.py explain`. It logs in as the app's role, sets
`app.tenant_id` to hearthline, and runs the app's own function, so row-level security applies
as in the app. It records the first SQL statement the function runs and runs
`EXPLAIN (ANALYZE, BUFFERS)` on it twice. Cold is the first run after other work; warm is the
second. The brief row is from after Ruling BB (migration 008). The other rows are from before
it; a re-run after it gives the same warm times (50.6, 0.5 and 10.1 ms).

| query | role | plan | rows | cold | warm |
|---|---|---|---|---|---|
| as-of event load (`asof.load_frames` events query) | radar_worker | parallel seq scan of `events` (2 workers), RLS as a one-time filter | 139,487 | 986 ms (15,871 buffers read) | 53 ms |
| /week list (`queries.worklist_rows`) | radar_web | index scans on worklists, accounts, account_scores; seq scans on cases (280 rows), outbox (309) and, per item, worklist_items (315) | 10 | 8.5 ms | 0.6 ms |
| account evidence timeline (`queries.TIMELINE`, 40 rows) | radar_web | seq scan, sort and unique of the tenant's 16,240 identity links, then `events_account_ref_idx` per ref | 40 | 20 ms | 10.9 ms |
| brief evidence (`evidence.FEATURE_EVENTS`) | radar_worker | seq scan of `identity_links` for the account's refs (3), then `identity_links_ref_idx` and `events_account_ref_idx` per ref | 17 | 1.1 ms | 1.1 ms (610 buffer hits) |

Before Ruling BB the brief query read every shown-type event of the tenant (81,834). For each
one it looked up the ref's latest link in `identity_links_lookup_idx`. That index leads with
`source`, which the lookup does not know, so each lookup walked the tenant's part of the index.
One hearthline brief took 3,732 ms and 2,159,845 buffer hits.

Now the query starts from the account's own refs, as the timeline does. Migration 008 adds
`identity_links_ref_idx (tenant_id, account_ref, available_at DESC, id DESC)` for both link
lookups. The pack is the same: tests/test_brief.py builds 20 hearthline packs on the small
world with the old query and the new one and compares them.

Wall time: `uv run python scripts/measure_10x.py briefs`, warm, 3 model-slot accounts per
tenant. The evidence query takes 1 to 2 ms in every tenant. Before Ruling BB it took 0.04 to
0.11 s (kitewire), 1.56 to 2.44 s (tallyworks) and 3.75 to 3.81 s (hearthline). The whole
pack (`build_pack`, 3 model_topk cases per tenant) takes 2 to 4 ms. The first run after other
work took up to 40 ms for the query and 50 ms for the pack.

One part still grows with the world: the seq scan that finds the account's refs. It reads the
whole `identity_links` table (27,405 rows for all tenants; RLS drops the others). At 10x that
is about 274,000 rows, so about 10 ms (projected, linear).

A 2-day soak re-run on a copy of `radar_full` after the fix (same script, `--days 2`) ran 113
brief jobs in 0.7 s in all, 6 ms each on average. The 7-day soak's brief jobs, before the fix,
averaged 1.62 s.

### Database size at 10x

Each tenant's measured size (Database size above) is scaled by its 10x history-record ratio.
This is a projection, because no 10x world was loaded into Postgres:

| tenant | 1x | ratio | 10x |
|---|---|---|---|
| hearthline | 326.9 MiB | 9.78 | 3,198 MiB |
| tallyworks | 194.8 MiB | 10.33 | 2,013 MiB |
| kitewire | 25.0 MiB | 9.80 | 245 MiB |
| total | 546.7 MiB | | 5,456 MiB (5.7 GB) |

That is 5.7 times Neon Free's 1 GB. The 1x database (584.4 MB) already uses 58% of it.

### What breaks first at 10x

From the measurements above, in the order they break as the world grows.

1. **Neon Free storage (1 GB) breaks at about 1.7x.** The bootstrapped database is 584.4 MB at
   1x and grows with records, so it passes 1 GB at 1.71x and reaches about 5.7 GB at 10x. At 1x
   it already fills 1 GB about 77 days after bootstrap if nobody prunes (5.4 MB a day, 4.6 MB of
   it score rows). Fix: run `radar prune` on a schedule; drop `feed_buffer` (43 MiB, demo only)
   once the feed is delivered; at 10x, move to a paid tier and partition `raw_records` and
   `events` by month, keeping old months in cold storage.
2. **Worker memory on Vercel breaks at about 2.9x hearthline.** A function gets 2 GB on Hobby
   and at most 4 GB on Pro (Vercel Functions limits,
   https://vercel.com/docs/functions/limitations).
   Every sweep, score run and `evaluate_rules` job loads the tenant's whole history with
   `load_frames`, which peaks at 5.0 KB per event over a 99 MiB baseline. That reaches 2 GB at
   about 400,000 events in one tenant (2.9x hearthline). A 10x hearthline would need about
   6,900 MiB. Fix: read the events straight into columns (tuple rows or COPY into the DataFrame;
   the frame itself is 0.6 KB per event), select only rows visible at T, and scope
   `evaluate_rules` to the one account it checks (its `shortcut:` comment already says so).
   Even then it is close: in the tiled run, the 10x frames, features and scores added 1,389 MiB
   over the 1x load.
3. **Brief generation no longer grows with the world (Ruling BB).** Jobs run inside
   `/internal/tick`, a Vercel function with `maxDuration` 300 s (vercel.json).
   `worker.run_for` checks its 240 s tick budget only between ticks, and a tick runs up to 20
   jobs. So a job that starts late in a tick gets at most 300 − 240 = 60 s before Vercel kills
   the function, and later jobs in that tick get less. A killed job keeps its lease, is reaped
   and retried, and goes dead if it keeps landing late.
   Before Ruling BB the brief evidence query took 3.75 s per hearthline brief at 1x and grew as
   size^1.5 to size^1.7, so it would have passed 60 s at 5.1x to 6.3x. It now reads only the
   account's refs: 1 to 2 ms at 1x, about 10 ms at 10x (projected). A template brief job took
   6 ms on average in the soak re-run. With a key, the LLM call dominates. Its time does not
   grow with the world, but it is not measured (the key had no credit). Both attempts (up to
   4,096 output tokens each) must fit in the time a late job has left.
   Fix: pass the tick's deadline from `run_for` down to `tick()`, which runs the jobs, and do not
   start a job whose expected time (its kind's recent p95) exceeds the time left.
4. **Rule checks cost about 7 hours a week at 10x; with briefs, the worker uses about 15% of
   its weekly budget.** At 1x: 262 checks a week, 0.95 s each, almost all of it the
   whole-tenant load. At 10x: about 10 times the checks, each about 10 times longer, so about
   2,620 × 9.5 s, or 6.9 hours a week. Briefs at 10x are about 1,350 jobs a week (10 × the
   soak's 135). As templates (6 ms at 1x plus the 10 ms link scan) they take about 20 s a week.
   Together: about 6.9 of the 44.8 hours the 15-minute ticks give each week (672 ticks ×
   240 s). The rest, 37.9 hours, covers LLM briefs of up to 101 s each (37.9 h / 1,350). That
   is more than the 60 s a late job gets, so the late-job window in item 3 binds first. Each
   rule check also hits the memory limit in item 2. The fix is the same as item 2.
5. **The daily sweep does not break.** It takes 3.0 s for hearthline at 1x. Projected to 10x:
   two whole-tenant loads (2 × 10.6 s), features and scoring (2.9 s, measured), rules (0.6 s,
   measured) and the other sweep work scaled linearly to about 5 s. The other sweep work is
   0.51 s at 1x: the 3.01 s total minus the two loads, `score_frame` and `rule_facts`. That is
   about 30 s in all, well under the 60 s a late job gets (item 3). It does need the memory fix
   in item 2.
6. **Page queries slow down linearly but stay fast.** The timeline sorts all of the tenant's
   identity links on every account page: 16,240 at 1x (10.9 ms warm), 162,400 at 10x. The /week
   list seq-scans `worklist_items` once per item; for hearthline at 1x that table grows by
   about 160 rows a week.
   Fix: start the timeline from the account's refs, as the brief query does since Ruling BB,
   so it uses `identity_links_ref_idx` (migration 008). Add indexes on
   `worklist_items (tenant_id, account_id)` and `cases (tenant_id, account_id)`.
7. **Bootstrap replay gets slower per record as a tenant grows.** It runs at 953 records/s for
   hearthline and 1,806/s for tallyworks. At 10x hearthline's 1.38 M history records would take at
   least 1,450 s at the 1x rate. The bootstrap runs on a laptop and is then pushed (`radar push`),
   so this is a wait, not an outage. Fix: bulk-load raw records with COPY.

Not measured: LLM spend during a correlated shock. The API key had no credit, so every brief
was a template. The soak ran 135 brief jobs in a week at 1x. With a key, each calls the LLM (one
attempt, at most one retry) unless the tenant's `llm.daily_token_budget` (200,000 tokens) is
spent, after which briefs fall back to the template. So spend in a shock is capped per tenant
per day by config, and a shock cannot raise it.

Upper bound in dollars. The budget counts every token (input, cache and output), and output is
the dearest rate, so the bound prices every token as output. The check runs once per brief,
before its two calls, so the last brief can pass the cap by two calls. A call is at most about
16,000 tokens: the largest stored pack (3,359 bytes over 280 briefs in `radar_full`), the
system prompt, schema, tool and retry instruction (about 5.6 KB), counted as one token per
byte, plus 4,096 output tokens (`max_tokens`). At most two workers run at once (the 15-minute
GitHub Actions tick and Vercel's daily cron), so a tenant's day is at most 200,000 + 2 × 2 ×
16,000 = 264,000 tokens:

| model | per tenant per day | 3 tenants per day | 3 tenants per 30 days |
|---|---|---|---|
| claude-haiku-5-5 (default, $0.50 per M output) | $0.13 | $0.40 | $11.88 |
| claude-sonnet-5-5 ($10 per M output) | $2.64 | $7.92 | $237.60 |
