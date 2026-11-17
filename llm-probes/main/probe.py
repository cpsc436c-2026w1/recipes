"""Probe an OpenAI-compatible LLM endpoint: which models answer, and which features work.

    export LLM_BASE_URL=https://your-endpoint.example/v1
    read -s LLM_API_KEY && export LLM_API_KEY     # from the environment only, never a command-line flag
    uv run python main/probe.py                   # the main scenario and every extension
    uv run python main/probe.py --groups main     # the main scenario only: models, wrong key, unknown model, chat

Every probe is one small HTTP request, written out in full so you can copy it into your own code. The main scenario
is in this file; each capability group is one ext-*/ folder. The script prints a table and changes nothing on the
endpoint. One run of every group used about 15,000 tokens in the captured run (README, "Cost and clean-up").
"""

import argparse
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from endpoint import Endpoint, failure, mask, one_word, row, text_of, tokens  # noqa: E402

# Capability groups, in the order they run for each chat model: folder and module. Each module has PROBES.
# Decision (extensions): one folder per capability, so a team copies only the probes its design depends on.
CHAT_GROUPS = {
    "streaming": ("ext-streaming", "streaming"),
    "reply-control": ("ext-reply-control", "reply_control"),
    "tool-calls": ("ext-tool-calls", "tool_calls"),
    "structured-output": ("ext-structured-output", "structured_output"),
    "prompt-cache": ("ext-prompt-cache", "prompt_cache"),
}
EMBEDDING_GROUP = {"embeddings": ("ext-embeddings", "embeddings")}
ALL_GROUPS = ["main", *CHAT_GROUPS, *EMBEDDING_GROUP]


def load(folder, module):
    sys.path.insert(0, str(HERE.parent / folder))
    return importlib.import_module(module)


def probe_models(ep):
    status, body, seconds = ep.request("GET", "/models")
    if status != 200:
        return [], [row("list models", "", "no", seconds, failure(status, body))]
    names = sorted(model["id"] for model in body.get("data", []))
    if not names:
        # A list with nothing in it is not a list of models: the key may see none, or the body is not the format.
        return [], [row("list models", "", "no", seconds, "the endpoint lists no models for this key")]
    return names, [row("list models", "", "yes", seconds, ", ".join(names))]


def probe_wrong_key(ep):
    # Decision (refusals first): an endpoint that answers a made-up key answers anyone.
    status, body, seconds = ep.request("GET", "/models", api_key="not-a-real-key")
    works = "yes" if status in (401, 403) else "no"
    return [row("wrong key is refused", "", works, seconds, failure(status, body))]


def probe_unknown_model(ep):
    # Decision (refusals first): a typo in a model name must fail, not be answered by some other model.
    status, body, seconds = ep.chat(one_word("no-such-model"))
    works = "yes" if status is not None and 400 <= status < 500 else "no"
    return [row("unknown model is refused", "", works, seconds, failure(status, body))]


def probe_chat(ep, model):
    status, reply, seconds = ep.chat(one_word(model))
    if status != 200:
        return [row("chat", model, "no", seconds, failure(status, reply))]
    text = text_of(reply)
    note = f"replied {text[:30]!r}; {tokens(reply)}" if text else f"empty reply; {tokens(reply)}"
    return [row("chat", model, "yes" if text else "no", seconds, note)]


def attempt(probe, ep, model):
    """The probe's rows. A reply with status 200 that is not in the expected format (no `choices`, a stream chunk
    that is not JSON) is one `no` row that says so, and the run goes on with the next probe."""
    try:
        return probe(ep, model)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        name = probe.__name__.removeprefix("probe_").replace("_", " ")
        return [row(name, model, "no", 0, f"malformed reply ({type(error).__name__})")]


def run(base_url, api_key, timeout=60.0, only=None, groups=None, transport=None, totals=None):
    """Run the main scenario and the chosen groups (default: all). Returns (rows, headers seen on the replies).
    `totals`, when given, is a dict that gets the run's token counts: tokens_in and tokens_out."""
    rows, ep = _run(base_url, api_key, timeout, only, groups, transport)
    if totals is not None:
        totals.update(tokens_in=ep.tokens_in, tokens_out=ep.tokens_out)
    return rows, ep.headers_seen


