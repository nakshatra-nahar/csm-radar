"""Authorization (design §9, Ruling G): one function for the agent's approvals and the web app.
Tenant scoping is not checked here: callers load the account inside the user's tenant_tx, so
RLS has already hidden every other tenant's rows."""

ADMIN_ONLY = {"edit_config", "promote_model", "view_ops", "ack_incident"}


def authorize(user: dict, account_owner_id, team_ids: set, action: str) -> bool:
    """A CSM acts on accounts they own, a manager on accounts owned by their team (team_ids:
    the CSMs they manage), an admin on any account of the tenant and on config and promotion."""
    if user["role"] == "admin":
        return True
    if action in ADMIN_ONLY:
        return False
    if user["role"] == "csm":
        return account_owner_id is not None and account_owner_id == user["id"]
    if user["role"] == "manager":
        return account_owner_id in team_ids
    return False


def team_ids(conn, user_id) -> set:
    """The ids of the users this user manages directly."""
    return {r["id"] for r in conn.execute("SELECT id FROM users WHERE manager_id = %s AND active",
                                          (user_id,)).fetchall()}
