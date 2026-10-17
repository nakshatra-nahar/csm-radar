"""Stripe-style webhook signatures: `t=<unix>,kid=<key id>,v1=<hex HMAC-SHA256 of "<t>." + body>`.

The key id names the secret, so a secret can be rotated by adding a new id first.
"""

import hashlib
import hmac
import json


class WebhookError(Exception):
    pass


def body_bytes(body: dict) -> bytes:
    """The canonical bytes a signature covers (compact, key-sorted JSON)."""
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def _mac(secret: str, body: bytes, t: int) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def sign(secret: str, key_id: str, body: bytes, t: int) -> str:
    return f"t={t},kid={key_id},v1={_mac(secret, body, t)}"


def verify(header: str, body: bytes, secrets: dict[str, str], now: int,
           tolerance: int = 300) -> str:
    """Return the key id that signed `body`, or raise WebhookError."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        # bytes: compare_digest raises TypeError on non-ASCII str; a lone surrogate raises
        # UnicodeEncodeError, a ValueError
        t, kid, mac = int(parts["t"]), parts["kid"], parts["v1"].encode()
    except (KeyError, ValueError) as e:
        raise WebhookError("malformed signature header") from e
    if kid not in secrets:
        raise WebhookError(f"unknown key id {kid!r}")
    if not hmac.compare_digest(mac, _mac(secrets[kid], body, t).encode()):
        raise WebhookError("signature mismatch")
    if abs(now - t) > tolerance:  # a replayed old delivery, or a badly skewed sender
        raise WebhookError("timestamp outside tolerance")
    return kid


def stripe_secrets(slug: str, configured: dict[str, str]) -> dict[str, str]:
    """Only the tenant's own key id, so one tenant's secret never verifies another's records.
    Settings may configure its secret; otherwise the demo rule applies."""
    kid = f"{slug}-stripe"
    return {kid: configured.get(kid, f"whsec_demo_{slug}")}