def _run(base_url, api_key, timeout, only, groups, transport):
    groups = set(groups or ALL_GROUPS)
    chat_probes = [p for g, (folder, mod) in CHAT_GROUPS.items() if g in groups for p in load(folder, mod).PROBES]
    embed_probes = load(*EMBEDDING_GROUP["embeddings"]).PROBES if "embeddings" in groups else []
    ep = Endpoint(base_url, api_key, timeout, transport=transport)
    models, rows = probe_models(ep)
    # Decision (stop early): without a model list the URL or the key is wrong, and every other probe would fail.
    if not models:
        return rows, ep
    rows += probe_wrong_key(ep) + probe_unknown_model(ep)
    for model in models:
        if only and model not in only:
            continue
        if "embed" in model:
            for probe in embed_probes:
                rows += attempt(probe, ep, model)
            continue
        first = attempt(probe_chat, ep, model)
        rows += first
        if first[0]["works"] != "yes":
            rows.append(row("other chat probes", model, "skipped", 0, "the model did not answer the first request"))
            continue
        for probe in chat_probes:
            rows += attempt(probe, ep, model)
    return rows, ep


def render(rows, headers_seen, base_url, api_key, timeout):
    host = httpx.URL(base_url).host
    lines = [
        f"Endpoint {host}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, {timeout:.0f} s timeout per request",
        "",
        "| probe | model | works | seconds | note |",
        "|---|---|---|---:|---|",
    ]
    for r in rows:
        # Decision (keys): masked first, as the text came; only then are "|" and line breaks taken out for the
        # table. The other way round a key with a "|" in it would no longer match.
        note = mask(str(r["note"]), api_key).replace("|", "/").replace("\n", " ")
        lines.append(f"| {r['probe']} | {r['model']} | {r['works']} | {r['seconds']:.2f} | {note} |")
    if headers_seen:
        lines += ["", "Headers on the last replies:"] + [f"  {k}: {mask(str(v), api_key)}"
                                                        for k, v in sorted(headers_seen.items())]
    return "\n".join(lines)


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for one reply")
    parser.add_argument("--models", help="comma-separated model names; default is every model the endpoint lists")
    parser.add_argument("--groups", help=f"comma-separated groups; default all of: {', '.join(ALL_GROUPS)}")
    parser.add_argument("--json", help="also write the rows to this file")
    parser.add_argument("--require", help="comma-separated probe names (the first column) your app depends on: "
                                          "exit with code 1 when one of them does not say yes")
    args = parser.parse_args(argv)
    # Decision (keys): one place for each value, the environment. No flag and no second variable name: a key
    # for another service that happens to be set must not be sent to this endpoint.
    base_url, api_key = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY")
    if not base_url or not api_key:
        sys.exit("Set LLM_BASE_URL and LLM_API_KEY in the environment first.")
    groups = args.groups.split(",") if args.groups else None
    unknown = set(groups or []) - set(ALL_GROUPS)
    if unknown:
        sys.exit(f"Unknown group {', '.join(sorted(unknown))}. Groups: {', '.join(ALL_GROUPS)}")

    only = args.models.split(",") if args.models else None
    totals = {}
    rows, headers_seen = run(base_url, api_key, args.timeout, only, groups, transport, totals=totals)
    print(render(rows, headers_seen, base_url, api_key, args.timeout))
    # Decision (small requests): say what the run used, so the claim "it can run before every demo" can be checked
    # against the key's budget. Only what the endpoint reported: a failed request or a reply without usage adds 0.
    print(f"\nTokens the endpoint reported for this run: {totals.get('tokens_in', 0)} in, "
          f"{totals.get('tokens_out', 0)} out")
    if args.json:
        with open(args.json, "w") as out:
            # Decision (keys): the same mask as on the screen; a row's note can carry an error that echoes the key.
            # Masked in the values, before encoding: json.dumps escapes a key with a quote, a backslash or a
            # non-ASCII character, and the escaped text no longer contains the key as written.
            out.write(json.dumps(mask(rows, api_key), indent=2))
    # Decision (a gate before a demo or a deploy): the rows this app depends on decide the exit code, so a script
    # can stop on it. A required row that was not run at all (its group was left out) fails too.
    missing = failed_requirements(rows, [name.strip() for name in (args.require or "").split(",") if name.strip()])
    if missing:
        print("required and not yes: " + "; ".join(missing))
    return 0 if rows[0]["works"] == "yes" and not missing else 1


def failed_requirements(rows, required):
    """One text per required probe and model without a `yes` row, or per probe that did not run at all.

    A probe of the chat groups is owed by every chat model of the run: a model whose first request failed has
    no row for it (its other probes were skipped), and that is a failure of the requirement, not a pass."""
    chat_models = [r["model"] for r in rows if r["probe"] == "chat"]
    failed = []
    for name in required:
        found = {r["model"]: r["works"] for r in rows if r["probe"] == name}
        if not found:
            failed.append(f"{name} (not run)")
            continue
        owed = chat_models if set(found) & set(chat_models) else []
        for model in list(found) + [m for m in owed if m not in found]:
            works = found.get(model, "not run")
            if works != "yes":
                failed.append(f"{name} ({model or 'endpoint'}: {works})")
    return failed


if __name__ == "__main__":
    sys.exit(main())
