"""Extension ext-reply-control: does temperature 0 give the same reply twice, and does max_tokens stop a reply?

README.md in this folder says what it adds to main/ and why.
"""

from endpoint import failure, one_word, row, text_of


def probe_repeat(ep, model):
    """The same request twice at temperature 0: is the text the same?"""
    texts, seconds = [], 0.0
    for _ in range(2):
        status, reply, took = ep.chat(one_word(model))
        seconds += took
        if status != 200:
            return [row("same reply on repeat", model, "no", seconds, failure(status, reply))]
        texts.append(text_of(reply))
    same = texts[0] == texts[1]
    return [row("same reply on repeat", model, "yes" if same else "no", seconds, "temperature 0, two calls")]


def probe_max_tokens(ep, model):
    # Decision: a limit of 5 tokens on a reply that needs about 100, so the stop is plain to see.
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Count from 1 to 50, one number per line."}],
        "max_tokens": 5,
        "temperature": 0,
    }
    status, reply, seconds = ep.chat(body)
    if status != 200:
        return [row("max_tokens stops the reply", model, "no", seconds, failure(status, reply))]
    reason = reply["choices"][0].get("finish_reason")
    out = (reply.get("usage") or {}).get("completion_tokens")
    works = "yes" if reason == "length" and (out is None or out <= 5) else "no"
    note = f"finish_reason {reason}, {out} tokens out"
    if not text_of(reply):
        note += ", all of them reasoning: the reply text is empty"
    return [row("max_tokens stops the reply", model, works, seconds, note)]


PROBES = [probe_repeat, probe_max_tokens]
