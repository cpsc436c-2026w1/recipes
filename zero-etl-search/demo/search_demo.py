import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="Search that follows the table")


@app.cell
def _():
    import sys
    import time

    import marimo as mo

    sys.path.insert(0, str(mo.notebook_dir().parent))   # the recipe folder: main/ is importable

    from main.config import Settings
    from main.envs import AwsEnv, LocalEnv
    from main.infra import _decimals as to_decimals
    from main.mappings import keyword_query
    from main.run import shows

    return AwsEnv, LocalEnv, Settings, keyword_query, mo, shows, time, to_decimals


@app.cell
def _(AwsEnv, LocalEnv, Settings, mo):
    target = mo.cli_args().get("target", "local")
    s = Settings()
    if target == "aws":
        env = AwsEnv(s)        # what `uv run python -m main.run --target aws` created; clean_up.py removes it
    else:
        env = LocalEnv(s)      # moto table and stream, stand-in mapping and index: no account needed
        env.deliver()          # the starting items
    where = (f"your sandbox ({s.region}): table `{s.table}`, function `{s.function}`, index `{s.index}`"
             if target == "aws" else "offline: a moto table and stream, a stand-in for Lambda's mapping and the index")
    mo.md(f"""
    # Search that follows the table

    The app writes to the DynamoDB table only. Every change goes onto the table's stream; Lambda reads the stream
    and calls the indexing function, which writes the change into the OpenSearch index under the item's key.
    Below: write or change an item, then refresh and see when the change is searchable.

    Running against **{where}**.
    {"Offline there is no real delay: a change reaches the index when you press **Refresh** (that hands the stream to the function). On AWS, Lambda delivers it on its own; Refresh only searches again." if target != "aws" else "Lambda delivers each change on its own; **Refresh** searches again, and **Wait until searchable** times it."}
    """)
    return env, s, target


@app.cell
def _(mo):
    form = mo.md("""
    **Write to the table** (the source of truth)

    {action} {item_id}

    {name} {description}

    {category} {brand} {price_cad} {in_stock}

    Try a price of `free`: the index's mapping takes only numbers for `price_cad`, so the function's write is refused
    and, after the retries, the change ends in the failure queue. The next change still arrives.
    """).batch(
        action=mo.ui.dropdown(["put", "delete"], value="put", label="action"),
        item_id=mo.ui.text(value="I09", label="id"),
        name=mo.ui.text(value="Camp stove", label="name"),
        description=mo.ui.text(value="Compact waterproof gas stove", label="description", full_width=True),
        category=mo.ui.text(value="gear", label="category"),
        brand=mo.ui.text(value="Cascade", label="brand"),
        price_cad=mo.ui.text(value="79", label="price_cad"),
        in_stock=mo.ui.checkbox(value=True, label="in stock"),
    ).form(submit_button_label="Write to DynamoDB")
    form
    return (form,)


@app.cell
def _(env, form, mo, time, to_decimals):
    last_write = None
    if form.value:
        _v = form.value
        try:
            _price = float(_v["price_cad"])
        except ValueError:
            _price = _v["price_cad"]                 # kept as text: the index will refuse it
        if _v["action"] == "delete":
            env.table.delete_item(Key={"id": _v["item_id"]})
            _attrs = None
        else:
            _attrs = {k: _v[k] for k in ("name", "description", "category", "brand", "in_stock")}
            _attrs["price_cad"] = _price
            env.table.put_item(Item=to_decimals({"id": _v["item_id"], **_attrs}))
        last_write = {"id": _v["item_id"], "attrs": _attrs, "at": time.time(), "seen_after": None}
        _out = mo.md(f"Wrote `{_v['action']} {_v['item_id']}` to the table at {time.strftime('%H:%M:%S')}.")
    else:
        _out = mo.md("No write yet in this session.")
    _out
    return (last_write,)


@app.cell
def _(mo):
    refresh = mo.ui.run_button(label="Refresh")
    wait = mo.ui.run_button(label="Wait until searchable (up to 2 minutes)")
    mo.hstack([refresh, wait], justify="start")
    return refresh, wait


@app.cell
def _(env, last_write, mo, refresh, shows, time, wait):
    refresh, wait                                   # rerun this cell on either button
    _delivered = env.deliver() if (refresh.value or wait.value) else {}
    if last_write is None:
        _status = "Nothing written yet."
    else:
        _deadline = time.time() + (120 if (wait.value and env.live) else 0)
        while True:
            _hit = shows(env, last_write["id"], last_write["attrs"])
            if _hit or time.time() >= _deadline:
                break
            time.sleep(0.5)
        if _hit and last_write["seen_after"] is None:
            last_write["seen_after"] = time.time() - last_write["at"]
        if _hit:
            _when = (f"{last_write['seen_after']:.1f} s after the write" if env.live
                     else "after one delivery of the stream")
            _status = f"`{last_write['id']}`: the change is searchable, {_when}."
        else:
            _status = f"`{last_write['id']}`: not searchable yet. Press Refresh again."
    _failed = env.failures()
    _lines = [f"- {_status}"]
    if not env.live:
        _lines.append(f"- Records on the stream not yet delivered: {env.pending()}")
    if _delivered:
        _lines.append(f"- This refresh delivered {_delivered['records']} record(s) in {_delivered['batches']} "
                      f"batch(es); {_delivered['discarded']} sent to the failure queue")
    if _failed:
        _keys = [k for _m in _failed for k in env.record_keys(_m)]
        _lines.append(f"- Failure queue: {len(_failed)} message(s), for item(s) {_keys}")
    if not env.live and env.log:
        _lines.append(f"- Last line of the function's log: `{env.log[-1][:200]}`")
    mo.md("**Index status**\n\n" + "\n".join(_lines))
    return


@app.cell
def _(mo):
    query = mo.ui.text(value="waterproof", label="Search the index (BM25 match on name, description, category, brand)",
                       full_width=True)
    query
    return (query,)


@app.cell
def _(env, keyword_query, last_write, mo, query, refresh, wait):
    refresh, wait, last_write                       # search again after a write or a refresh
    _hits = env.search(keyword_query(query.value, k=10)) if query.value.strip() else []
    _rows = [{"id": h["_id"], "score": round(h["_score"], 3), "name": h["_source"].get("name"),
              "description": h["_source"].get("description"), "price_cad": h["_source"].get("price_cad"),
              "in_stock": h["_source"].get("in_stock"), "index_version": h["_source"].get("index_version")}
             for h in _hits]
    mo.vstack([mo.md(f"{len(_rows)} hit(s) for `{query.value}`, {env.count()} documents in the index"),
               mo.ui.table(_rows, selection=None) if _rows else mo.md("_No hits._")])
    return


if __name__ == "__main__":
    app.run()
