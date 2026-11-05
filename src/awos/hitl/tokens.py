"""Signed one-time approval links.

Token  = <claims>.<sig>  (both urlsafe base64, no padding)
claims = {"v":1, "a":approval_id, "o":option|null, "e":exp_unix, "n":nonce, "p":payload_digest}
sig    = HMAC-SHA256(var/link_key, claims_b64)        # key: 32 random bytes, created on first use, never logged

Only sha256(token) is stored (approvals.token_hash), so there is a single active token per approval: signing a new
link invalidates the previous one, and the link dies as soon as the approval stops being `pending` (decided,
expired, cancelled) or its payload changes. Expiry is min(ttl_hours, approval.expires_at).

API usage: GET /approve/<token> must only verify() and render a confirm page (IM apps pre-fetch links);
POST commits with consume() + repo.decide_approval(..., via="link").
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .. import db
from ..config import load_config
from ..db import loads, parse_ts, q1
from ..models import ApprovalStatus

KEY_BYTES = 32
MAX_TOKEN_LEN = 2048


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def key_path() -> Path:
    return load_config().path("var") / "link_key"


def _key() -> bytes:
    p = key_path()
    if not p.exists():
        try:
            with p.open("xb") as f:  # exclusive create: concurrent first use cannot produce two keys
                f.write(secrets.token_bytes(KEY_BYTES))
        except FileExistsError:
            pass
    for _ in range(40):  # another process may be between create and write
        k = p.read_bytes()
        if len(k) >= KEY_BYTES:
            return k
        time.sleep(0.05)
    raise RuntimeError(f"{p} is corrupt (expected {KEY_BYTES} bytes); delete it to rotate (invalidates all links)")


def _mac(body: str) -> bytes:
    return hmac.new(_key(), body.encode("utf-8"), hashlib.sha256).digest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _payload_digest(raw_payload: str | None) -> str:
    return hashlib.sha256((raw_payload or "").encode("utf-8")).hexdigest()[:16]


def _row(approval_id: str) -> dict | None:
    return q1("SELECT id, status, options, payload, expires_at, token_hash FROM approvals WHERE id = ?", (approval_id,))


def sign(approval_id: str, option: str | None = None, ttl_hours: float = 24) -> str:
    """Create the (single) active link token for a pending approval. Raises KeyError / ValueError."""
    row = _row(approval_id)
    if row is None:
        raise KeyError(approval_id)
    if row["status"] != ApprovalStatus.PENDING:
        raise ValueError(f"approval {approval_id} is {row['status']}, not pending")
    if option is not None:
        keys = [o.get("key") for o in (loads(row["options"], []) or []) if isinstance(o, dict)]
        if option not in keys:
            raise ValueError(f"unknown option {option!r}; expected one of {keys}")
    exp = int(time.time() + ttl_hours * 3600)
    try:
        ap_exp = parse_ts(row["expires_at"])
    except ValueError:
        ap_exp = None
    if ap_exp is not None:
        exp = min(exp, int(ap_exp.timestamp()))
    claims = {"v": 1, "a": approval_id, "o": option, "e": exp, "n": _b64e(secrets.token_bytes(12)),
              "p": _payload_digest(row["payload"])}
    body = _b64e(json.dumps(claims, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
    token = f"{body}.{_b64e(_mac(body))}"
    n = db.execute("UPDATE approvals SET token_hash = ? WHERE id = ? AND status = ?",
                   (token_hash(token), approval_id, str(ApprovalStatus.PENDING)))
    if not n:
        raise ValueError(f"approval {approval_id} is no longer pending")
    return token


def verify(token: str) -> tuple[str, str | None]:
    """Return (approval_id, option|None) for a valid, unexpired, current token of a pending approval.

    Raises ValueError on malformed token / bad signature / expired / approval not pending / superseded or used.
    """
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN or token.count(".") != 1:
        raise ValueError("malformed token")
    body, sig = token.split(".")
    try:
        given = _b64d(sig)
        claims = json.loads(_b64d(body))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError("malformed token") from None
    if not hmac.compare_digest(given, _mac(body)):
        raise ValueError("bad signature")
    if not isinstance(claims, dict) or claims.get("v") != 1 or not isinstance(claims.get("a"), str):
        raise ValueError("malformed token")
    if not isinstance(claims.get("e"), int) or time.time() >= claims["e"]:
        raise ValueError("link expired")
    row = _row(claims["a"])
    if row is None:
        raise ValueError("unknown approval")
    if row["status"] != ApprovalStatus.PENDING:
        raise ValueError(f"approval is {row['status']}, not pending")
    if not hmac.compare_digest(str(row["token_hash"] or ""), token_hash(token)):
        raise ValueError("link superseded or already used")
    if not hmac.compare_digest(str(claims.get("p") or ""), _payload_digest(row["payload"])):
        raise ValueError("approval changed since the link was issued")
    option = claims.get("o")
    return row["id"], option if isinstance(option, str) else None


def consume(token: str) -> tuple[str, str | None]:
    """verify() + atomically burn the token (compare-and-set), so concurrent POSTs cannot both pass."""
    approval_id, option = verify(token)
    n = db.execute("UPDATE approvals SET token_hash = NULL WHERE id = ? AND token_hash = ? AND status = ?",
                   (approval_id, token_hash(token), str(ApprovalStatus.PENDING)))
    if not n:
        raise ValueError("link superseded or already used")
    return approval_id, option


def revoke(approval_id: str) -> None:
    db.update("approvals", "id", approval_id, {"token_hash": None})


def approval_link(approval_id: str, option: str | None = None, ttl_hours: float = 24) -> str:
    """Full one-time URL: http://127.0.0.1:<port>/approve/<token> (invalidates any earlier link for this approval)."""
    from .notify import base_url

    return f"{base_url()}/approve/{sign(approval_id, option, ttl_hours)}"
