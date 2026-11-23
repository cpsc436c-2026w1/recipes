"""A refusal of the course LLM gateway, read the same way under both SDKs (README, Decisions: no wait on Retry-After).

Strands raises ModelThrottledException for a 429 with the OpenAI error as its cause, and the OpenAI error itself for
the other refusals; Pydantic AI raises a ModelAPIError subclass (ModelQuotaExceededError, ModelRateLimitError,
ModelAuthenticationError, ModelServerError) with the OpenAI error as its cause. Either way the OpenAI
`APIStatusError` is in the exception's chain, with the HTTP status, the body's `code` and the response headers.
"""
from __future__ import annotations

from openai import APIStatusError

REFUSED = (401, 403, 429)       # and every 5xx


def gateway_refusal(err: BaseException) -> dict | None:
    """{"http_status", "code", "retry_after"} when the gateway refused a model call, else None."""
    seen, e = set(), err
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, APIStatusError) and (e.status_code in REFUSED or e.status_code >= 500):
            body = e.body if isinstance(e.body, dict) else {}
            body = body.get("error", body) if isinstance(body.get("error"), dict) else body
            retry = e.response.headers.get("retry-after") if e.response is not None else None
            return {"http_status": e.status_code, "code": body.get("code") or "unknown",
                    "retry_after": int(retry) if retry and retry.isdigit() else None}
        e = e.__cause__ or e.__context__
    return None
