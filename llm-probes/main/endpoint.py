"""One OpenAI-compatible endpoint, and the helpers every probe shares (main/ and each ext-*/ folder import this).

README.md, "Decisions", says why the probes are built this way; the decision is named in the comment beside it.
"""

import time

import httpx

# Reply headers worth showing. A LiteLLM proxy reports its version, what the call was billed and the key's spend.
SHOWN_HEADERS = {
    "server",
    "retry-after",
    "x-litellm-version",
    "x-litellm-attempted-retries",
    "x-litellm-attempted-fallbacks",
    "x-litellm-response-cost",
    "x-litellm-key-spend",
    "x-litellm-key-max-budget",
}


MIN_MASKED = 8   # a piece of the key this long or longer is masked too; shorter pieces match ordinary words


def mask(value, api_key: str):
    """`value` (a string, a dict, a list) with the key, and every piece of it of MIN_MASKED characters or more,
    replaced by [key]. A proxy may echo the key back whole, or cut: "expected sk-..., received abcd1234".
    A key shorter than MIN_MASKED characters is not masked: it would match parts of ordinary words."""
    if isinstance(value, dict):
        return {mask(k, api_key): mask(v, api_key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v, api_key) for v in value]
    if not isinstance(value, str) or len(api_key) < MIN_MASKED:
        return value
    for length in range(len(api_key), MIN_MASKED - 1, -1):          # the longest piece first
        for start in range(len(api_key) - length + 1):
            piece = api_key[start:start + length]
            if piece in value:
                value = value.replace(piece, "[key]")
    return value


class Endpoint:
    def __init__(self, base_url, api_key, timeout, transport=None):
        self.base_url = base_url.rstrip("/")
        # Decision (keys): the key comes from the environment (main/probe.py) and goes only into this header.
        self.api_key = api_key
        self.client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)
        self.timeout = timeout
        self.headers_seen = {}
        self.tokens_in = self.tokens_out = 0     # what the endpoint reported for this run's requests

    def count(self, usage):
        """Add one reply's token counts to the run's total. A reply without usage adds nothing."""
        if isinstance(usage, dict):
            out = usage.get("completion_tokens") or 0
            # A chat reply has prompt_tokens, completion_tokens and total_tokens; an embeddings reply has
            # prompt_tokens and total_tokens. Where only the total is given, the input is the total less the output.
            given = usage.get("prompt_tokens")
            self.tokens_in += given if given is not None else max((usage.get("total_tokens") or 0) - out, 0)
            self.tokens_out += out

    def request(self, method, path, body=None, api_key=None):
        """One request. Returns (status, body, seconds). Status is None when nothing came back in time."""
        started = time.monotonic()
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                json=body,
                headers={"Authorization": f"Bearer {api_key or self.api_key}"},
            )
        except httpx.HTTPError as error:
            return None, type(error).__name__, time.monotonic() - started
        seconds = time.monotonic() - started
        for name, value in response.headers.items():
            if name in SHOWN_HEADERS or name.startswith("x-ratelimit"):
                self.headers_seen[name] = mask(value, self.api_key)
        # Decision (keys): masked here, where the reply arrives and before any probe cuts or reformats its text.
        # A note keeps 110 characters of an error: a key that straddles that cut would otherwise leave a piece.
        try:
            body = response.json()
        except ValueError:
            return response.status_code, mask(response.text, self.api_key), seconds
        if isinstance(body, dict):
            self.count(body.get("usage"))
        return response.status_code, mask(body, self.api_key), seconds

    def chat(self, body):
        return self.request("POST", "/chat/completions", body)


def row(probe, model, works, seconds, note):
    return {"probe": probe, "model": model, "works": works, "seconds": round(seconds, 2), "note": note}


def failure(status, body):
    """A short reason for a request that did not return 200."""
    if status is None:
        return f"no reply ({body})"
    if isinstance(body, dict):
        error = body.get("error", body)
        body = error.get("message", error) if isinstance(error, dict) else error
    text = str(body)
    return f"HTTP {status}: {text[:110]}" + ("..." if len(text) > 110 else "")


def text_of(reply):
    return (reply["choices"][0]["message"].get("content") or "").strip()


def tokens(reply):
    usage = reply.get("usage") or {}
    if not usage:
        return "no usage in the reply"
    note = f"{usage.get('prompt_tokens')} tokens in, {usage.get('completion_tokens')} out"
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return note + (f", {reasoning} of them reasoning" if reasoning else "")


def one_word(model):
    # Decision (small requests): a one-word reply at temperature 0, so a probe uses a few dozen tokens.
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You answer with one word."},
            {"role": "user", "content": "Reply with the word ready."},
        ],
        "max_tokens": 200,
        "temperature": 0,
    }
