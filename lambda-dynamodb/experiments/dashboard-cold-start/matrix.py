"""Cold starts of recipe-dashcold-page: memory x where the client is made, N forced cold starts each.

    python matrix.py <recipe ext-dashboard-page dir> <lazy page.py> <out.jsonl> [--n 5] [--mem 128,512,1024]

Each cell: the cell's page.py in the zip (with page.html, slots.py), MemorySize set; then for each sample a new value of
DASHCOLD_ROUND in the environment (other variables kept), LastUpdateStatus Successful, one invoke of
GET /api/zone-counts with LogType=Tail (REPORT line), then 3 warm invokes. A cold sample without Init Duration is redone.
"""
import base64, io, json, re, sys, time, zipfile
from pathlib import Path
import boto3

src, lazy, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
N = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 5
MEMS = [int(m) for m in (sys.argv[sys.argv.index("--mem") + 1] if "--mem" in sys.argv else "128,512,1024").split(",")]
VARIANTS = [v for v in (sys.argv[sys.argv.index("--var") + 1] if "--var" in sys.argv else "first-read,init").split(",")]
FN = "recipe-dashcold-page"
lam = boto3.session.Session(region_name="ca-central-1").client("lambda")
REPORT = re.compile(r"Duration:\s*([\d.]+) ms\s+Billed Duration:\s*(\d+) ms\s+Memory Size:\s*(\d+) MB\s+"
                    r"Max Memory Used:\s*(\d+) MB(?:\s+Init Duration:\s*([\d.]+) ms)?")
EVENT = json.dumps({"rawPath": "/api/zone-counts", "requestContext": {"http": {"method": "GET"}}}).encode()


def wait():
    while True:
        c = lam.get_function_configuration(FunctionName=FN)
        if c.get("LastUpdateStatus") == "Successful" and c.get("State") == "Active":
            return c
        if c.get("LastUpdateStatus") == "Failed":
            raise RuntimeError(c)
        time.sleep(1)


def zip_with(page: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(page, "page.py")
        for f in ("page.html", "slots.py"):
            z.write(src / f, f)
    return buf.getvalue()


def invoke():
    t0 = time.perf_counter()
    r = lam.invoke(FunctionName=FN, Payload=EVENT, LogType="Tail")
    wall = (time.perf_counter() - t0) * 1000
    body = json.loads(r["Payload"].read())
    log = base64.b64decode(r["LogResult"]).decode()
    m = REPORT.search(log)
    timing = {}
    for line in log.splitlines():
        if '"timing"' in line:
            try:
                timing |= {k: v for k, v in json.loads(line[line.index("{"):]).items() if k != "timing"}
            except ValueError:
                pass
    return {"status": body.get("statusCode"), "wall_ms": round(wall, 1), "duration_ms": float(m[1]),
            "billed_ms": int(m[2]), "memory_mb": int(m[3]), "max_memory_mb": int(m[4]),
            "init_ms": float(m[5]) if m[5] else None, "report": m[0].strip(), **timing}


rnd = int(time.time())
with out.open("a") as f:
    for mem in MEMS:
        for var in VARIANTS:
            page = lazy if var == "first-read" else src / "page.py"
            lam.update_function_code(FunctionName=FN, ZipFile=zip_with(page))
            wait()
            done = 0
            while done < N:
                rnd += 1
                env = wait().get("Environment", {}).get("Variables", {}) | {"DASHCOLD_ROUND": str(rnd)}
                lam.update_function_configuration(FunctionName=FN, MemorySize=mem, Environment={"Variables": env})
                wait()
                cold = invoke()
                if cold["init_ms"] is None:
                    print("not cold, redone:", cold["report"], flush=True)
                    continue
                done += 1
                row = {"memory_mb": mem, "client": var, "sample": done, "kind": "cold", **cold}
                f.write(json.dumps(row) + "\n")
                print(json.dumps(row), flush=True)
                for w in range(3):
                    warm = invoke()
                    row = {"memory_mb": mem, "client": var, "sample": done, "kind": "warm", **warm}
                    f.write(json.dumps(row) + "\n")
                    print(json.dumps({k: row[k] for k in ("memory_mb", "client", "kind", "wall_ms", "duration_ms",
                                                          "billed_ms", "init_ms")}), flush=True)
                f.flush()
