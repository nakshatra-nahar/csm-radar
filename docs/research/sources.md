# Research sources

The synthetic feeds copy the field names, delivery habits and mess of real vendors, and the
design leans on published practice. This page lists what each source says and what Radar
did with it. Every fact here carries its link; facts we could not tie to a source were left
out. The research was done on 2026-10-17; vendor pages change.

- [Stripe](#stripe)
- [Zendesk](#zendesk)
- [Salesforce](#salesforce)
- [Gmail and email standards](#gmail-and-email-standards)
- [Product analytics](#product-analytics)
- [The hearthline tenant](#the-hearthline-tenant)
- [Churn practice](#churn-practice)
- [Postgres multi-tenancy and job queues](#postgres-multi-tenancy-and-job-queues)
- [LLM guardrails](#llm-guardrails)
- [Hosting](#hosting)

## Stripe

| what the source says | what we did |
|---|---|
| An Event has `id` (`evt_…`), `api_version`, `created` (Unix seconds), `data.object` and, on `*.updated`, `data.previous_attributes`; the contents of `data` never change. ([events object](https://docs.stripe.com/api/events/object)) | The feed emits Event envelopes with these fields. The evt id is the raw record's `source_record_id`. |
| Live-mode webhooks retry "for up to three days with an exponential backoff". ([webhooks](https://docs.stripe.com/webhooks)) | 1.5% of Stripe deliveries get a backoff tail capped at 72 hours (`feed.stripe.tail`). |
| "Stripe doesn't guarantee the delivery of events in the order that they're generated", and `created` is in seconds, so events can share a timestamp. ([webhooks](https://docs.stripe.com/webhooks)) | Events in the same second are shuffled. A subscription's version key ranks terminal states first, so a late "active" cannot resurrect a cancelled subscription. |
| Endpoints may receive the same event more than once; in some cases two separate Event objects are generated for one change, identified by the object id. ([webhooks](https://docs.stripe.com/webhooks)) | Both are planted: 1% re-deliveries with the same evt id (deduplicated on the raw id) and 0.3% semantic twins (dropped and counted on a hash of type and data). |
| The `Stripe-Signature` header is `t=<unix>,v1=<HMAC-SHA256>` over `timestamp.body`; libraries default to a 5-minute tolerance. ([webhooks](https://docs.stripe.com/webhooks)) | The verifier uses the same shape plus a key id that names the tenant, a constant-time compare and a 300 s tolerance. |
| Since 2024-09-30 Stripe ships monthly versions and two major releases a year: basil 2025-03-31, dahlia 2026-03-25. ([versioning](https://docs.stripe.com/api/versioning)) | Hearthline and tallyworks move from `2024-06-20` to `2025-03-31.basil` on 2025-06-01; kitewire is on `2026-03-25.dahlia`. |
| Basil moved `current_period_start/end` from the Subscription onto its items. ([changelog](https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end)) | The normaliser reads the period end from either place. |
| Basil removed `Invoice.subscription`; use `invoice.parent.subscription_details.subscription`. ([changelog](https://docs.stripe.com/changelog/basil/2025-03-31/adds-new-parent-field-to-invoicing-objects)) | The normaliser reads the subscription reference from either place. |
| `cancel_at_period_end` and `cancel_at` both schedule a cancellation; in flexible billing mode a portal cancellation sets `cancel_at` and leaves `cancel_at_period_end` false. ([subscription object](https://docs.stripe.com/api/subscriptions/object); [billing modes](https://docs.stripe.com/billing/subscriptions/billing-mode/compare)) | The `cancel_announced` rule and the churn label read either field; 25% of generated cancels use `cancel_at`. |
| `cancellation_details.reason` is one of `cancellation_requested`, `payment_failed`, `payment_disputed`, `canceled_by_retention_policy`. ([subscription object](https://docs.stripe.com/api/subscriptions/object?query=cancellation_details)) | A `payment_failed` cancellation is involuntary churn: excluded from the label and owned by the dunning rule. |
| Smart Retries: "The recommended default setting is 8 tries within 2 weeks". ([smart retries](https://docs.stripe.com/billing/revenue-recovery/smart-retries)) | The generator makes up to 8 attempts over 14 days and recovers 60%. |
| `invoice.payment_failed` fires for each failed attempt; `attempt_count` counts retry-schedule attempts; `next_payment_attempt` is the next try. ([invoice object](https://docs.stripe.com/api/invoices/object)) | `dunning_exhausted` fires on an open failed invoice with `attempt_count` >= 3 or no next attempt, or past_due for more than 7 days; never on the first failure. |
| Amounts are integers in the currency's smallest unit. ([currencies](https://docs.stripe.com/currencies)) | Money is stored as minor units (`amount_minor`, `arr_minor`). |
| Metadata does not copy to related objects. ([metadata](https://docs.stripe.com/metadata)) | Only the customer carries `sf_account_id`; subscriptions and invoices are attributed through the customer's identity link. |

## Zendesk

| what the source says | what we did |
|---|---|
| Ticket fields include `priority` (urgent, high, normal, low, or null), `status`, `type`, `organization_id` and `tags`. ([tickets API](https://developer.zendesk.com/api-reference/ticketing/tickets/tickets/)) | Tickets are emitted with these fields and enums; about 10% have no organisation. |
| `updated_at` changes only when an update creates a ticket event; `generated_timestamp` changes on every update, including system ones. ([OpenAPI spec](https://developer.zendesk.com/zendesk/oas.yaml)) | A ticket version's id is `ticket_id:updated_at:generated_timestamp`; `updated_at` is its `occurred_at`. |
| The incremental export returns items changed on or after the start time; "subsequent responses may contain duplicate items". ([incremental export](https://developer.zendesk.com/documentation/ticketing/managing-tickets/using-the-incremental-export-api/)) | Zendesk arrives as versioned snapshots, about two per ticket, with occasional duplicates that the raw ledger counts and drops. |
| An organisation has `external_id`, a unique `name` and `domain_names`. ([organizations](https://developer.zendesk.com/api-reference/ticketing/organizations/organizations/)) | 70% of organisations carry the Salesforce id as `external_id`; the rest can match only by a unique corporate domain. |
| `satisfaction_rating.score` is `offered`, `unoffered`, `good` or `bad`. ([satisfaction ratings](https://developer.zendesk.com/api-reference/ticketing/ticket-management/satisfaction_ratings/)) | `support_friction_90d` counts tickets rated bad or reopened in the last 90 days. |

## Salesforce

| what the source says | what we did |
|---|---|
| 15-character ids are case-sensitive; API calls return 18-character case-safe ids whose last three characters encode the case of the first 15. ([object reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/object_reference.pdf)) | Ids typed into other tools are normalised: trimmed, a 15-character id extended with its checksum, and an 18-character id's case restored from its suffix, so even a lowercased id is recovered. |
| Account ids start with `001`, opportunities with `006`. ([SOAP API guide](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/apex_api.pdf)) | An external id without the `001` prefix is not used as a key; with no other key the record goes to quarantine. |
| After a merge, the deleted record's `MasterRecordId` holds the id of the record kept. ([object reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/object_reference.pdf)) | 1% of accounts get a duplicate that is merged later; ingestion adds a `merge` link from the loser to the master. |
| `SystemModstamp` is when a user or an automated process last modified the record. ([object reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/object_reference.pdf)) | A Salesforce record's id is `Id:SystemModstamp`, and `SystemModstamp` is its `occurred_at`. |
| `OpportunityHistory` gets a row whenever Amount, Probability, StageName or CloseDate changes. ([object reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/object_reference.pdf)) | The churn label takes the first Closed Lost history row on a renewal opportunity, dated by its `CreatedDate`. Stage and loss reason are banned as model inputs. |
| `HasOptedOutOfEmail` marks a contact who does not want email; bounce fields are set only for mail sent through Salesforce. ([object reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/object_reference.pdf)) | Opted-out contacts are never a draft's recipient. Bounces come from Gmail delivery reports instead. |

## Gmail and email standards

| what the source says | what we did |
|---|---|
| A message's `internalDate` is a string of epoch milliseconds; `id` and `threadId` belong to one mailbox. ([messages](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)) | `internalDate` is the email's `occurred_at`. One message copied to two CSMs arrives twice and is kept once, keyed by its `Message-ID` header. |
| The `metadata` format returns only ids, labels and headers. ([format](https://developers.google.com/workspace/gmail/api/reference/rest/v1/Format)) | The feed carries headers only; bodies are never collected. |
| The `Date` header is when the sender finished the message, in the sender's local time, not when it was sent. ([RFC 5322](https://www.rfc-editor.org/rfc/rfc5322)) | 5% of generated `Date` headers carry a wrong offset, and Radar never reads `Date` as event time. |
| A delivery status notification reports a status code per recipient; 5.1.1 is a bad mailbox, 4.x is temporary. ([RFC 3464](https://www.rfc-editor.org/rfc/rfc3464); [RFC 3463](https://www.rfc-editor.org/rfc/rfc3463)) | `champion_bounced` fires on 5.1.1 or 5.2.1 for a champion; 4.x delays and 5.7.1 policy rejects are ignored. |
| `Auto-Submitted: auto-replied` marks automatic replies. ([RFC 3834](https://www.rfc-editor.org/rfc/rfc3834)) | Auto-replies are flagged and do not count as customer email in `days_since_customer_email`. |

## Product analytics

| what the source says | what we did |
|---|---|
| A Segment track call has `event`, `properties`, `timestamp`, `messageId`, and `receivedAt` set by Segment; `context.groupId` carries the account in B2B use. ([common fields](https://www.twilio.com/docs/segment/connections/spec/common); [B2B SaaS spec](https://www.twilio.com/docs/segment/connections/spec/b2b-saas)) | Discrete product events are Segment-shaped, named like `Integration Disconnected`, with the workspace in `context.groupId`; the `messageId` is the record id. |
| Segment deduplicates on `messageId`, not on content. ([duplicate data](https://www.twilio.com/docs/segment/guides/duplicate-data)) | Product events are deduplicated on `messageId`. |
| Segment corrects device clock skew from `sentAt` and `receivedAt`. ([timestamps](https://www.twilio.com/docs/segment/connections/spec/common#timestamps)) | A few events carry a future timestamp; they go to quarantine as `future_timestamp`. |
| GA4 updates daily export tables for up to two more days; restated data is normal. ([GA4 export](https://support.google.com/analytics/answer/9358801)) | Usage summaries are restated as version + 1 within 3 days, and one month per tenant is reprocessed later. |

## The hearthline tenant

Hearthline is shaped like Fello's public description of itself. The exercise's
Salesforce/Zendesk/Stripe stack is a stand-in, not Fello's confirmed stack.

| what the source says | what we did |
|---|---|
| "Fello currently offers 12 month contracts with the option to pay monthly or annually." ([contract term](https://fello.ai/knowledge/what-is-the-contract-term-for-fello)) | Hearthline has no month-to-month plans: 60% pay annually, 40% monthly on a 12-month contract, and decisions cluster at renewal. |
| Public plans are Growth, Scale and Enterprise, priced per contact tier and seat. ([pricing](https://fello.ai/pricing)) | Hearthline's plans are growth, scale and enterprise, billed per seat (one price item per subscription; a stated shortcut). |
| G2 reviewers' complaints: inaccurate valuations, per-seat cost, lead quality, learning curve, friction with Follow Up Boss, missing features. ([G2 reviews](https://g2.com/products/fello-fello/reviews)) | Hearthline's Salesforce loss reasons are data_accuracy, price_roi, lead_quality, onboarding_training, deliverability_10dlc and feature_gap. |
| Fello integrates natively with CRMs such as Follow Up Boss and Sierra Interactive, through an API key. ([CRM integrations](https://fello.ai/knowledge/which-crms-can-fello-integrate-with-)) | A CRM integration break is a planted shock (10% a year), seen as `Integration Sync Error` and `Integration Disconnected` events. |
| Fello renamed its lead score to a signal-intent measure. ([signals and intent](https://fello.ai/knowledge/knowledge/signals-and-intent-in-fello)) | The usage schema moves to v2 on 2025-09-01 and renames `lead_score_avg` to `signal_intent_high`. |
| New-home sales are seasonal: a monthly index from FRED series HSN1FNSA runs from 1.17 in March to 0.85 in December. ([FRED](https://fred.stlouisfed.org/series/HSN1FNSA)) | Hearthline's engagement usage is multiplied by that index (`seasonality` in params.py); usage features are tenant-relative, so the season does not read as risk. |

## Churn practice

| what the source says | what we did |
|---|---|
| Gainsight: low-touch CSMs (under $10K ACV) average 144 accounts. KBCM/Sapphire: the SMB median is 110 accounts per CSM. ([Gainsight](https://www.gainsight.com/blog/gainsight-horizon-ai-labs-what-is-the-right-csm-to-customer-ratio/); [SuccessCOACHING summary](https://research.successcoaching.co/docs/focus-cs-coverage)) | Books hold about 110 to 120 accounts; capacity K is a per-tenant setting with a default of 10. |
| SaaS Capital 2025: median gross revenue retention around 90%, and 89% for month-to-month against 90% annual and 94% multi-year. ([SaaS Capital](https://www.saas-capital.com/wp-content/uploads/2025/09/RB32WS1-2025-B2B-SaaS-Retention-Benchmarks.pdf)) | Churn targets: hearthline about 13% annual logo churn; tallyworks about 24% month-to-month and 12% annual. |
| Leakage is "the introduction of information about the data mining target, which should not be legitimately available to mine from"; time-stamp every observation. (Kaufman, Rosset and Perlich, [KDD 2011](https://www.cs.umb.edu/~ding/history/470_670_fall_2011/papers/cs670_Tran_PreferredPaper_LeakingInDataMining.pdf)) | Two clocks on every record, banned inputs, and a naive current-state row in each model card that shows the leak. |
| Churn-model tournaments are scored by top-decile lift; logit models did well. (Gupta et al., [Defection detection](https://iaorifors.com/paper/53214)) | The headline is recall@K inside each CSM's book; the model is a logistic regression. |
| Targeting by the effect of the intervention reduces churn more than targeting by risk. (Ascarza 2018, [AMA summary](https://www.ama.org/2019/04/24/escarzas-retention-futility-targeting-high-risk-customers-might-be-ineffective-wins-2018-award/)) | Uplift modelling is on the "next" list; worklist items and case outcomes are logged so it becomes possible. |
| In a field experiment, proactive outreach raised churn from 6% to 10% over three months. (Ascarza, Iyengar and Schleicher 2016, [paper](https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/12782/Perils_Ascarza.pdf)) | One reason there is no automatic email: a person decides each touch. |
| Isotonic calibration "is not recommended when the number of calibration samples is too low (≪1000)". ([scikit-learn calibration](https://scikit-learn.org/stable/modules/calibration.html)) | Platt only, and only for a segment with at least 300 rows and ECE above 0.02. |
| PostHog's handbook shows health bands including "Unknown" rather than a colour for an account it cannot judge. ([PostHog handbook](https://posthog.com/handbook/cs-and-onboarding/health-tracking)) | An account without enough data is Unknown, never green. |

## Postgres multi-tenancy and job queues

| what the source says | what we did |
|---|---|
| Superusers and BYPASSRLS roles always bypass row-level security; a table's owner does too unless the table has FORCE ROW LEVEL SECURITY. ([row security](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)) | Every tenant table has ENABLE and FORCE; the app roles are NOSUPERUSER, NOBYPASSRLS and own nothing, and the app checks this at startup. |
| `set_config(name, value, true)` lasts until the end of the transaction; a session `SET` outlives it and leaks to the next pool checkout. ([admin functions](https://www.postgresql.org/docs/16/functions-admin.html); [psycopg pool](https://www.psycopg.org/psycopg3/docs/api/pool.html)) | `tenant_tx` sets the tenant with a transaction-local `set_config`; session `SET` is banned. |
| Views read tables with the view owner's rights unless `security_invoker = true`; materialized views cannot have RLS; foreign-key checks bypass RLS. ([CREATE VIEW](https://www.postgresql.org/docs/16/sql-createview.html); [row security](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)) | Views are `security_invoker`, there are no materialized views, and foreign keys include `tenant_id`. |
| Policy conditions run before user conditions unless the user's functions are LEAKPROOF; jsonb operators are not. ([CREATE POLICY](https://www.postgresql.org/docs/16/sql-createpolicy.html)) | Filtered attributes are typed columns (`status`, `priority`, `amount_minor`), not jsonb. |
| `SELECT ... FOR UPDATE SKIP LOCKED` skips locked rows; `FOR NO KEY UPDATE` does not block the lock foreign-key checks take. ([SELECT](https://www.postgresql.org/docs/16/sql-select.html); [explicit locking](https://www.postgresql.org/docs/16/explicit-locking.html)) | Workers claim jobs with `FOR NO KEY UPDATE SKIP LOCKED` in a CTE. |
| A visibility timeout makes a claimed message reappear if its worker dies; retries should use capped exponential backoff with full jitter. ([SQS visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html); [AWS backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)) | Jobs have 300 s leases with a fencing check on completion, and full-jitter backoff capped at an hour. |
| `INSERT ... ON CONFLICT` on a partial unique index must repeat the index's predicate. ([INSERT](https://www.postgresql.org/docs/16/sql-insert.html)) | Job coalescing and the one-open-incident index repeat their predicates in `ON CONFLICT`. |
| NOTIFY is delivered only when the sending transaction commits, and only to listeners connected at the time. ([NOTIFY](https://www.postgresql.org/docs/16/sql-notify.html)) | The worker polls instead; the guarantee comes from enqueueing in the data's own transaction. |
| Transaction-mode poolers do not support session `SET`, `LISTEN` or SQL-level `PREPARE`. ([Neon pooling](https://neon.com/docs/connect/connection-pooling); [PgBouncer features](https://www.pgbouncer.org/features.html)) | Behind Neon's pooler, `RADAR_PGBOUNCER=1` turns prepared statements off and takes the session time zone from the role. |
| An as-of read must filter on both event time and record time, or late and restated rows leak into the past. ([Fowler, bitemporal history](https://martinfowler.com/articles/bitemporal-history.html); [Feast point-in-time joins](https://docs.feast.dev/getting-started/concepts/point-in-time-joins)) | The as-of rule: `available_at <= T AND occurred_at <= T`. |

## LLM guardrails

| what the source says | what we did |
|---|---|
| Schema-validated JSON output is sent as `output_config={'format': {'type': 'json_schema', ...}}` on `messages.create`. ([structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)) | Briefs are requested with `output_config` holding the `Brief` schema and `effort: medium`. |
| Claude Haiku 5.5 is `claude-haiku-5-5`, priced per million tokens at $0.10 input and $0.50 output (prompts up to 100,000 tokens). ([Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview); [pricing](https://platform.claude.com/docs/en/about-claude/pricing)) | The default model, with a dated price table used for the cost of every call. |
| "Put untrusted content only in tool results", and state the policy in the system prompt. ([mitigate jailbreaks](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks)) | The evidence pack is the `tool_result` of a synthetic read-only call; ticket text is tagged untrusted, and the system prompt states the policy. |
| OWASP LLM01: constrain behaviour and validate output formats with deterministic code. ([LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)) | A deterministic validator checks ids, numbers, banned words and contact data before anything is stored as a draft. |
| OWASP LLM05: treat the model as an untrusted user and encode its output. ([LLM05](https://genai.owasp.org/llmrisk/llm052025-improper-output-handling/)) | Autoescape on, no `safe` filter, a CSP header, plain-text drafts. |
| OWASP LLM06: an extension should "only read mail, not delete or send it". ([LLM06](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)) | The model has no tools that act; a person approves every email and the transport is a dry run. |
| OWASP LLM09 and LLM10: human oversight against misinformation; limits against unbounded consumption. ([LLM09](https://genai.owasp.org/llmrisk/llm092025-misinformation/); [LLM10](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/)) | Every number must appear in the cited evidence; a per-tenant daily token budget, one retry, then the template. |
| Haiku 5.5 does not accept a non-default temperature, so identical inputs can give different outputs. ([Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)) | Determinism lives in the evidence pack and the validator, not in the model. |

## Hosting

| what the source says | what we did |
|---|---|
| Neon Free: 1 GB storage per project, 100 CU-hours a month, compute suspended after 5 minutes without queries. ([Neon pricing](https://neon.com/pricing); [scale to zero](https://neon.com/docs/introduction/scale-to-zero)) | The demo runs on Neon Free with a 15-minute tick (about 65 to 70 CU-hours a month at 0.25 CU), and `radar prune` keeps storage under 1 GB. |
| Vercel Functions get at most 2 GB of memory on Hobby and 4 GB on Pro. ([Vercel limits](https://vercel.com/docs/functions/limitations)) | The 10x analysis: worker memory is the second thing to break. |
| Railway's config-as-code files are deprecated; new services cannot opt in. ([Railway docs](https://docs.railway.com/reference/config-as-code)) | No `railway.toml`; the Docker image and compose file serve any always-on host. |
| Render's free web services spin down after 15 minutes without traffic. ([Render free](https://render.com/docs/free)) | Not chosen: the agent must run while nobody is looking. |
