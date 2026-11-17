"""ASGI app: the gateway's four routes.

POST /v1/chat/completions and POST /v1/embeddings forward to the upstream after the
key, rate and budget checks. GET /v1/models lists the key's models. GET /health
answers 200 for the Lambda Web Adapter's readiness check.
"""

import asyncio
import json
import logging
import time
import uuid

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import auth, budget, notify, pricing, settings, textlog, upstream
from .auth import GatewayError

log = logging.getLogger("gateway")
_background: set[asyncio.Task] = set()


def error_response(err: GatewayError, rid: str) -> JSONResponse:
    log.info("refused request_id=%s key_id=%s status=%d code=%s", rid, err.key_id or "-", err.status, err.code)
    body = {"error": {"message": err.message, "type": err.type, "code": err.code}}
    return JSONResponse(body, status_code=err.status, headers={"x-request-id": rid, **err.headers})


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def models(request: Request) -> Response:
    rid = str(uuid.uuid4())
    try:
        key = auth.parse_bearer(request.headers.get("authorization"))
        item = auth.lookup(key)
        try:
            auth.check_state(item)
        except GatewayError as e:
            e.key_id = item["key_id"]
            raise
    except GatewayError as e:
        return error_response(e, rid)
    data = [{"id": m, "object": "model", "owned_by": "upstream"} for m in sorted(item.get("allowed_models", []))]
    return JSONResponse({"object": "list", "data": data}, headers={"x-request-id": rid})


class Request_:
    """State of one forwarded request. `finish` settles exactly once."""

    def __init__(self, rid: str, item: dict, model: str, amount: int, tokens_in_est: int,
                 tokens_out_max: int = 0):
        self.rid = rid
        self.item = item
        self.model = model
        self.amount = amount
        self.tokens_in_est = tokens_in_est
        self.tokens_out_max = tokens_out_max
        self.settled = False
        self.cost_micro: int | None = None  # what the key was charged, once the settle has gone through
        self.t0 = time.monotonic()
        # for the text log: the body sent upstream, the path it went to, and the reply once one has been read
        self.body: dict | None = None
        self.endpoint = ""
        self.reply: dict | None = None

    def finish_at_reserve(self) -> None:
        """No usage from the upstream: keep the whole reserve as spent, marked estimated."""
        self.finish(self.tokens_in_est, self.tokens_out_max, estimated=True)

    def finish_failed(self, status: int, *, charge: bool) -> None:
        """No reply. `charge`: the upstream had the request (a timeout or a 5xx after it was sent), so the whole
        reserve stays as spent, since the provider may bill up to max_tokens. Otherwise the reserve goes back."""
        if charge:
            self.finish(self.tokens_in_est, self.tokens_out_max, failed=True, status=status)
        else:
            self.finish(0, 0, failed=True, refund=True, status=status)

    def finish(self, tokens_in: int, tokens_out: int, *, estimated=False, failed=False, refund=False,
               status=200) -> None:
        if self.settled:
            return
        self.settled = True
        textlog.write(self.rid, self.item, self.model, self.endpoint, status, self.body, self.reply)
        if refund:
            tokens_in = tokens_out = 0
        cost = 0 if refund else self.amount if failed else pricing.cost(self.model, tokens_in, tokens_out)
        try:
            new = budget.settle(self.item, self.model, self.amount, tokens_in, tokens_out,
                                estimated=estimated, failed=failed, refund=refund)
            notify.check_thresholds(new)
        except Exception as e:  # never let bookkeeping break the reply
            log.error("settle failed request_id=%s key_id=%s error=%s", self.rid, self.item["key_id"],
                      type(e).__name__)
            return
        self.cost_micro = cost
        log.info(
            "request_id=%s key_id=%s model=%s status=%s tokens_in=%d tokens_out=%d cost_micro=%d estimated=%s ms=%d",
            self.rid, self.item["key_id"], self.model, status, tokens_in, tokens_out, cost,
            estimated or (failed and not refund), (time.monotonic() - self.t0) * 1000,
        )


async def _admit(request: Request, rid: str, embeddings: bool) -> tuple[Request_, dict]:
    """Steps 2 to 6 of the request flow in the README. Raises GatewayError on refusal."""
    key = auth.parse_bearer(request.headers.get("authorization"))
    item = auth.lookup(key)
    try:
        return await _admit_key(request, rid, embeddings, item)
    except GatewayError as e:
        e.key_id = item["key_id"]  # the refusal's log line names the key, never its owner
        raise


