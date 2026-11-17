"""main/handler.py against moto: each action, the race on one trip, and the transaction that must not half-apply."""

import json


def call(handler, **event):
    """Call the handler the way a function URL does: the request is the JSON body."""
    r = handler.lambda_handler({"body": json.dumps(event)})
    return r["statusCode"], json.loads(r["body"])


def test_get_by_key(handler):
    status, out = call(handler, action="get", trip_id="t-0001", strong=True)
    assert status == 200 and out["item"]["trip_id"] == "t-0001"
    assert out["item"]["fare_quote"] == 21  # a DynamoDB number comes back as a JSON number, not text


def test_rider_history_in_time_order(handler):
    _, out = call(handler, action="rider", rider_id="r-03", since="2026-10-02")
    times = [i["requested_at"] for i in out["items"]]
    assert times == sorted(times) and len(times) == 2
    _, everything = call(handler, action="rider", rider_id="r-03")  # since is optional
    assert len(everything["items"]) == 3


def test_race_one_claim_wins(handler):
    assert call(handler, action="claim", trip_id="t-0000", driver_id="d-1")[1] == {"claimed": True}
    # Decision (conditional write): the second driver is refused and told who holds the trip.
    assert call(handler, action="claim", trip_id="t-0000", driver_id="d-2")[1] == {"claimed": False, "held_by": "d-1"}


def test_accept_is_all_or_nothing(handler):
    assert call(handler, action="accept", trip_id="t-0001", driver_id="d-2")[1] == {"accepted": True}
    # d-2 is now busy: the driver's condition fails, so the trip must not get a driver either.
    _, out = call(handler, action="accept", trip_id="t-0002", driver_id="d-2")
    assert out["accepted"] is False and out["reasons"]["driver"] == "ConditionalCheckFailed"
    trip = call(handler, action="get", trip_id="t-0002", strong=True)[1]["item"]
    assert "driver_id" not in trip and trip["status"] == "requested"


def test_unknown_action_is_refused(handler):
    status, out = call(handler, action="delete-everything")
    assert status == 400 and "unknown action" in out["error"]
