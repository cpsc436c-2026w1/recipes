"""Extension ext-streaming: a streamed reply, its time to the first token, and whether usage arrives in the stream.

README.md in this folder says what it adds to main/ and why.
"""

import json
import time

import httpx
from endpoint import failure, mask, one_word, row, tokens


def probe_stream(ep, model):
    """Streamed reply: time to the first token, and whether usage arrives at the end of the stream."""
    # Decision: ask for usage in the stream; without it, a streamed reply's tokens cannot be counted.
    body = one_word(model) | {"stream": True, "stream_options": {"include_usage": True}}
    started = time.monotonic()
    first_token, usage, text, reasoning_chunks = None, None, "", 0
    try:
        with ep.client.stream(
            "POST",
            ep.base_url + "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {ep.api_key}"},
        ) as response:
            if response.status_code != 200:
                response.read()
                # masked before failure() cuts it, as Endpoint.request does for the other probes
                return [row("streaming", model, "no", time.monotonic() - started,
                            failure(response.status_code, mask(response.text, ep.api_key)))]
            for line in response.iter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                chunk = json.loads(line[5:])
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content") or delta.get("reasoning"):
                        reasoning_chunks += 1
                    if delta.get("content"):
                        first_token = first_token or time.monotonic() - started
                        text += delta["content"]
    except httpx.HTTPError as error:
        return [row("streaming", model, "no", time.monotonic() - started, f"no reply ({type(error).__name__})")]
    ep.count(usage)          # the stream's last chunk: the run's token total includes streamed replies
    seconds = time.monotonic() - started
    note = f"first token after {first_token:.2f} s" if first_token else "no text arrived"
    if reasoning_chunks:
        note += f"; {reasoning_chunks} reasoning chunks came first"
    return [
        row("streaming", model, "yes" if text.strip() else "no", seconds, note),
        row("usage in the stream", model, "yes" if usage else "no", seconds, tokens({"usage": usage})),
    ]


PROBES = [probe_stream]