async def _admit_key(request: Request, rid: str, embeddings: bool, item: dict) -> tuple[Request_, dict]:
    auth.check_state(item)
    max_body = int(item.get("max_body_bytes", settings.DEFAULT_MAX_BODY_BYTES))
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_body:
        raise GatewayError(413, f"request body over {max_body} bytes", "invalid_request_error", "body_too_large")
    raw = await request.body()
    if len(raw) > max_body:
        raise GatewayError(413, f"request body over {max_body} bytes", "invalid_request_error", "body_too_large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise GatewayError(400, "request body is not valid JSON", "invalid_request_error", "bad_json") from None
    if not isinstance(body, dict):
        raise GatewayError(400, "request body must be a JSON object", "invalid_request_error", "bad_json")
    model = body.get("model")
    if not isinstance(model, str):
        raise GatewayError(400, "model is missing or not a string", "invalid_request_error", "bad_model")
    served = pricing.EMBEDDING_MODELS if embeddings else pricing.CHAT_MODELS
    if model not in pricing.PRICES:
        auth.check_model(item, model)  # raises 403 with the allowed list
        raise GatewayError(403, f"model {model!r} is not known", "permission_error", "model_not_allowed")
    if model not in served:
        raise GatewayError(400, f"model {model!r} is not served on this endpoint", "invalid_request_error",
                           "wrong_endpoint")
    if not embeddings:
        if not isinstance(body.get("messages"), list):
            raise GatewayError(400, "messages must be a list", "invalid_request_error", "bad_messages")
        n = body.get("n", 1)
        if isinstance(n, bool) or n != 1:
            raise GatewayError(400, "n must be 1", "invalid_request_error", "n_not_supported")
        if "best_of" in body:
            raise GatewayError(400, "best_of is not supported", "invalid_request_error", "best_of_not_supported")
        cap = int(item.get("max_tokens_cap", settings.DEFAULT_MAX_TOKENS_CAP))
        if body.get("max_tokens") is None and body.get("max_completion_tokens") is None:
            body["max_tokens"] = min(settings.DEFAULT_MAX_TOKENS, cap)
        for k in ("max_tokens", "max_completion_tokens"):
            if body.get(k) is not None:
                if not isinstance(body[k], int) or isinstance(body[k], bool) or body[k] < 1:
                    raise GatewayError(400, f"{k} must be a positive integer", "invalid_request_error", "bad_max")
                body[k] = min(body[k], cap)
        if body.get("stream") is not None and not isinstance(body["stream"], bool):
            raise GatewayError(400, "stream must be true or false", "invalid_request_error", "bad_stream")
        if body.get("stream"):
            opts = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            body["stream_options"] = {**opts, "include_usage": True}
    budget.rate_limit(item["key_id"], int(item.get("max_rpm", settings.DEFAULT_MAX_RPM)))
    tokens_in_est = budget.input_tokens_estimate(body, embeddings)
    tokens_out_max = 0 if embeddings else budget.output_tokens_bound(body)
    amount = budget.reserve_amount(model, tokens_in_est, tokens_out_max)
    try:
        budget.reserve(item, model, amount)
    except GatewayError as e:
        if e.code == "budget_exhausted":
            notify.notify_refused(item)  # the 100 % notice: a refused reserve means the budget is used up
        raise
    return Request_(rid, item, model, amount, tokens_in_est, tokens_out_max), body


async def _proxy(request: Request, path: str, embeddings: bool) -> Response:
    rid = str(uuid.uuid4())
    try:
        req, body = await _admit(request, rid, embeddings)
    except GatewayError as e:
        return error_response(e, rid)
    req.body, req.endpoint = body, path
    if body.get("stream") and not embeddings:
        return await _stream(req, path, body)
    return await _buffered(req, path, body)


def _upstream_failed(req: Request_, e: BaseException, *, charge: bool) -> Response:
    """No reply from the upstream: settle (the reserve back, or kept when the request had been sent) and answer 502."""
    req.finish_failed(502, charge=charge)
    log.warning("upstream error request_id=%s error=%s charged=%s", req.rid, type(e).__name__, charge)
    return JSONResponse(upstream.UPSTREAM_FAILED, status_code=502, headers={"x-request-id": req.rid})


def _upstream_status(req: Request_, r: httpx.Response, content: bytes = b"") -> Response:
    """An error status from the upstream. A 5xx came after it had the request: the reserve stays as spent, except a
    503 or 529, where the provider turned the request away without generating anything. A 4xx gives the reserve
    back: a 401 or 403 (the gateway's provider key) answers 502, a 429 answers 429, any other 4xx answers 400 with
    the provider's message."""
    status, err, extra = upstream.error_for_status(r.status_code, r.headers, content)
    req.finish_failed(r.status_code, charge=r.status_code >= 500 and r.status_code not in upstream.NOT_TAKEN)
    if r.status_code in (401, 403):
        log.warning("upstream refused the provider key request_id=%s key_id=%s model=%s status=%d", req.rid,
                    req.item["key_id"], req.model, r.status_code)
    elif status == 400:
        log.info("upstream rejected request_id=%s key_id=%s model=%s status=%d", req.rid, req.item["key_id"],
                 req.model, r.status_code)
    return JSONResponse(err, status_code=status, headers={"x-request-id": req.rid, **extra})


async def _buffered(req: Request_, path: str, body: dict) -> Response:
    try:
        async with upstream.new_client() as client:
            try:
                request = upstream.build_request(client, path, body)
            except Exception as e:  # no upstream URL, or the secret read failed: nothing was sent
                return _upstream_failed(req, e, charge=False)
            r = await asyncio.wait_for(client.send(request), settings.load().total_timeout_s)
    except Exception as e:  # connection failed (not sent), or a timeout or a dropped connection after sending
        return _upstream_failed(req, e, charge=upstream.was_sent(e))
    if r.status_code >= 400:
        return _upstream_status(req, r, r.content)
    return _settle_buffered(req, r)


def _settle_buffered(req: Request_, r: httpx.Response) -> Response:
    """Settle a read 2xx reply from its usage, or at the reserve when it has none."""
    try:
        data = r.json()
    except ValueError:
        data = None
    usage = upstream.usage_from_body(data)
    req.reply = textlog.reply_from_body(data)
    if usage is None:
        req.finish_at_reserve()
    else:
        req.finish(*usage)
    headers = {"x-request-id": req.rid}
    if usage is not None and req.cost_micro is not None:
        # Tell the caller what this call was charged, in micro-units: the same number the settle wrote. Only for a
        # reply settled from its usage; a streamed reply's headers are sent before its cost is known. This header
        # reports the charge; it does not change it.
        headers["x-gateway-cost-micro"] = str(req.cost_micro)
    return Response(r.content, status_code=r.status_code, headers=headers,
                    media_type=r.headers.get("content-type", "application/json"))


class _StreamState:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.timeout_cm: asyncio.Timeout | None = None
        self.client_gone = False

    def disconnected(self) -> None:
        """Client left. Keep reading the upstream for the grace period, then settle."""
        if self.client_gone:
            return
        self.client_gone = True
        if self.timeout_cm is not None:
            loop = asyncio.get_running_loop()
            self.timeout_cm.reschedule(min(self.timeout_cm.when() or float("inf"),
                                           loop.time() + settings.load().disconnect_grace_s))


async def _stream(req: Request_, path: str, body: dict) -> Response:
    client = upstream.new_client()
    try:
        request = upstream.build_request(client, path, body)
    except Exception as e:  # no upstream URL, or the secret read failed: nothing was sent
        await client.aclose()
        return _upstream_failed(req, e, charge=False)
    try:
        r = await client.send(request, stream=True)
    except Exception as e:  # connection failed (not sent), or a timeout before the reply's headers (sent)
        await client.aclose()
        return _upstream_failed(req, e, charge=upstream.was_sent(e))
    if r.status_code >= 400:
        content = b""
        if 400 <= r.status_code < 500:  # the provider's message goes back to the caller: read the short error body
            try:
                content = await asyncio.wait_for(r.aread(), settings.load().read_timeout_s)
            except Exception:
                content = b""
        await r.aclose()
        await client.aclose()
        return _upstream_status(req, r, content)
    if not r.headers.get("content-type", "").startswith("text/event-stream"):
        # The upstream answered with one body, not SSE. Read and settle it as a buffered reply.
        try:
            await asyncio.wait_for(r.aread(), settings.load().total_timeout_s)
        except Exception as e:  # the reply had started: the request was sent
            return _upstream_failed(req, e, charge=True)
        finally:
            await r.aclose()
            await client.aclose()
        return _settle_buffered(req, r)

    state = _StreamState()
    reader = asyncio.create_task(_read_upstream(req, client, r, state))
    _background.add(reader)
    reader.add_done_callback(_background.discard)

    async def relay():
        finished = False
        try:
            while (chunk := await state.queue.get()) is not None:
                yield chunk
            await reader
            finished = True
        finally:
            if not finished:
                state.disconnected()

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"x-request-id": req.rid})


