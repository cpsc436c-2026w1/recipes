"""Everything the team has in the account, found by four routes, written to docs/resources.md.

    uv run python scripts/inventory.py            # docs/resources.md and state/inventory.json
    uv run python scripts/inventory.py --probe    # each read-only list call once: allowed or refused

The four routes, merged by ARN:
1. the ledger `state/ledger.jsonl` (scripts/tags.py writes a row for every create);
2. the tag Team=<TEAM>: the Resource Groups Tagging API `GetResources`, and IAM `ListRoles` with `ListRoleTags`
   (GetResources does not return IAM roles);
3. schedule groups named `<PREFIX>-…` and the schedules in each;
4. the name prefix, for what takes no tags: Glue databases and tables `<prefix>_…`, Athena data sources, the event
   source mappings of each function found, log groups `/aws/lambda/<PREFIX>-…`.

A row found by a route other than the ledger and not in it goes under "Not in the ledger": someone made it outside
the scripts (or a script that does not call record()).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kinds import arn_of, bills, canonical, from_arn, label  # noqa: E402
from tags import ledger_path, ledger_rows, prefix, tag_values  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

def docs_path() -> Path:
    """docs/resources.md of the recipe folder, or $RESOURCES_DOC."""
    import os
    return Path(os.environ.get("RESOURCES_DOC") or ROOT / "docs" / "resources.md")
REGION = "ca-central-1"


@dataclass
class Row:
    kind: str
    name: str                    # as a person reads it
    arn: str
    state: str = ""              # created, borrowed (ledger), or "" (not in the ledger)
    clean: str = ""
    found_by: list[str] = field(default_factory=list)
    outcome: str = ""            # after a teardown: deleted <time>, kept, still there, delete by hand

    @property
    def id(self) -> str:
        """The name its own service knows it by (an event source mapping's UUID)."""
        return from_arn(self.arn)[1] if self.arn.startswith("arn:") else self.name

    @property
    def bills(self) -> str:
        return bills(self.kind)

    @property
    def in_ledger(self) -> bool:
        return "ledger" in self.found_by


def session():
    return boto3.session.Session(region_name=REGION)


def foreign(row: dict, account: str) -> str:
    """Why a ledger row is not this account's and PREFIX's ("" when it is): a copied or old ledger must not make the
    teardown delete a same-named resource of the account it runs in."""
    parts = (row.get("arn") or "").split(":")
    if len(parts) > 4 and parts[4] not in ("", account):
        return f"recorded in account {parts[4]}, not this one"
    if row.get("prefix") and row["prefix"] != prefix():
        return f"recorded under PREFIX {row['prefix']}, not {prefix()}"
    return ""


def _ledger(account: str, log=None) -> dict[str, Row]:
    last: dict[tuple, dict] = {}
    for r in ledger_rows():
        last[(r["type"], r["name"])] = r
    out = {}
    for (kind, name), r in last.items():
        if r["state"] == "deleted":
            continue
        why = foreign(r, account)
        if why:
            if log:
                log(f"skipped  ledger row {kind} {name}: {why}; check the ledger and PREFIX")
            continue
        arn = canonical(r["arn"]) if r.get("arn") else arn_of(kind, name, account)
        out[arn] = Row(kind, name, arn, r["state"], r.get("clean", ""), ["ledger"])
    return out


def _add(rows: dict[str, Row], arn: str, route: str, kind: str | None = None, name: str | None = None) -> None:
    arn = canonical(arn) if arn.startswith("arn:") else arn
    if arn in rows:
        if route not in rows[arn].found_by:
            rows[arn].found_by.append(route)
        return
    k, n = from_arn(arn) if arn.startswith("arn:") else (kind, name)
    rows[arn] = Row(kind or k, name or n, arn, found_by=[route])


def by_tag(sess, rows: dict[str, Row], team: str) -> None:
    tagging = sess.client("resourcegroupstaggingapi")
    token = ""
    while True:
        page = tagging.get_resources(TagFilters=[{"Key": "Team", "Values": [team]}], ResourcesPerPage=100,
                                     **({"PaginationToken": token} if token else {}))
        for r in page.get("ResourceTagMappingList", []):
            _add(rows, r["ResourceARN"], "tag")
            row = rows[canonical(r["ResourceARN"])]
            if row.kind == "appsync:apis":          # a kind the teardown does not delete: shown by its name
                try:
                    row.name = sess.client("appsync").get_graphql_api(apiId=row.name)["graphqlApi"]["name"]
                except ClientError:
                    pass
        token = page.get("PaginationToken", "")
        if not token:
            break


def by_role_tag(sess, rows: dict[str, Row], team: str, log=print) -> None:
    """IAM roles tagged Team=<TEAM> (GetResources does not return roles). A role whose tags are refused is skipped:
    a sandbox account holds the lease's own roles, whose ListRoleTags is denied (live run 2026-11-23: 2 of 35), and
    one of them must not drop the whole route."""
    iam, refused = sess.client("iam"), []
    for page in iam.get_paginator("list_roles").paginate():
        for role in page["Roles"]:
            try:
                found = iam.list_role_tags(RoleName=role["RoleName"]).get("Tags", [])
            except ClientError as err:
                if err.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
                    raise
                refused.append(role["RoleName"])
                continue
            if {t["Key"]: t["Value"] for t in found}.get("Team") == team:
                _add(rows, role["Arn"], "IAM")
    if refused:
        log(f"skipped  {len(refused)} IAM roles whose tags are refused (the account's own, not the team's)")


def by_schedule_group(sess, rows: dict[str, Row], p: str) -> None:
    sch = sess.client("scheduler")
    for page in sch.get_paginator("list_schedule_groups").paginate(NamePrefix=f"{p}-"):
        for g in page["ScheduleGroups"]:
            _add(rows, g["Arn"], "schedule group")
            for sp in sch.get_paginator("list_schedules").paginate(GroupName=g["Name"]):
                for s in sp["Schedules"]:
                    _add(rows, s["Arn"], "schedule group", "schedule", f"{g['Name']}/{s['Name']}")


def by_name(sess, rows: dict[str, Row], p: str, account: str) -> None:
    glue_prefix = p.replace("-", "_") + "_"
    glue = sess.client("glue")
    for page in glue.get_paginator("get_databases").paginate():
        for db in page["DatabaseList"]:
            if not db["Name"].startswith(glue_prefix):
                continue
            _add(rows, arn_of("glue-database", db["Name"], account), "name")
            for tp in glue.get_paginator("get_tables").paginate(DatabaseName=db["Name"]):
                for t in tp["TableList"]:
                    _add(rows, arn_of("glue-table", f"{db['Name']}.{t['Name']}", account), "name")
    athena = sess.client("athena")
    for page in athena.get_paginator("list_data_catalogs").paginate():
        for c in page["DataCatalogsSummary"]:
            arn = arn_of("athena-data-catalog", c["CatalogName"], account)
            if c["CatalogName"].startswith(p) or arn in rows:
                _add(rows, arn, "name")
    lam = sess.client("lambda")
    functions = [r.name for r in rows.values() if r.kind == "lambda-function"]
    for page in lam.get_paginator("list_functions").paginate():
        functions += [f["FunctionName"] for f in page["Functions"] if f["FunctionName"].startswith(f"{p}-")]
    for fn in sorted(set(functions)):
        for page in lam.get_paginator("list_event_source_mappings").paginate(FunctionName=fn):
            for m in page["EventSourceMappings"]:
                arn = m.get("EventSourceMappingArn") or f"arn:aws:lambda:{REGION}:{account}:event-source-mapping:{m['UUID']}"
                if arn not in rows:
                    src = m.get("EventSourceArn", "").rsplit("/", 1)[-1]
                    _add(rows, arn, "name", "lambda-esm", f"{src} to {fn}")
                else:
                    _add(rows, arn, "name")
    logs = sess.client("logs")
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=f"/aws/lambda/{p}-"):
        for g in page["logGroups"]:
            _add(rows, arn_of("log-group", g["logGroupName"], account), "name")


