# Data dictionary

Generated from `radar/registry.py`.

## Model features

| name | version | kind | source | unit | risk | definition | implementation |
|---|---|---|---|---|---|---|---|
| usage_drop | 2 | behavioural | product | log ratio | +1 | max(0, -(log((active_users in the last complete month + 1) / (mean of the 3 prior complete months + 1)) minus the tenant median of the same at T)); growth reads 0; NaN with < 4 complete months | `radar.features:usage_drop` v2:fd35e4b12ee4 |
| seat_utilization | 2 | behavioural | product+billing | ratio | -1 | mean active_users over the last 3 complete months (those reported) / seats of the active subscription at T | `radar.features:seat_utilization` v2:847c2711614a |
| integration_broken | 1 | behavioural | product | 0/1 | +1 | 1 if the latest integration event leaves the account in sync_error/disconnected and it has been broken for >= 7 days | `radar.features:integration_broken` v1:d1e4abd9279f |
| not_live_after_30d | 1 | behavioural | product | 0/1 | +1 | 1 if the contract started on or after the product source start, tenure >= 30 days, and no workspace_went_live is visible | `radar.features:not_live_after_30d` v1:25b784d17290 |
| open_urgent_tickets | 1 | behavioural | zendesk | log1p(count) | +1 | log1p(tickets whose current version is new/open/pending/hold and high/urgent) | `radar.features:open_urgent_tickets` v1:ba1519b29c07 |
| support_friction_90d | 1 | behavioural | zendesk | log1p(count) | +1 | log1p(tickets with a version in (T-90d, T] rated bad or reopened after solved) | `radar.features:support_friction_90d` v1:e0e6b39d160e |
| days_since_customer_email | 1 | behavioural | gmail | days | +1 | min(120, days from the last inbound non-auto-reply email to the Gmail watermark); 120 if none since Gmail was connected | `radar.features:days_since_customer_email` v1:b228cea81a55 |
| seat_change_180d | 1 | behavioural | billing | ratio | -1 | (seats at T - seats at T-180d) / seats at T-180d, clipped to [-1, 1] | `radar.features:seat_change_180d` v1:b25e43957870 |
| log_arr | 1 | structural | billing | ln(major units) |  | ln(arr v1) in tenant currency, major units | `radar.features:log_arr` v1:2b05ba0aa80a |
| tenure_years | 1 | structural | crm | log1p(years) |  | log1p(years since the first contract start) | `radar.features:tenure_years` v1:566b6a92b19a |
| term_end_within_60d | 1 | structural | crm+billing | 0/1 |  | 1 if term_end v1 is <= 60 days away; month-to-month is always 1 | `radar.features:term_end_within_60d` v1:4114e2d9dd1b |
| billing_month_to_month | 1 | structural | billing | 0/1 |  | 1 if the account has no contract term | `radar.features:billing_month_to_month` v1:b7377d4c3a51 |
| no_telemetry | 1 | coverage | product | 0/1 |  | 1 if no workspace is linked at T; product features are then imputed by the model | `radar.features:no_telemetry` v1:bd86425d106c |

## Metrics

