"""The deterministic brief: used without an API key, past the token budget, or when the model's
output fails validation twice. Same shape as validate.Brief."""

from radar.agent.evidence import EvidencePack

SIGN_OFF = "\n\nBest regards"
# (subject, body) per play; plain words and no numbers, so it passes the validator. Numbers
# reach the email only inside the customer-safe display strings it quotes and cites.
EMAILS = {
    "save_call": ("Can we talk this week?",
                  "Hi there,\n\nI would like to hear how things are going for your team and"
                  " what we could do better. Could we set up a short call this week?"),
    "billing_outreach": ("A quick note about your account",
                         "Hi there,\n\nIt looks like a recent payment did not go through. If it"
                         " helps, I am happy to walk through the billing details with you."),
    "support_escalation": ("Following up on your support request",
                           "Hi there,\n\nI saw that your recent support request is still open"
                           " and I am following it up with our team. Could we find a short slot"
                           " to make sure it gets resolved?"),
    "value_review": ("Getting more from the product",
                     "Hi there,\n\nI would like to review with you how your team uses the"
                     " product and where we can help. Would a short call next week work?"),
    "exec_check_in": ("Checking in",
                      "Hi there,\n\nI wanted to check in on how things are going and whether"
                      " there is anything we can help with. Would a short call suit you?"),
}


def render(pack: EvidencePack) -> dict:
    """One summary point per system item, quoting its display string; customer text is left in
    the evidence list (never repeated). The email quotes up to two customer-safe system items
    (an exact key to this account), and find_new_champion gets no email."""
    summary = [{"evidence_ids": [i.id], "text": i.display}
               for i in pack.items if i.trust == "system"][:5]
    email = None
    if pack.play in EMAILS:
        subject, body = EMAILS[pack.play]
        safe = [i for i in pack.items if i.customer_safe and i.trust == "system"][:2]
        if safe:
            body += "\n\nWhat I am looking at:\n" + "\n".join(f"- {i.display}" for i in safe)
        email = {"subject": subject, "body": body + SIGN_OFF,
                 "evidence_ids": [i.id for i in safe]}
    return {"summary": summary, "email": email,
            "insufficient_evidence": len(summary) < 2, "injection_suspected": False}