async def _read_upstream(req: Request_, client: httpx.AsyncClient, r: httpx.Response, state: _StreamState):
    """Read the upstream to the end and settle once in `finally`, whatever the client does."""
    meter = upstream.SSEMeter(keep_text=bool(settings.load().table_texts))
    broken = False
    try:
        async with asyncio.timeout_at(asyncio.get_running_loop().time() + settings.load().total_timeout_s) as cm:
            state.timeout_cm = cm
            if state.client_gone:
                state.client_gone = False
                state.disconnected()
            async for chunk in r.aiter_bytes():
                meter.feed(chunk)
                state.queue.put_nowait(chunk)  # passed on unchanged
    except Exception as e:  # httpx error, or the grace or total timeout
        broken = not state.client_gone
        log.warning("upstream stream stopped request_id=%s error=%s", req.rid, type(e).__name__)
    finally:
        await r.aclose()
        await client.aclose()
        if broken:
            state.queue.put_nowait(upstream.STREAM_ENDED_EARLY)
        req.reply = meter.reply()
        if meter.usage is not None:
            req.finish(*meter.usage)
        else:  # no usage chunk arrived: count the content chunks seen so far (an undercount, see the README)
            req.finish(req.tokens_in_est, meter.content_chunks, estimated=True)
        state.queue.put_nowait(None)


async def chat_completions(request: Request) -> Response:
    return await _proxy(request, "/chat/completions", embeddings=False)


async def embeddings(request: Request) -> Response:
    return await _proxy(request, "/embeddings", embeddings=True)


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/v1/models", models, methods=["GET"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/v1/embeddings", embeddings, methods=["POST"]),
])
# uvicorn sets up only its own loggers. Without a handler here Python prints WARNING and above and drops the rest.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)
