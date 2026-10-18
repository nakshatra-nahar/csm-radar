"""Every generator knob in one dict (design §3.2). The README's generator table prints from it.

Rates are annual unless named `_weekly` or `_monthly`. Health effects are level offsets on h.
"""

from datetime import UTC, date, datetime

HISTORY_END = datetime(2026, 10, 17, tzinfo=UTC)  # the generator's default "now"
PARAMS: dict = {
    "sim_start": date(2022, 2, 28),  # a Monday; weeks run Monday to Sunday
    # account start dates are uniform over this window (per tenant)
    "start_window": {
        "hearthline": (date(2022, 3, 1), date(2026, 12, 1)),
        "tallyworks": (date(2022, 3, 1), date(2026, 12, 1)),
        "kitewire": (date(2025, 11, 1), date(2026, 11, 15)),
    },
    # seats ~ lognormal(median, sigma) clipped; plans are (name, max seats, monthly minor units
    # per seat); segments are (name, max seats). shortcut: one per-seat price item, no add-ons.
    "accounts": {
        "hearthline": {"seats": (6, 0.7, 2, 120), "domain": ".com",
                       "plans": [("growth", 5, 12500), ("scale", 20, 7500),
                                 ("enterprise", 9999, 6000)],
                       "segments": [("Small Team", 5), ("Medium Team", 10), ("Large Team", 20),
                                    ("Mega Team", 50), ("Enterprise Team", 9999)]},
        "tallyworks": {"seats": (3, 0.6, 1, 40), "domain": ".co.uk",
                       "plans": [("starter", 2, 1800), ("standard", 6, 3200),
                                 ("premium", 9999, 4800)],
                       "segments": [("Micro", 2), ("Small", 10), ("Medium", 9999)]},
        "kitewire": {"seats": (12, 0.9, 3, 400), "domain": ".io",
                     "plans": [("team", 20, 2500), ("business", 100, 3500),
                               ("enterprise", 9999, 4500)],
                     "segments": [("SMB", 20), ("Mid-Market", 100), ("Enterprise", 9999)]},
    },
    "annual_discount": 0.83,  # yearly price = 12 * monthly * this
    "latent": {"phi": 0.92, "sigma": 0.25, "mu_mean": 0.3, "mu_sd": 0.6},
    "onboarding": {"ramp_weeks": 8, "start_offset": -1.2, "fail_rate": 0.15,
                   "fail_offset": -1.0, "fail_weeks": 52},
    # shock: annual rate and health effect (see world._profile for the shape)
    "shocks": {
        # SF learns of the departure in sf_mark_share of cases, sf_lag_weeks later;
        # a successor champion appears successor_weeks later
        # Task 5b: durations and sizes set so a shock still weighs on the decision 90 days on
        "champion_departure": {"rate": 0.15, "effect": -1.0, "weeks": 26, "sf_mark_share": 0.6,
                               "sf_lag_weeks": (3, 12), "successor_weeks": (2, 10)},
        "integration_break": {"rate": 0.10, "effect": -0.8, "repair_share": 0.4,
                              "repair_mean_weeks": 4, "ticket_share": 0.5},
        "budget_cut": {"rate": 0.12, "effect": -1.0, "weeks": 52, "seat_cut": 0.3},
        "competitor_eval": {"rate": 0.10, "drift": -0.05, "weeks": 16, "recover_weeks": 26},
        "bug_episode": {"rate": 0.20, "effect": -1.0, "weeks": 12, "resolved_share": 0.4},
        "expansion": {"rate": 0.15, "effect": 0.6, "weeks": 26, "seat_add": 0.3},
    },
    "outage": {"hearthline": date(2025, 11, 10), "tallyworks": date(2026, 3, 2),
               "account_share": 0.3, "effect": -0.15},
    # FRED HSN1FNSA monthly index (Jan..Dec), applied to hearthline engagement usage only
    "seasonality": [0.89, 1.02, 1.17, 1.15, 1.11, 1.09, 1.02, 0.95, 0.93, 0.94, 0.89, 0.85],
    # churn link: P(churn at renewal) = sigmoid(alpha + beta * mean h over prior 8 weeks);
    # month-to-month weekly hazard = sigmoid(alpha + beta * h_t)
    "churn": {
        "hearthline": {"annual": (-3.3, -2.8), "monthly_on_contract": (-3.3, -2.8)},
        "tallyworks": {"annual": (-4.0, -3.2), "month_to_month": (-9.4, -4.5)},
        "kitewire": {"annual": (-3.45, -2.8), "month_to_month": (-6.5, -2.0)},
    },
    "notice_days": (30, 75),
    # annual rate of churn drawn independently of h; measured share of voluntary churn
    # decisions at seed 7: hearthline 18%, tallyworks 11%, kitewire 15%
    "unforecastable": {"rate": {"hearthline": 0.026, "tallyworks": 0.03, "kitewire": 0.05},
                       "causes": ["acquired", "business_closed", "team_dissolved",
                                  "brokerage_switch"]},
    # SF loss reasons (hearthline: Fello G2 complaint themes); a recent shock names the cause
    "loss_reasons": {
        "hearthline": ["data_accuracy", "price_roi", "lead_quality", "onboarding_training",
                       "deliverability_10dlc", "feature_gap"],
        "tallyworks": ["price", "missing_feature", "onboarding", "support_experience"],
        "kitewire": ["price", "performance", "missing_feature", "support_experience"],
    },
    "shock_reasons": {
        "hearthline": {"integration_break": "crm_sync", "competitor_eval": "competitor",
                       "budget_cut": "team_downsized", "bug_episode": "product_bugs"},
        "tallyworks": {"integration_break": "bank_feed_issues", "competitor_eval": "competitor",
                       "budget_cut": "cost_cutting", "bug_episode": "product_bugs"},
        "kitewire": {"integration_break": "integration_issues", "competitor_eval": "competitor",
                     "budget_cut": "cost_cutting", "bug_episode": "product_bugs"},
    },
    "notice_modes": {"stripe_cancel": 0.7, "no_notice": 0.2, "cancellation_ticket": 0.1},
    "cancel_at_share": 0.25,  # of Stripe cancels, use cancel_at instead of cancel_at_period_end
    "save_share": 0.5,  # renewers schedule then reverse a cancel with prob save_share * p_churn
    "loss_reason_noise": 0.25,  # SF loss reason differs from the true cause this often
    # attempt_gap_hours: 8 Smart Retries attempts land inside the 14-day window
    "payments": {"fail_monthly": 0.015, "max_attempts": 8, "retry_days": 14, "recover": 0.6,
                 "attempt_gap_hours": 47},
    "opp_open_days": 120,  # renewal opportunity opens this long before the term end
    "vp_notice_days": 35,  # the VP cases decide this long before the term end they lose
    "telemetry_share": 0.82,
    # hearthline: chance a shock also pauses the Felix Sales Agent (account-wide)
    "sales_agent_pause": {"competitor_eval": 0.5, "budget_cut": 0.3},
    "contacts_per_account": (1, 3),
    "personal_email_share": 0.1,
    "merge_share": 0.01,  # accounts that get a duplicate SF record, merged weeks later
    "test_accounts": 3,
    "tickets": {
        "howto_weekly": 0.007, "howto_h": 0.5,  # rate * exp(howto_h * h)
        "complaint_weekly": 0.008, "complaint_h": -1.2,  # rate * exp(complaint_h * h)
        "silent_share": 0.4, "silent_factor": 0.3,  # silent accounts file fewer of both
        "bug_burst": (2, 4), "reopen_share": 0.1, "csat_offered": 1.0,
        # solve time = median hours by kind * lognormal(0, solve_sigma)
        "solve_hours": {"how_to": 20, "complaint": 48, "bug": 72, "integration": 48,
                        "outage": 6, "cancellation": 24},
        "solve_sigma": 0.8,
        # bug and integration tickets stay open until the fix ships (episode end or repair);
        # a break never repaired is closed unfixed this many weeks after onset
        "unfixed_close_weeks": 12,
        # answered share of offered CSAT; P(bad) = sigmoid(-(a + b * h)) + incident_bad
        # for any kind but how_to
        "csat_answered": 0.7, "csat_bad": (1.0, 3.5), "csat_incident_bad": 0.2,
    },
    # outbound = outbound_weekly * (seats / median) ** size_exp;
    # inbound = inbound_weekly * exp(inbound_h * h); reply delay = hours * lognormal(0, sigma)
    "email": {"outbound_weekly": 0.05, "size_exp": 0.3, "reply_base": 0.0, "reply_h": 1.2,
              "reply_delay": (6, 1.0), "inbound_weekly": 0.01, "inbound_h": 0.6,
              "auto_reply": 0.03, "manager_cc": 0.05,
              "soft_bounce": 0.004, "policy_reject": 0.002, "suppress_days": 60},
    # monthly usage: value = seats * per_seat * utilisation * noise (* seasonality if listed);
    # utilisation = lo + (hi - lo) * sigmoid(slope[tenant] * mean h of the month); hearthline's
    # flatter slope keeps its usage level under the design §3.6 single-feature cap (Task 5b)
    "usage": {
        "utilisation": (0.2, 0.95), "noise_sd": 0.2,
        "slope": {"hearthline": 0.8, "tallyworks": 1.3, "kitewire": 1.5},
        "per_seat": {
            "hearthline": {"active_contacts": 450, "emails_sent": 250, "dashboard_views": 30,
                           "form_submissions": 2, "automations_active": 0.8,
                           "felix_calls_made": 40},
            "tallyworks": {"invoices_created": 20, "bank_transactions_reconciled": 80,
                           "reports_run": 5},
            "kitewire": {"builds_run": 60, "deploys": 8, "api_calls": 3000},
        },
        "seasonal": ["emails_sent", "dashboard_views", "form_submissions", "felix_calls_made"],
        # hearthline derived metrics: delivered = sent * U(lo, hi); opens = delivered *
        # (a + b * util); handoffs ~ Poisson(calls * rate); lead score = a + b * util + N(0, sd)
        "hearthline_derived": {"delivered": (0.95, 0.99), "open_rate": (0.1, 0.25),
                               "handoff_rate": 0.025, "lead_score": (30, 50, 3)},
    },
    # vendor payload and delivery mess (design §3.5); shares are of records unless named
    "feed": {
        # customers: metadata present / mangled when present / key renamed to salesforce_id;
        # a missing id is added later by a customer.updated with prob metadata_fixed;
        # billing email on a free-mail (or, hearthline, franchise) domain
        "stripe": {"metadata": 0.85, "messy_id": 0.2, "salesforce_id_key": 0.05,
                   "metadata_fixed": 0.3, "free_mail": 0.1,
                   "tail": 0.015, "tail_mean_hours": 12, "redeliver": 0.01, "twin": 0.003},
        "zendesk": {"org_external_id": 0.7, "duplicate": 0.005},
        "salesforce": {"reexport": 0.02},
        "gmail": {"date_skew": 0.05},
        # sf_account_id present on usage rows; late batches are 1-9 days late; restated rows
        # get version+1 within 3 days; one month per tenant is reprocessed ~30 days later
        "product": {"sf_account_id": 0.9, "messy_id": 0.2, "late_batch": 0.1,
                    "restate": 0.03, "future": 0.001},
    },
}
