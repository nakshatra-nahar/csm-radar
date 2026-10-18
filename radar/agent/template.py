"""The deterministic brief: used without an API key, past the token budget, or when the model's
output fails validation twice. Same shape as validate.Brief."""

from radar.agent.evidence import EvidencePack

# (subject, opening) per play, written to the customer (Ruling AF): plain words, no numbers,
# no evidence quoted or cited. The evidence displays stay in the CSM-facing summary.
EMAILS = {
    "save_call": ("Can we talk this week?",
                  "I would like to hear how things are going for your team and what we could do"
                  " better. Could we set up a short call this week?"),
    "billing_outreach": ("A quick note about your account",
                         "It looks like a recent payment did not go through. If it helps, I am"
                         " happy to walk through the billing details with you."),
    "support_escalation": ("Following up on your support request",
                           "I saw that your recent support request is still open and I am"
                           " following it up with our team. Could we find a short slot to make"
                           " sure it gets resolved?"),
    "value_review": ("Getting more from the product",
                     "I would like to review with you how your team uses the product and where"
                     " we can help. Would a short call next week work?"),
    "exec_check_in": ("Checking in",
                      "I wanted to check in on how things are going and whether there is"
                      " anything we can help with. Would a short call suit you?"),
    "renewal_check_in": ("Planning ahead together",
                         "As we look ahead, I would like to hear what is working for your team"
                         " and what you would like to see next. Could we find a short slot to"
                         " talk?"),
}


def render(pack: EvidencePack) -> dict:
    """One summary point per system item, quoting its display string; customer text is left in
    the evidence list (never repeated). The email is the play's plain message with {first_name}
    and {sender_name} placeholders; find_new_champion gets no email."""
    summary = [{"evidence_ids": [i.id], "text": i.display}
               for i in pack.items if i.trust == "system"][:5]
    email = None
    if pack.play in EMAILS:
        subject, opening = EMAILS[pack.play]
        email = {"subject": subject, "evidence_ids": [],
                 "body": f"Hi {{first_name}},\n\n{opening}\n\nBest regards,\n{{sender_name}}"}
    return {"summary": summary, "email": email,
            "insufficient_evidence": len(summary) < 2, "injection_suspected": False}
