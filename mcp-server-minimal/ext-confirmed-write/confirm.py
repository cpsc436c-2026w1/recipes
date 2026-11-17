"""Confirmation codes: a person approves one exact change, and only that change (ext README, "Decisions").

A code is `<approver>.<expiry>.<16 hex digits>`: the approver's name, the time the code stops working (seconds
since 1970, UTC) and an HMAC-SHA256 over the approver, the expiry and the change (request id, order id, amount,
reason) under APPROVAL_SECRET. The approval script and the server hold the secret; the model never does. So the
model cannot make a code, a code for one change does not fit another (change the amount by a cent and the HMAC no
longer matches), and nobody can move the expiry: it is part of what is signed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass

APPROVER = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
APPROVAL_TTL_S = 900      # ours: a code works for 15 minutes after the person made it


@dataclass(frozen=True)
class Refund:
    request_id: str
    order_id: int
    amount_cad: float
    reason: str

    def canonical(self) -> str:
        # one fixed text per change: sorted keys, amount rounded to cents, so the same change always signs the same
        d = asdict(self)
        d["amount_cad"] = f"{self.amount_cad:.2f}"
        return json.dumps(d, sort_keys=True, separators=(",", ":"))


def make_code(secret: str, approver: str, refund: Refund, expires_at: int | None = None) -> str:
    """The code for one change. expires_at: when it stops working (default: APPROVAL_TTL_S from now)."""
    if not APPROVER.match(approver):
        raise ValueError("approver: 2 to 31 lowercase letters, digits or dashes, starting with a letter")
    expires_at = int(time.time()) + APPROVAL_TTL_S if expires_at is None else int(expires_at)
    return f"{approver}.{expires_at}.{_mac(secret, approver, expires_at, refund)}"


def _mac(secret: str, approver: str, expires_at: int, refund: Refund) -> str:
    text = f"{approver}|{expires_at}|{refund.canonical()}"
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()[:16]


def read_code(secret: str, code: str, refund: Refund, now: float | None = None) -> tuple[str | None, bool]:
    """(approver, expired). approver is the name when the code was made for this change, else None. expired is
    True when such a code is past its time: the server then refuses it and says so."""
    approver, _, rest = (code or "").partition(".")
    expiry, _, mac = rest.partition(".")
    if not APPROVER.match(approver) or not expiry.isascii() or not expiry.isdigit() or len(expiry) > 12:
        return None, False
    # bytes, not str: compare_digest raises TypeError on a str with a non-ASCII character, and the code comes
    # from the model
    if not hmac.compare_digest(_mac(secret, approver, int(expiry), refund).encode(), mac.encode()):
        return None, False
    return approver, (time.time() if now is None else now) >= int(expiry)


def check_code(secret: str, code: str, refund: Refund, now: float | None = None) -> str | None:
    """Returns the approver's name when the code fits this change and has not expired, else None."""
    approver, expired = read_code(secret, code, refund, now)
    return None if expired else approver
