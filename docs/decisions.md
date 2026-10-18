# Decision log

The product and engineering decisions behind Radar, grouped by area. Each entry gives the
context, the decision, the alternatives we considered, why we chose it, what it costs if it
is wrong, and where it lives in the code. "Ruling X" refers to a decision recorded during the
build; "design N" to a section of [design.md](design.md). Process decisions about how the
build was run are left out.

- [Data and identity](#data-and-identity)
- [Features and labels](#features-and-labels)
- [Model and evaluation](#model-and-evaluation)
- [Policy and worklist](#policy-and-worklist)
- [Agent and LLM](#agent-and-llm)
- [Multi-tenancy and security](#multi-tenancy-and-security)
- [Deployment](#deployment)

## Data and identity

### Two clocks on every record

- **Context.** Sources arrive late: Salesforce a day late, product usage up to 9 days late,
  Stripe retries for up to 3 days. A model trained on what happened, rather than on what was
  known, learns from data it will not have when it scores.
- **Decision.** Every record carries `occurred_at` and `available_at`. A record is visible at
  T only if both are <= T. `available_at` is the feed's delivery time in replay and the
  server's arrival time on the webhook path, never a client value. Identity links carry their
  own `available_at`. `ingested_at` is audit only.
- **Alternatives.** One event-time column; a bitemporal model with validity ranges.
- **Why.** One rule, used by training, scoring, evidence and the trace, is easy to test
  (truncation equivalence) and easy to explain.
- **Cost if wrong.** If delivery times were simulated badly, training would still be
  optimistic. The hindsight row in each model card measures the gap (+0.001 AUC here).
- **Code.** `radar/asof.py` (`visible`, `latest`, `attribute`); `tests/test_point_in_time.py`.

### An immutable raw ledger and append-only events

- **Context.** Vendors re-send, re-deliver and restate. A trace must still show what the
  system saw.
- **Decision.** `raw_records` keeps one row per distinct source record, unique on
  `(tenant, source, source_record_id)`. A re-send with the same hash counts as a duplicate;
  the same id with a different body goes to quarantine as `conflicting_resend`. `events` are
  normalised versions, append-only. The app roles have only INSERT and SELECT on both.
- **Alternatives.** Upsert current state per entity; keep only canonical rows.
- **Why.** Replays are idempotent, any score can be recomputed from stored events, and
  re-attribution never rewrites history because events never store an account id.
- **Cost if wrong.** Storage: raw and events are 424 MiB of the 557 MiB database at 1x.
- **Code.** `radar/ingest/pipeline.py`, `radar/migrations/001_core.sql`,
  `radar/migrations/004_security.sql`.

### Exact-key identity only, with a reasoned quarantine

- **Context.** Every source names the customer differently: Stripe metadata, Zendesk
  organisation external ids, product workspace ids, email addresses.
- **Decision.** Match in a fixed order: an explicit external id (normalised: trim, 15 to 18
  characters, `001` prefix check), then a contact's exact email, then a corporate domain that
  maps to exactly one non-test account and is not free mail or a franchise. Anything else goes
  to quarantine with a reason. A new Salesforce account or contact retries the quarantine.
- **Alternatives.** Fuzzy name matching; matching by domain first.
- **Why.** A wrong match can put customer B's ticket in customer A's email. A missing match is
  visible on the ops page and fixable. Measured precision is 1.000 for every method except
  tallyworks `sf_id` at 0.999.
- **Cost if wrong.** Recall. About 11% of records sit in quarantine at bootstrap, most by
  design (385 workspaces never send a Salesforce id; tickets with no organisation).
- **Code.** `radar/ingest/identity.py`; `tests/test_identity.py`.

### Terminal states win out-of-order ties

- **Context.** Stripe does not guarantee delivery order, and its `created` is in seconds.
- **Decision.** A subscription version key is `<created>:<state rank>:<evt id>`, with
  canceled above past_due above active, so a late "active" cannot resurrect a cancelled
  subscription. Semantic twins (new event id, same state) are dropped and counted.
- **Alternatives.** Order by arrival; order by `created` alone.
- **Why.** The final state must not depend on delivery order.
- **Cost if wrong.** A genuine reactivation in the same second as a cancel would read as
  cancelled until the next event.
- **Code.** `radar/ingest/normalize.py`; `tests/test_ingest.py::test_out_of_order_subscription_terminal_state_wins`.

### Product completeness watermark counts last month's reporters (Ruling T)

- **Context.** Usage arrives as a monthly batch, sometimes late. A partial month reads as a
  usage drop.
- **Decision.** A month is complete when at least 90% of the workspaces that reported the
  previous month have reported. Features read only complete months.
- **Alternatives.** All linked workspaces as the denominator; a fixed day of the month.
- **Why.** Churned accounts' workspaces stay linked but never report again; counting them
  would freeze the watermark as churn accumulates.
- **Cost if wrong.** A month can count as complete while a few live workspaces are missing,
  bounded by the 90% threshold.
- **Code.** `radar/features.py` (`watermarks`);
  `tests/test_features.py::test_product_watermark_ignores_churned_workspaces_but_waits_for_a_late_batch`.

## Features and labels

### The label is a churn decision within 90 days

- **Context.** A subscription ends weeks after the customer decides. By then nothing can
  change the outcome.
- **Decision.** `churn_decision` is the earliest of a Stripe cancellation announced and not
  reversed, the first Closed Lost row on a renewal opportunity, a ticket tagged
  `cancellation_request`, or a subscription ending with no notice. The label is 1 if a
  decision falls in (T, T+90d]. A snapshot is excluded if its decision is already visible, if
  the window holds an involuntary (payment failure) churn or a merge, or if its label has not
  matured (T + 90 + 30 + 7 days past the cutoff). Payment-failure churn is not in the label;
  a rule owns it.
- **Alternatives.** Subscription end; Salesforce `Type` turning to Former Customer; a
  survival model.
- **Why.** 90 days is a window in which a CSM touch can still matter, and labels mature fast
  enough to evaluate every quarter. Each weekly snapshot is a landmark with a fixed horizon,
  which handles time-varying inputs without survival machinery.
- **Cost if wrong.** Annual accounts more than 90 days from their decision score low. They
  still show their band and drivers on the Book page.
- **Code.** `radar/labels.py`; `tests/test_labels.py`.

### Inputs that would leak, or measure ourselves, are banned

- **Context.** Some fields are written after the decision or are under our own control.
- **Decision.** The registry lists banned inputs with a reason: renewal opportunity stage,
  forecast category, close date and loss reason (human judgement, or written after the
  outcome); Salesforce `Type` and `ARR__c` as current state (post-churn cleanup leaks the
  label); the CSM's own outbound email recency (the model would learn our own policy); owner
  changes; anything from an LLM.
- **Alternatives.** Use them and rely on regularisation.
- **Why.** The naive current-state build in each model card shows what they buy: AUC 0.981
  against 0.767 for hearthline, all of it leakage.
- **Cost if wrong.** A small amount of real signal is left on the table.
- **Code.** `radar/registry.py`; `tests/test_features.py::test_banned_inputs_not_in_features`.

### One registry of definitions, with a code hash per feature

- **Context.** Features, metrics and rules get re-implemented in the UI, the evaluation and
  the policy, and drift apart.
- **Decision.** Every definition exists once in `radar/registry.py` with a version, a source,
  a risk direction and a one-line meaning. A hash of each implementation (including the
  helpers and constants it reaches) is computed at import. A model stores its manifest; the
  scorer refuses to score when the manifest no longer matches, raises one
  `manifest_mismatch` incident, and the rules keep running. The data dictionary is generated
  from the registry.
- **Alternatives.** Version numbers bumped by hand; a feature store.
- **Why.** A model scored on changed feature code gives wrong numbers silently. Failing
  closed is visible.
- **Cost if wrong.** A harmless refactor of a feature function blocks scoring until a model
  is retrained.
- **Code.** `radar/registry.py`, `radar/scoring.py`;
  `tests/test_scoring.py::test_manifest_mismatch_fails_closed`;
  `tests/test_web.py::test_promotion_refuses_a_candidate_built_on_other_feature_code`.

### Usage features are hinged and tenant-relative (Task 5b)

- **Context.** In the first build, the usage level and the usage trend were collinear and one
  of them took the wrong sign at full scale.
- **Decision.** `usage_drop` v2 is max(0, -d), where d is the log change of active users
  against the prior three complete months, minus the tenant median of the same; growth reads
  0. `seat_utilization` v2 averages three complete months. Seasonality and tenant-wide
  outages therefore do not read as account risk.
- **Alternatives.** One usage feature; a per-seat redefinition of seat change (Ruling W
  declined it).
- **Why.** Every behavioural coefficient must match its risk direction to pass the promotion
  gate, and both tenants now do.
- **Cost if wrong.** Seat changes carry weak signal (AUC 0.55 alone); some seat-cut risk
  shows as a usage drop.
- **Code.** `radar/features.py` (`usage_drop`, `seat_utilization`);
  `tests/test_features.py::test_usage_drop_is_a_tenant_relative_hinge_on_complete_months`.

## Model and evaluation

### One logistic regression per tenant, stored as JSON

- **Context.** A few hundred churn decisions per tenant (340 in hearthline's final fit), and
  every number a CSM sees must trace to the computation.
- **Decision.** Standardised features, L2 with C = 1.0 fixed (a sensitivity line, no tuning
  loop). The artifact is JSON: features, hashes, imputation values, means, scales,
  coefficients, intercept and an optional Platt pair. Serving is a numpy dot product.
  Contributions are `coef_i * z_i`, and `intercept + sum = logit(p)` holds on every stored
  score.
- **Alternatives.** Gradient boosting with SHAP; a pooled model across tenants; a pickle.
- **Why.** At this size LR is stable, its contributions are exact (no approximation to
  defend), the JSON diffs in review and loads without executing code. Gradient boosting
  stays as a diagnostic row: it scored below LR here (48.8% against 61.6% recall@K for
  hearthline).
- **Cost if wrong.** Interactions are missed. The oracle's 77.7% shows the room left, most of
  it from data the model cannot see.
- **Code.** `radar/model.py`; `tests/test_model.py::test_json_predict_matches_sklearn`;
  `tests/test_model.py::test_contributions_sum_to_logit`.

### Per-tenant models; a new tenant runs on rules

- **Context.** Training on one customer's data to score another's is a data-use question for
  the contract, not an engineering default.
- **Decision.** Each tenant gets its own model. A tenant trains only if its spec sets
  `has_model` (`radar/tenants.py`); the `min_decisions` gate (100 matured decisions,
  `radar/bootstrap.py`) then applies. Kitewire, onboarded in 2026, is off by config
  (`has_model=False`), so it has no model at any decision count: its slots are ranked by
  `rules_score x ARR`, and its pages say so. Turning it on is a deliberate step.
- **Alternatives.** One pooled model; transfer from the mature tenants.
- **Why.** No cross-tenant data use, and the rules were measured on the mature tenants
  (53.5% and 49.7% recall@K).
- **Cost if wrong.** A new tenant waits months for a model.
- **Code.** `radar/tenants.py` (`has_model`), `radar/registry.py` (`rules_score`);
  `tests/test_scoring.py::test_rules_only_tenant_scores`.

### Rolling-origin evaluation against the policies CSMs would otherwise use

- **Context.** AUC does not say whether a CSM's ten slots go to the right accounts.
- **Decision.** Monday snapshots; 4 origins 8 weeks apart; each trains on snapshots whose
  labels had settled by the origin (T + 127 days), fitted as training deploys the model
  (Platt step included), and tests on the next 8 weeks. Headline: ARR-weighted recall@K
  per CSM book per week, averaged. Every policy (random, renewal-date sort, ARR sort, rules,
  model, oracle, GBM) is scored on the same rows with the same slot floor. Paired cluster
  bootstrap CIs over accounts. Plus recall by churn cause, hindsight and naive leakage rows,
  source ablation, the VP cases' weekly rank, and the outage week.
- **Alternatives.** A random train/test split; AUC alone; precision at a threshold.
- **Why.** It measures the list a CSM gets, against the spreadsheet sort.
- **Cost if wrong.** Four origins give wide intervals (about ±8 points on the model).
- **Code.** `radar/evaluate.py`; `tests/test_evaluate.py`; [model card](model-card-hearthline.md).

### Platt calibration only where it is needed (Ruling Z)

- **Context.** The band uses lift, and expected ARR at risk uses p, so p should be calibrated.
- **Decision.** Fit Platt on the latest 8 matured weeks only if a billing segment with at
  least 300 rows has ECE above 0.02, and keep it only if it lowers the worst such segment's
  ECE (one pooled sigmoid cannot fix segments miscalibrated in opposite directions).
  Contributions are scaled so additivity still holds.
  Isotonic is not used.
- **Alternatives.** Always calibrate; isotonic regression.
- **Why.** Isotonic overfits small samples and its ties reorder the top of the list. Platt is
  monotone in p, so the order by p does not change (the order by p x ARR can).
- **Cost if wrong.** Small probability shifts.
- **Code.** `radar/bootstrap.py` (`train_and_promote`), `radar/model.py` (`platt`);
  `tests/test_model.py::test_platt_keeps_additivity_and_ranking`;
  `tests/test_model.py::test_platt_is_rejected_when_it_worsens_the_worst_segment`.

### Promotion behind gates, with a human click for candidates

- **Context.** A new model changes every CSM's list.
- **Decision.** Training refuses a tenant with fewer than 100 matured decisions and stores a
  fit whose behavioural signs disagree with their risk direction as `rejected`. Promotion
  refuses a candidate whose feature manifest no longer matches the code. On `/ops` an admin
  sees how many bands a candidate would change before promoting it. `radar bootstrap` and
  `radar train` promote their fit directly, as `system`.
- **Alternatives.** Scheduled retraining with automatic promotion.
- **Why.** Labels arrive 90 days late, and once CSMs act on the list, saved accounts become
  negatives; a person should look before the lists move.
- **Cost if wrong.** Models go stale if nobody retrains. The design's fourth gate, "recall@K
  beats renewal sort on the latest origin", is checked by `radar eval` (the model card's
  acceptance gates), not at training time; the code carries a `shortcut:` comment to add it
  when retraining is scheduled.
- **Code.** `radar/bootstrap.py` (`train_and_promote`), `radar/web/oversight.py` (`promote`);
  `tests/test_scoring.py::test_wrong_sign_fit_is_rejected_not_promoted`;
  `tests/test_web.py::test_promote_shows_band_changes_first_then_promotes`.

## Policy and worklist

### A frozen weekly list per CSM, ranked by expected ARR at risk (Rulings AP, AZ, BA)

- **Context.** A CSM has about ten proactive touches a week. A list that re-sorts every day
  cannot be planned around.
- **Decision.** The Monday 06:00 sweep freezes each CSM's list: open must-acts first (all of
  them; past K they are flagged as overflow), then up to two renewal blind-spot check-ins,
  then model slots ranked by p x ARR (rules_score x ARR without a model). A model slot needs
  a key of at least `min_slot_lift` (1.0) x the tenant's mean key over eligible accounts.
  Capacity is a cap, not a quota: a healthy book gets a shorter list, and every list is a
  prefix of its book's ranking. The backtest applies the same floor. A must-act that opens
  after the freeze (Ruling BH) is appended at the end of its owner's list for the week,
  marked new since Monday and counted as overflow past K; nothing re-sorts. An account
  already listed as a model slot or check-in has its row upgraded to the must-act in place
  (same position, new since Monday). If Monday's sweep goes dead, the next sweep that week
  freezes the list from its own run.
- **Alternatives.** Rank by p alone (Ruling AP's floor on p); fill every slot regardless of
  risk; re-rank daily.
- **Why.** p x ARR is the expected loss a touch can address. A floor on the key itself can
  only shorten a list, never reorder it or swap in a smaller account.
- **Cost if wrong.** ARR-weighted lists flag fewer small churners early: measured before the
  label-window and settled-label fixes, tallyworks flagged 70 of 174 decisions ahead under
  the key floor against 90 under the floor on p, while recall@K rose from 42.6% to 59.1%
  (now 65 of 174 and 59.6%). Small high-risk accounts are still covered by must-act
  rules and the Book page.
- **Code.** `radar/policy.py` (`freeze_worklists`, `slot_key`, `slot_floor`);
  `tests/test_evaluate.py::test_floored_top_k_is_a_prefix_of_the_ranking`;
  `tests/test_policy.py::test_a_low_risk_book_gets_fewer_model_slots`;
  `tests/test_policy.py::test_mid_week_must_act_appends_new_since_monday`.

### Hard facts are rules with an SLA, not model output

- **Context.** "We lost two renewals nobody saw coming." Some facts need a person whatever a
  model says.
- **Decision.** Four must-act rules, evaluated when a triggering event lands and again in
  the daily sweep: `cancel_announced` (SLA 1 business day), `dunning_exhausted` (2),
  `urgent_ticket_stalled` (2), `champion_bounced` (5). Each opens one case per account and
  kind, with a due date in the tenant's business days. The hourly SLA check escalates an
  overdue case exactly once, into the manager's digest and the audit log; a case without an
  owner goes to the admin.
- **Alternatives.** Let the model's probability carry these facts.
- **Why.** A cancellation notice is already a fact. "Nothing slips" needs an owner, a due
  date and an escalation; more email does not provide them.
- **Cost if wrong.** A noisy rule floods lists; the circuit breaker below bounds that.
- **Code.** `radar/features.py` (`rule_facts`), `radar/policy.py` (`open_cases`),
  `radar/agent/handlers.py` (`sla_check`);
  `tests/test_worker.py::test_sla_escalation_fires_exactly_once`.

### Red, yellow and green are display only, with a fixed meaning

- **Context.** The VP asked for R/Y/G. A colour whose meaning drifts, or a green on an account
  we know nothing about, is worse than none.
- **Decision.** Red: an open must-act, or lift >= 3 (p over the tenant's mean p). Yellow:
  lift >= 1.5. Unknown: coverage fails. Not in window: an annual account outside its decision
  window with no must-act. Green otherwise. Thresholds are versioned tenant config. The band
  drives no action.
- **Alternatives.** Bands from fixed p cut-offs; bands that open cases.
- **Why.** Only must-acts and the frozen list create work, so the band cannot cause a storm.
- **Cost if wrong.** People may still read red as "call now"; the list is what says who.
- **Code.** `radar/policy.py` (`band`); `tests/test_policy.py::test_band_semantics_red_requires_lift3_or_must_act`;
  `tests/test_policy.py::test_unknown_never_green`.

### A circuit breaker on new cases (Rulings AB, AI)

- **Context.** The generator plants a tenant-wide outage week. A late batch or an outage
  must not become hundreds of cases.
- **Decision.** If new must-act cases in 24 hours exceed max(10, 3 x the trailing 28-day
  daily median), new cases open as `held` and one incident is raised for the admin. The
  bootstrap's first sweep opens the existing backlog once without the breaker (audited), and
  those cases do not count toward later baselines. Usage features are tenant-relative, so an
  outage does not move ranks either.
- **Alternatives.** Per-account rate limits; no breaker.
- **Why.** One incident a person can read beats a flood nobody reads.
- **Cost if wrong.** A real wave of cancellations is held until an admin releases it.
- **Code.** `radar/policy.py` (`open_cases`);
  `tests/test_policy.py::test_breaker_holds_cases_and_opens_one_incident`;
  `tests/test_policy.py::test_outage_week_replay_caps_new_cases_per_csm`.

### The play is chosen by rule; dismissals need a reason

- **Context.** The LLM must not decide what a CSM does.
- **Decision.** Must-act kinds map to fixed plays. A model slot's play follows the source of
  its largest positive behavioural contribution (product or billing: value review; Zendesk:
  support escalation; Gmail or none: exec check-in). "Not a risk" or "already handled" needs
  a reason and holds the account out of model slots until p rises by 0.10 or a must-act
  fires. Managers see every dismissal. A model-slot case closes itself after two Mondays
  outside the top 2K with no outcome.
- **Alternatives.** Let the LLM suggest the play.
- **Why.** The play is auditable and stable for the same evidence.
- **Cost if wrong.** Plays are coarse.
- **Code.** `radar/policy.py` (`choose_play`);
  `tests/test_policy.py::test_suppressed_account_returns_only_on_material_increase`;
  `tests/test_policy.py::test_model_case_auto_closes_after_two_mondays_outside_2k`.

### "Why now" names a behavioural driver (Rulings AQ, BF)

- **Context.** A structural term such as "renewal within 60 days: no (+0.23)" reads as
  nonsense to a CSM, and time drift in tenure is not news.
- **Decision.** `/week` shows the largest material change since the last run; otherwise the
  largest behavioural contribution of at least 0.1 log-odds; otherwise "large account:
  expected ARR at risk <amount>", because the slot is there through p x ARR. The brief's
  driver line follows the same rule.
- **Alternatives.** Show the largest contribution of any kind.
- **Why.** The reason must be something the CSM can check and talk about.
- **Cost if wrong.** Copy only.
- **Code.** `radar/web/queries.py` (`why_now`, `driver`);
  `tests/test_web.py::test_why_now_names_a_behavioural_driver_over_a_larger_structural_one`.

## Agent and LLM

### The job queue is a Postgres table, polled

- **Context.** Work must happen without a person: deliver the feed, check rules, sweep,
  freeze, draft, escalate.
- **Decision.** One `jobs` table. A claim is `SELECT ... FOR NO KEY UPDATE SKIP LOCKED` in a
  CTE; leases last 300 s and completion checks a fencing token; retries back off with full
  jitter, capped at an hour; a job goes `dead` after 5 attempts and shows on `/ops`. A partial
  unique index coalesces queued jobs per dedupe key. Schedules are rows in `schedule_runs`,
  unique per (tenant, kind, period), so each tenant sweeps once per local day across time
  zones and DST. Ingestion enqueues `evaluate_rules` in the same transaction as the data.
- **Alternatives.** Redis or a managed queue; LISTEN/NOTIFY.
- **Why.** No dual write and no lost trigger; one fewer system to run and secure. NOTIFY is
  lost while a listener is down and does not pass through PgBouncer in transaction mode.
- **Cost if wrong.** At high job churn the queue competes with normal work on one primary.
  The 7-day soak ran 2,938 jobs with 0 dead.
- **Code.** `radar/agent/jobs.py`, `radar/agent/worker.py`;
  `tests/test_jobs.py::test_two_workers_never_claim_same_job`;
  `tests/test_jobs.py::test_fenced_completion_after_lease_expiry`;
  `tests/test_worker.py::test_schedules_respect_tenant_timezone_and_dst`.

### Rules on events, the model once a day

- **Context.** Hard facts arrive at any time; the model's inputs are weekly and monthly
  aggregates.
- **Decision.** A triggering event (payment failure, subscription change, ticket status,
  priority or tag change, a bounce) enqueues a coalesced rule check for its account. The model
  rescores every account in the 06:00 local sweep, never on events.
- **Alternatives.** Rescore the model on every event.
- **Why.** Event rescoring is how storms reach the queue, and the inputs do not change
  between batches.
- **Cost if wrong.** Absence-based facts (an urgent ticket stalled for three days) wait for
  the sweep or the next event.
- **Code.** `radar/ingest/pipeline.py`, `radar/agent/handlers.py`;
  `tests/test_worker.py::test_feed_triggers_commit_with_their_batch`.

### The LLM writes words only, from a read-only evidence pack (Rulings AC, AE, AF)

- **Context.** A model's draft can be wrong, can be steered by text a customer wrote, and
  can cost money in a loop.
- **Decision.** Briefs only for must-act cases and frozen model slots, cached by (case,
  evidence hash, prompt version); the pending row is written before the call, so a retry
  never pays twice. The pack is deterministic: account facts, score, what changed, top
  contributions and evidence items E1..En with display strings, a trust tag and the match
  method. It goes in the `tool_result` of a synthetic read-only call with `tool_choice` none.
  Output is schema-checked JSON with no recipient or play field. A deterministic validator
  checks every id, that each number appears in the cited displays, that the email cites only
  items matched by an exact key to this account, and bans words (churn, risk, discount, ...),
  URLs, emails, phones and HTML. One retry with the errors, then the template. No key, LLM
  off, or the tenant's 200,000-token daily budget spent: the template directly.
- **Alternatives.** Free-text generation reviewed by a person; an LLM judge.
- **Why.** The LLM never scores, ranks, picks a recipient or a play. Every claim it makes is
  checked by code before a person reads it.
- **Cost if wrong.** Template briefs are plainer. The template email never quotes system
  evidence (Ruling AF): it is one short message per play.
- **Code.** `radar/agent/evidence.py`, `radar/agent/brief.py`, `radar/agent/validate.py`,
  `radar/agent/template.py`; `tests/test_brief.py`.

### Customer email is only ever a draft

- **Context.** The VP asked for automatic email on red.
- **Decision.** Drafts land in `outbox` as `draft`. Code picks the recipient: the champion
  or primary contact, skipping hard-bounced, departed and opted-out contacts. A CSM may edit
  and approve; approval re-checks access and goes through a dry-run transport. Internal
  digests go out at once through the same dry-run transport.
- **Alternatives.** Auto-send for red accounts.
- **Why.** A wrong email to a customer cannot be taken back. The route to auto-send, one
  play at a time, is in the brief: when CSMs send its drafts unedited about 80% of the time
  for a month.
- **Cost if wrong.** A CSM click per email.
- **Code.** `radar/agent/brief.py` (`recipient`, `approve_and_send`);
  `tests/test_outbox.py::test_no_send_without_approval`.

## Multi-tenancy and security

### Row-level security, forced, on every tenant table

- **Context.** Several customer organisations share one database. Isolation by "every query
  remembers its WHERE clause" fails the first time one does not.
- **Decision.** Every tenant table has `tenant_id` defaulting to the session's
  `app.tenant_id`, a primary key `(tenant_id, id)`, composite foreign keys (foreign-key
  checks bypass RLS), ENABLE and FORCE row-level security, and one policy using
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (after a pooled connection's
  first transaction the setting reads `''`, not NULL). Views are `security_invoker`; there are
  no materialized views and no SECURITY DEFINER functions. Code reaches the database only
  through `tenant_tx`, whose first statement is a transaction-local `set_config`; session
  `SET` is banned. The worker's role sees every tenant's jobs (ids only); the web role only
  its own.
- **Alternatives.** A schema or database per tenant; application-level filtering.
- **Why.** The database enforces it, and a catalog meta-test checks every table, so a new
  table cannot be forgotten.
- **Cost if wrong.** Every query pays the policy filter; jsonb operators are not leakproof,
  so hot filters are typed columns.
- **Code.** `radar/migrations/001_core.sql` to `004_security.sql`, `radar/db.py`
  (`tenant_tx`); `tests/test_isolation.py`.

### App roles that RLS binds, and a startup check (Ruling AU)

- **Context.** Superusers, BYPASSRLS roles and table owners skip RLS. Neon's Vercel
  integration puts the owner's credentials in `DATABASE_URL` and `PG*`.
- **Decision.** Migrations run as the owner. The web app and the worker use `radar_web` and
  `radar_worker`: LOGIN, NOSUPERUSER, NOBYPASSRLS, owning nothing. Both refuse to start on a
  role that could bypass RLS. The runtime never reads `DATABASE_URL` or `PG*`; each role DSN
  must name every connection parameter itself, and production requires TLS.
- **Alternatives.** Trust the deploy configuration.
- **Why.** One wrong environment variable would otherwise turn isolation off silently.
- **Cost if wrong.** None found; it fails closed.
- **Code.** `radar/db.py` (`assert_role_safety`), `radar/config.py`;
  `tests/test_isolation.py::test_web_and_worker_refuse_a_neon_style_owner`;
  `tests/test_config.py::test_role_dsns_must_name_every_parameter_so_libpq_never_reads_pg_vars`.

### Append-only where history matters (Ruling O)

- **Context.** A trace is worthless if its inputs can be edited.
- **Decision.** The app roles get only INSERT and SELECT on `raw_records`, `events`,
  `identity_links`, `account_scores`, `score_runs`, `llm_calls` and `audit_log`; `score_runs`
  may update only `status`, `finished_at` and `n_scored`. Triggers write `audit_log` for
  cases, outbox, suppressions, models and incidents, with the actor from `app.actor`.
- **Alternatives.** Soft deletes and updated_at columns.
- **Why.** A stored score is evidence; it must not change after the fact.
- **Cost if wrong.** Score history grows (about 4.6 MB a day); `radar prune` (owner only)
  removes old non-Monday runs that no list references.
- **Code.** `radar/migrations/004_security.sql`;
  `tests/test_isolation.py::test_append_only_tables_reject_update`.

### One authorization function; anything not yours is a 404

- **Context.** CSMs, managers and admins see different slices of a tenant.
- **Decision.** `authorize(user, account owner, team, action)`: a CSM acts on their own book,
  a manager on their team's, an admin on the tenant plus config and promotion. Tenant and
  role come only from the signed session, re-checked on every request. Anything else returns
  404, so another account's existence is not revealed.
- **Alternatives.** Per-route checks.
- **Why.** One function is one thing to test (a role by route matrix).
- **Cost if wrong.** A shared demo passcode is not real authentication; SSO is skipped.
- **Code.** `radar/rbac.py`, `radar/web/auth.py`; `tests/test_web.py::test_role_route_matrix`;
  `tests/test_web.py::test_idor_other_tenant_account_is_404`.

### Signed webhooks; the tenant comes from the key id

- **Context.** The live path accepts records from outside.
- **Decision.** `POST /webhooks/{source}` verifies a Stripe-style `t=..,v1=..` HMAC over the
  raw body in constant time, with a tolerance window; the tenant comes from the key id, and
  `available_at` is server time. A record with a bad signature is quarantined and never
  lands, so a valid resend still applies.
- **Alternatives.** A tenant id in the URL; trusting the sender's timestamp.
- **Why.** A URL can be guessed; a key cannot. A forged record must not take the id the
  real one needs.
- **Cost if wrong.** Rotating a secret means configuring a second key id next to the first
  for a while (`RADAR_WEBHOOK_SECRETS` takes several).
- **Code.** `radar/ingest/webhooks.py`, `radar/web/hooks.py`;
  `tests/test_ingest.py::test_bad_signature_never_lands_so_valid_resend_applies`;
  `tests/test_web.py::test_webhook_rejects_bad_signature_and_routes_tenant_by_kid`.

## Deployment

### Vercel Hobby and Neon Free, with a 15-minute tick (Rulings U, Y)

- **Context.** The live system must run for the review period at little or no cost, and the
  agent must keep working without a person.
- **Decision.** The web app is one Vercel Python function. A GitHub Actions schedule posts
  to `/internal/tick` every 15 minutes with a bearer secret; the tick runs the worker for up
  to 240 s of a 300 s function. A daily Vercel cron calls the same endpoint as a backstop.
  Postgres is Neon Free (100 CU-hours a month; a project is capped at 512 MB, measured at
  deploy, although the pricing page lists 1 GB), so the live demo holds two of the three
  tenants. Docker
  Compose, with a long-lived worker, is the always-on alternative.
- **Alternatives.** Railway Hobby (the design estimated about $8 a month) with an always-on worker; Vercel
  Pro for per-minute cron.
- **Why.** Free, and the worker is already a stateless `tick()`. A 15-minute cadence keeps
  Neon's compute inside its free hours.
- **Cost if wrong.** Rules react within 15 minutes instead of seconds; GitHub runs can start
  late and pause after 60 days without repository activity (the daily cron covers that).
- **Code.** `radar/web/hooks.py` (`tick`), `radar/agent/worker.py` (`run_for`),
  `.github/workflows/tick.yml`, `vercel.json`; [deploy.md](deploy.md).

### The feed lives in the database on serverless (Ruling Y)

- **Context.** A Vercel function has no persistent disk, but the demo feed must keep
  arriving for six months.
- **Decision.** Bootstrap loads the not-yet-delivered feed lines into `feed_buffer`
  (`FEED_SOURCE=db`); `feed_tick` delivers due lines through the same ingestion entrypoint
  as a webhook and deletes each line in the transaction that delivers it.
- **Alternatives.** Object storage; a second service holding the files.
- **Why.** One store, and delivery and deletion commit together.
- **Cost if wrong.** 43 MiB of the Neon budget until the feed is delivered.
- **Code.** `radar/ingest/feed.py`;
  `tests/test_bootstrap.py::test_feed_buffer_delivers_what_the_files_deliver`.

### Remote bootstrap is a local bootstrap plus `radar push` (Ruling AT)

- **Context.** The bootstrap replays about 230,000 records one by one.
- **Decision.** Bootstrap against a local PostgreSQL 16, then `radar push` copies it to Neon:
  check the target role, migrate, `pg_dump` data only, disable user triggers on the audit
  tables, `pg_restore`, re-enable, ANALYZE, compare row counts. It refuses a loaded target
  unless told `--force`.
- **Alternatives.** Run the bootstrap against Neon; bootstrap from GitHub Actions.
- **Why.** Replaying record by record over a ~250 ms round trip would take days; the push
  takes minutes.
- **Cost if wrong.** The push needs matching client tools (pg_dump 16) and a direct, not
  pooled, owner connection; deploy.md says so.
- **Code.** `radar/ops.py` (`push`);
  `tests/test_bootstrap.py::test_push_copies_every_row_once_and_refuses_a_loaded_target`.

### Storage stays inside Neon's 512 MB: two tenants live, plus pruning (Ruling AM)

- **Context.** The three-tenant database is 584.4 MB, a free Neon project holds 512 MB, and
  daily score rows add about 4.6 MB a day for three tenants.
- **Decision.** The live demo bootstraps two tenants (`radar bootstrap --tenants
  hearthline,kitewire`, 369 MB on Neon); Tallyworks runs locally. `radar prune --older-than-days N` (owner only) deletes old score runs and
  their rows, keeping the latest done run of each kind, every run a list was frozen from, and
  the Monday runs that history reads. One audit row records it.
- **Alternatives.** A paid tier; partitions with cold storage (the 10x answer).
- **Why.** Score rows stay append-only for the app; deletion is a separate, owner-only act.
- **Cost if wrong.** Without pruning, the two-tenant database reaches 512 MB about 45 days
  after bootstrap.
- **Code.** `radar/ops.py` (`prune`);
  `tests/test_bootstrap.py::test_prune_keeps_frozen_latest_and_monday_runs_per_tenant`.

### Training dependencies are an optional extra (Ruling AN)

- **Context.** A Vercel function bundle has a size limit.
- **Decision.** scikit-learn and pyarrow sit in the `ml` extra, used by bootstrap, training,
  evaluation and the simulator. Serving and scoring run on numpy and the JSON artifact.
- **Why.** The deployed function installs about 116 MB of dependencies instead of 344 MB.
- **Cost if wrong.** A stray import breaks the deploy; a test blocks those imports and runs
  the web app and scoring.
- **Code.** `pyproject.toml`; `tests/test_deploy.py::test_web_app_and_scoring_run_without_the_ml_extra`.
