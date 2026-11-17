"""Extension ext-prompt-cache: does the endpoint report cached input tokens when the same long prompt comes twice?

README.md in this folder says what it adds to main/ and why.
"""

from endpoint import failure, row

# About 3,300 tokens of fixed text (11,722 characters), long enough for a provider's prompt cache to apply.
# Decision: the fixed part first and the question last, the order a cache can reuse.
LONG_SYSTEM = "You answer questions about a taxi trips table.\n" + "\n".join(
    f"Rule {i}: column number {i} of the trips table is documented in section {i} of the data dictionary."
    for i in range(1, 121)
)


def probe_prompt_cache(ep, model):
    """The same long system prompt twice: does the second call report cached input tokens?"""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": LONG_SYSTEM}, {"role": "user", "content": "Which section documents column 7?"}],
        "max_tokens": 200,
        "temperature": 0,
    }
    took, cached, prompt = [], None, None
    for _ in range(2):
        status, reply, seconds = ep.chat(body)
        took.append(seconds)
        if status != 200:
            return [row("prompt cache reported", model, "no", sum(took), failure(status, reply))]
        usage = reply.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    note = f"second call: {cached} of {prompt} input tokens cached; {took[0]:.2f} s then {took[1]:.2f} s"
    return [row("prompt cache reported", model, "yes" if cached else "no", sum(took), note)]


PROBES = [probe_prompt_cache]
