"""Two clients race to accept the same trip.

Each client runs in its own thread with its own DynamoDB client: it reads the trip, waits at a barrier until the other
client has read too, then writes. The barrier forces the interleaving in which a lost update happens (both read before
either writes). Without it the race happens only by chance, under load, which is why a test that does not force it
proves little.
"""

import threading

import trips

ACCEPT = {"unsafe": trips.accept_unsafe, "safe": trips.accept_safe}


def race_once(make_client, table: str, trip_id: str, mode: str, drivers=("d-7", "d-9")) -> dict:
    setup = make_client()
    trips.reset_trip(setup, table, trip_id)
    barrier = threading.Barrier(len(drivers))
    results: dict[str, dict] = {}

    def client(driver: str):
        ddb = make_client()
        seen = trips.read_trip(ddb, table, trip_id)
        barrier.wait(timeout=30)                         # both have read; now both write
        if seen["status"] != "requested":
            results[driver] = {"driver": driver, "accepted": False, "held_by": seen["driver_id"]}
            return
        results[driver] = ACCEPT[mode](ddb, table, trip_id, driver, seen["version"])

    threads = [threading.Thread(target=client, args=(d,)) for d in drivers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = trips.read_trip(setup, table, trip_id)
    told_yes = sorted(d for d, r in results.items() if r["accepted"])
    return {"told_accepted": told_yes, "stored_driver": final["driver_id"], "version": final["version"],
            "results": results}


def race_rounds(make_client, table: str, mode: str, rounds: int) -> dict:
    """Run `rounds` races, each on a fresh trip. Count rounds where more than one driver was told yes."""
    summary = {"rounds": rounds, "one_winner": 0, "both_told_yes": 0, "winner_is_stored": 0}
    for i in range(rounds):
        r = race_once(make_client, table, f"t-race-{i:04d}", mode)
        if len(r["told_accepted"]) == 1:
            summary["one_winner"] += 1
            summary["winner_is_stored"] += r["told_accepted"][0] == r["stored_driver"]
        elif len(r["told_accepted"]) > 1:
            summary["both_told_yes"] += 1
    return summary