| name | version | kind | source | unit | risk | definition | implementation |
|---|---|---|---|---|---|---|---|
| arr | 1 | metric | derived | minor units |  | Sum over active Stripe subscription items of unit_amount * quantity * 12 / months_per_interval as of T; SF ARR__c as of T only for accounts with no Stripe link (arr_source=salesforce) | `radar.features:arr` v1:c4b15a011cab |
| term_end | 1 | metric | derived | timestamp |  | Yearly subscriptions: Stripe current_period_end; 12-month contracts paid monthly: SF Renewal_Date__c as of T; month-to-month: the current period end | `radar.features:term_end` v1:bce5d84ed493 |
| completeness_watermark | 1 | metric | derived | timestamp |  | Product: the latest month reported by >= 90% of the linked workspaces that reported the previous month (churned accounts' workspaces stay linked but stop reporting, so they leave the denominator); other sources: the max visible available_at | `radar.features:watermarks` v1:a97599550478 |
| eligible | 1 | metric | derived | bool |  | Active subscription, not test, not merged, not in open dunning, and month-to-month or term end <= eligibility_window_days (150) away; training also needs every source connected 120 days before T | `radar.features:eligible` v1:4594eeedef3c |
| coverage | 1 | metric | derived | bool |  | billing plus at least one of: product telemetry, a Zendesk ticket in 365 days, an inbound email in 180 days | `radar.features:coverage_ok` v1:1c183c7dd65d |
| churn_decision | 1 | metric | derived |  |  | Earliest of: a Stripe cancellation announced and not reversed, the first Closed Lost row on a renewal opportunity, a cancellation_request ticket, or the subscription ending without notice; not merges, involuntary churn or reactivation within 30 days (hindsight, labels only; radar/labels.py, Task 5) |  |
| churn_label_90d | 1 | metric | derived |  |  | 1 if a churn decision falls in (T, T+90d]; excluded if already visible at T, involuntary or merged in the window, or censored at the label cutoff (radar/labels.py, Task 5) |  |
| risk_band | 1 | metric | derived |  |  | Display only: red (open must-act or lift >= 3), yellow (lift >= 1.5), green, unknown (coverage fails), not_in_window (radar/policy.py, Task 7) |  |
| worklist | 1 | metric | derived |  |  | Per CSM per ISO week: open must-acts, up to 2 renewal blind-spot check-ins, then model slots by p * arr up to K (radar/policy.py, Task 7) |  |
| play | 1 | metric | derived |  |  | Must-act kind -> its play; model slots by the source family of the top behavioural contribution (radar/policy.py, Task 7) |  |
| suppression | 1 | metric | derived |  |  | A reasoned dismissal holds the account out of model slots until p rises >= 0.10 or a must-act fires (radar/policy.py, Task 7) |  |
| circuit_breaker | 1 | metric | derived |  |  | New must-acts in 24 h above max(10, 3 x trailing 28-day daily median) open as held with one incident (radar/policy.py, Task 7) |  |
| source_freshness | 1 | metric | derived |  |  | Tenant config freshness_hours (Stripe, Zendesk, Gmail 24 h; Salesforce 26 h); product the monthly batch due on the 3rd + 3 days; checked hourly (radar/agent/handlers.py, Task 9, Ruling AH) |  |
| rules_score | 1 | metric | derived | points |  | 2*integration_broken + 1.5*(open_urgent_tickets>0) + 1*(support_friction_90d>0) + 1*(days_since_customer_email>=60) + 1.5*(usage_drop>0.3) + 1*(seat_change_180d<-0.1) + 1*not_live_after_30d; a missing feature counts as no signal. The rules baseline in evaluation and kitewire's cold-start ranking | `radar.registry:rules_score` v1:793e2ae21d05 |

## Must-act rules

| name | version | kind | source | unit | risk | definition | implementation |
|---|---|---|---|---|---|---|---|
| cancel_announced | 1 | rule | billing+zendesk | bool |  | A subscription active/past_due/trialing with cancel_at_period_end or cancel_at set, or an open ticket tagged cancellation_request. SLA 1 business day; play save_call | `radar.features:cancel_announced` v1:e87c2dfaad39 |
| dunning_exhausted | 1 | rule | billing | bool |  | An open invoice with attempt_count >= 3 or no next attempt after a failure, or past_due for more than 7 days. SLA 2; play billing_outreach | `radar.features:dunning_exhausted` v1:9978ad79d4f6 |
| champion_bounced | 1 | rule | gmail+crm | bool |  | A hard bounce (5.1.1/5.2.1) for a champion contact in the last 30 days. SLA 5; play find_new_champion (internal, no customer draft) | `radar.features:champion_bounced` v1:78439e558169 |
| urgent_ticket_stalled | 1 | rule | zendesk | bool |  | An urgent or high ticket open for more than 3 days. SLA 2; play support_escalation | `radar.features:urgent_ticket_stalled` v1:082bb8b86d76 |

## Banned inputs

| name | version | kind | source | unit | risk | definition | implementation |
|---|---|---|---|---|---|---|---|
| renewal_opportunity_stage | 1 | banned | crm |  |  | Not a model input: human judgement, often written after the outcome |  |
| forecast_category | 1 | banned | crm |  |  | Not a model input: human judgement, often written after the outcome |  |
| close_date | 1 | banned | crm |  |  | Not a model input: human judgement, often written after the outcome |  |
| loss_reason | 1 | banned | crm |  |  | Not a model input: written after the outcome |  |
| sf_type_current | 1 | banned | crm |  |  | Not a model input: post-churn cleanup sets Type, so current state leaks the label |  |
| sf_arr_current | 1 | banned | crm |  |  | Not a model input: post-churn cleanup zeroes ARR__c, so current state leaks the label |  |
| csm_outbound_email_recency | 1 | banned | gmail |  |  | Not a model input: CSM-controlled: the model would learn our own policy (shown as context instead) |  |
| owner_changes | 1 | banned | crm |  |  | Not a model input: they often follow the churn decision |  |
| llm_output | 1 | banned | llm |  |  | Not a model input: anything an LLM wrote is never a model input |  |
