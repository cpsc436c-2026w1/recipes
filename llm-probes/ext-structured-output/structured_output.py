"""Extension ext-structured-output: does the reply fit a JSON schema, even when the prompt asks for something else?

README.md in this folder says what it adds to main/ and why.
"""

import json

from endpoint import failure, row, text_of

CITY_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "population": {"type": "integer"}},
    "required": ["city", "population"],
    "additionalProperties": False,
}


def fits_city_schema(text):
    try:
        parsed = json.loads(text)
        return set(parsed) == {"city", "population"} and isinstance(parsed["population"], int)
    except (ValueError, TypeError):
        return False


def probe_json_schema(ep, model):
    """Structured output: the reply must be JSON that fits a schema we send.

    The second request asks for a plain sentence. If JSON still comes back, the endpoint enforces the schema
    while the model generates; if a sentence comes back, the schema is only a request the model may ignore.
    """
    # Decision: the second request pits the prompt against the schema, to tell enforced from requested.
    asks = {
        "output fits a JSON schema": "Extract the city and its population.",
        "schema wins over the prompt": "Reply with one plain English sentence, not JSON.",
    }
    rows = []
    for name, ask in asks.items():
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Vancouver has about 660 thousand residents. " + ask}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "city", "strict": True, "schema": CITY_SCHEMA}},
            "max_tokens": 400,
            "temperature": 0,
        }
        status, reply, seconds = ep.chat(body)
        if status != 200:
            rows.append(row(name, model, "no", seconds, failure(status, reply)))
            continue
        text = " ".join(text_of(reply).split())
        rows.append(row(name, model, "yes" if fits_city_schema(text) else "no", seconds, f"replied {text[:70]!r}"))
    return rows


PROBES = [probe_json_schema]
