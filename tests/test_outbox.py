import pytest

from radar import rbac
from radar.agent import brief
from tests.conftest import add_case


def draft_of(owner_conn, brief_id):
    return owner_conn.execute("SELECT * FROM outbox WHERE brief_id = %s",
                              (brief_id,)).fetchone()


def approve(worker_db, w, outbox_id, user_id):
    with worker_db.tenant_tx(w["tid"], actor=f"user:{user_id}") as conn:
        brief.approve_and_send(conn, outbox_id, user_id)


def test_no_send_without_approval(owner_conn, worker_db, brief_world):
    w = brief_world
    draft = draft_of(owner_conn, brief.generate_brief(worker_db, w["tid"], w["case"], None))
    assert (draft["status"], draft["approved_by"], draft["sent_at"]) == ("draft", None, None)
    approve(worker_db, w, draft["id"], w["csm"])
    sent = owner_conn.execute("SELECT * FROM outbox WHERE id = %s", (draft["id"],)).fetchone()
    assert (sent["status"], sent["approved_by"], sent["transport"]) == ("sent", w["csm"],
                                                                         "dry_run")
    assert sent["approved_at"] is not None and sent["sent_at"] is not None
    with pytest.raises(ValueError, match="draft"):  # nothing is sent twice
        approve(worker_db, w, draft["id"], w["csm"])


def test_recipient_chosen_by_code_excludes_bounced_and_departed(owner_conn, worker_db,
                                                                brief_world):
    w = brief_world
    champion, backup = w["contacts"]
    owner_conn.execute("UPDATE contacts SET hard_bounced_at = now() WHERE id = %s", (champion,))
    draft = draft_of(owner_conn, brief.generate_brief(worker_db, w["tid"], w["case"], None))
    assert draft["to_contact_id"] == backup
    owner_conn.execute("UPDATE contacts SET departed = true WHERE id = %s", (backup,))
    with pytest.raises(ValueError, match="recipient"):  # re-checked at approval time
        approve(worker_db, w, draft["id"], w["csm"])
    other = add_case(owner_conn, w["tid"], w["account"], "cancel_announced", "save_call", [])
    brief_id = brief.generate_brief(worker_db, w["tid"], other, None)
    assert draft_of(owner_conn, brief_id) is None
    reason = owner_conn.execute("SELECT reason FROM audit_log WHERE action = 'draft_skipped'"
                                " AND entity_id = %s", (str(brief_id),)).fetchone()["reason"]
    assert reason == "no_allowed_recipient"


def test_find_new_champion_has_no_customer_draft(owner_conn, worker_db, brief_world):
    w = brief_world
    case = add_case(owner_conn, w["tid"], w["account"], "champion_bounced",
                    "find_new_champion", [])
    brief_id = brief.generate_brief(worker_db, w["tid"], case, None)
    assert draft_of(owner_conn, brief_id) is None
    assert owner_conn.execute("SELECT output FROM briefs WHERE id = %s",
                              (brief_id,)).fetchone()["output"]["email"] is None


def test_csm_cannot_approve_other_book(owner_conn, worker_db, brief_world):
    w = brief_world
    users = owner_conn.execute("SELECT id, role FROM users WHERE tenant_id = %s ORDER BY email",
                               (w["tid"],)).fetchall()
    other_csm = next(u["id"] for u in users if u["role"] == "csm" and u["id"] != w["csm"])
    manager = next(u["id"] for u in users if u["role"] == "manager")
    draft = draft_of(owner_conn, brief.generate_brief(worker_db, w["tid"], w["case"], None))
    with pytest.raises(PermissionError):
        approve(worker_db, w, draft["id"], other_csm)
    with pytest.raises(PermissionError):  # a manager acts only on their own team
        approve(worker_db, w, draft["id"], manager)
    owner_conn.execute("UPDATE users SET manager_id = %s WHERE id = %s", (manager, w["csm"]))
    approve(worker_db, w, draft["id"], manager)
    assert draft_of(owner_conn, draft["brief_id"])["approved_by"] == manager


def test_authorize_roles():
    csm, mgr, admin = ({"id": i, "role": r, "tenant_id": "t"}
                       for i, r in (("c1", "csm"), ("m1", "manager"), ("a1", "admin")))
    assert rbac.authorize(csm, "c1", set(), "approve_send")
    assert not rbac.authorize(csm, "c2", set(), "approve_send")
    assert rbac.authorize(mgr, "c2", {"c2"}, "approve_send")
    assert not rbac.authorize(mgr, "c3", {"c2"}, "approve_send")
    assert rbac.authorize(admin, "c3", set(), "approve_send")
    assert rbac.authorize(admin, None, set(), "promote_model")
    assert not rbac.authorize(mgr, "c2", {"c2"}, "promote_model")
    assert not rbac.authorize(csm, "c1", set(), "edit_config")