def take(sess=None, log=print, refused: list[str] | None = None) -> list[Row]:
    """The four routes, merged. A refused list call is printed and its route is left out, never the whole run; the
    route's name is added to `refused`, so a teardown can say its inventory is incomplete."""
    sess = sess or session()
    account = sess.client("sts").get_caller_identity()["Account"]
    p, team = prefix(), tag_values()["Team"]
    rows = _ledger(account, log)
    for route, fn, args in (("tag", by_tag, (team,)), ("IAM", by_role_tag, (team, log)),
                            ("schedule group", by_schedule_group, (p,)), ("name", by_name, (p, account))):
        try:
            fn(sess, rows, *args)
        except ClientError as err:
            log(f"refused  route '{route}': {err.response['Error']['Code']}; the inventory is incomplete: what only "
                f"this route finds is not listed")
            if refused is not None:
                refused.append(route)
    return sorted(rows.values(), key=lambda r: (not r.in_ledger, r.bills != "hourly", r.kind, r.name))


def write(rows: list[Row], path: Path | None = None, by_hand: list[Row] | None = None, title: str = "") -> None:
    """docs/resources.md: one row per resource, then those not in the ledger, then those left for a person."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    def table(rs):
        lines = ["| Type | Name | Created or borrowed | Bills | Clean-up step | Found by | After teardown |",
                 "|---|---|---|---|---|---|---|"]
        lines += [f"| {label(r.kind)} | `{r.name}` | {r.state or '—'} | {r.bills} | {r.clean or '—'} | "
                  f"{', '.join(r.found_by)} | {r.outcome or '—'} |" for r in rs]
        return lines
    known = [r for r in rows if r.in_ledger]
    other = [r for r in rows if not r.in_ledger]
    out = [f"# Resources of team {tag_values()['Team']} (PREFIX {prefix()})", "",
           title or f"Written by scripts/inventory.py at {now}: {len(known)} in the ledger, {len(other)} not.", "",
           *table(known), "", "## Not in the ledger", ""]
    out += table(other) if other else ["None."]
    if by_hand:
        out += ["", "## Delete by hand", "", "The teardown does not know these kinds; it left them in place.", "",
                "| Type | Name | ARN |", "|---|---|---|"]
        out += [f"| {r.kind} | `{r.name}` | `{r.arn}` |" for r in by_hand]
    path = path or docs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    state = ledger_path().parent / "inventory.json"          # beside the ledger, in state/
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps([asdict(r) for r in rows], indent=1))


def probe(sess=None, log=print) -> int:
    """Each read-only list call of the inventory once, so a refused one shows before a teardown needs it."""
    sess = sess or session()
    calls = [("tag:GetResources", lambda: sess.client("resourcegroupstaggingapi").get_resources(ResourcesPerPage=1)),
             ("iam:ListRoles", lambda: sess.client("iam").list_roles(MaxItems=1)),
             ("scheduler:ListScheduleGroups", lambda: sess.client("scheduler").list_schedule_groups(MaxResults=1)),
             ("glue:GetDatabases", lambda: sess.client("glue").get_databases(MaxResults=1)),
             ("athena:ListDataCatalogs", lambda: sess.client("athena").list_data_catalogs(MaxResults=2)),
             ("lambda:ListEventSourceMappings", lambda: sess.client("lambda").list_event_source_mappings(MaxItems=1)),
             ("logs:DescribeLogGroups", lambda: sess.client("logs").describe_log_groups(limit=1))]
    refused = 0
    for name, call in calls:
        try:
            call()
            log(f"allowed  {name}")
        except ClientError as err:
            refused += 1
            log(f"refused  {name}: {err.response['Error']['Code']} (the ledger stands in for this route)")
    return 1 if refused else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args(argv)
    if args.probe:
        return probe()
    rows = take()
    write(rows)
    other = [r for r in rows if not r.in_ledger]
    print(f"inventory {len(rows) - len(other)} in the ledger, {len(other)} not in the ledger -> docs/resources.md")
    for r in other:
        print(f"not in the ledger: {label(r.kind)} {r.name} (found by {', '.join(r.found_by)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
