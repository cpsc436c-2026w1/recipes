"""Fan-out through a topic to two queues: a stopped consumer does not hold up the other. Offline, against moto."""

import importlib.util
from pathlib import Path


def load_run():
    path = Path(__file__).parent.parent / "ext-fan-out" / "run.py"
    spec = importlib.util.spec_from_file_location("fanout_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stopped_consumer_does_not_hold_up_the_dashboard():
    out = load_run().run_local(100, 3.0, "recipe-test")
    stopped = out["while_stopped"]
    assert stopped == {"dashboard stored": 100, "history stored": 0, "history queue holds": 100}
    assert out["summary"]["history"]["stored"] == 100               # history caught up once started
    # History waited while stopped (3 s), the dashboard did not. Measured from the event time this run writes on
    # its own clock: the fake queue's SentTimestamp may be set when the copy is first received, which would hide the
    # stop from "entered to stored".
    dash = out["summary"]["dashboard"]["lag: event to stored"]
    hist = out["summary"]["history"]["lag: event to stored"]
    assert hist["median_s"] - dash["median_s"] >= 2.0
