# Solution brief: catching at-risk renewals before they are lost

To: VP Customer Success · From: Nakshatra Nahar · 17 Oct 2026 · Written before any code

## The problem as I read it

Your CSMs aren't short of data. They're short of a trustworthy answer to "who do I call this week, and why?" Each CSM owns about 120 accounts and has room for maybe ten proactive conversations a week. Today those ten come from a spreadsheet, so the choice follows renewal dates and memory. The two renewals lost last quarter had warnings in Zendesk and in product usage that nobody looked at in time. The job is not to score everything. It is to put the right ten accounts in front of each CSM every Monday, with the evidence, and to make sure no serious risk sits without an owner.

## What I would build

A weekly worklist for each CSM, backed by a risk model and an agent that runs on its own.

- Every account gets an estimate of how likely it is to decide to leave in the next 90 days, fitted on your own history in Salesforce, Stripe, Zendesk, product usage and email metadata. Hard facts skip the model and go to the top: a cancellation request, a payment that retries didn't recover, a champion's email bouncing.
- On Monday each CSM sees their top ten: revenue at stake, what changed since last week, the evidence, a suggested move and a draft email.
- An agent watches incoming data around the clock. When a hard fact arrives it opens a case for the owner the same day. Every case has a due date; a missed one goes to the manager.
- A team page shows every account as red, yellow or green, revenue at risk per CSM, and risky accounts without a working owner.
- Every number on screen links to the data and model version behind it.
- Each customer organisation's data is kept apart in the database, with its own model. A new organisation with little history runs on rules until it has enough.

First I would load the sources with the time each record actually arrived, replay last year week by week, and check two things. Would this have flagged your two lost renewals, and how early? Does it beat sorting the spreadsheet by renewal date? If not, nothing else matters.

## Where I would do it differently

Red/yellow/green: you get it on every account, and red means the same thing every week: at least three times the usual risk, or a hard fact. But a CSM with 30 reds still doesn't know who to call first, so the Monday list is ranked by revenue at risk and sized to what one person can do.

Auto-emailing red accounts: the system does send an email, to the CSM, with the draft and the evidence, one click from sending. I wouldn't let it write to customers on its own yet. A model is wrong some of the time, and an automatic "just checking in" to a customer with an angry open ticket, or to a champion who left last month, does damage. If a ticket is matched to the wrong account, it could even quote another customer's problem. I'd switch on auto-send one play at a time, once CSMs send its drafts unedited about 80% of the time for a month.

"Nothing slips" comes from ownership, not email volume: every serious risk gets an owner, a due date and an escalation.

Accounts with almost no data show as Unknown, never green. If one renews within 90 days, it joins the list as a check-in.

## What I need from the business

1. The two accounts lost last quarter, with dates. They are the acceptance test.
2. What counts as lost. Default: cancellation and non-renewal; downgrades reported separately.
3. Which Salesforce field holds the CSM, and who each CSM reports to.
4. Weekly capacity per CSM (default ten) and holiday cover.
5. Which system wins when Stripe and Salesforce disagree. Default: Stripe for money, Salesforce for owners.
6. Approved plays, who signs off customer-facing wording, and legal approval to read email metadata (never content).
7. Whether customer organisations allow a shared model. Until then, each gets its own.
8. A six-week pilot with three CSMs and a manager, measured on cases handled on time and "surprise churn": lost revenue that never appeared on its owner's list.

All data in this exercise is synthetic and deliberately messy; the README has the details.
