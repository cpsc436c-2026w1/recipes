"""The rides events, one Pydantic model per kind, and the delivery line a system receives.

    trip_requested  event_id, trip_id, pickup_zone_id, event_time
    trip_accepted   event_id, trip_id, driver_id, event_time
    trip_completed  event_id, trip_id, driver_id, dropoff_zone_id, trip_miles, trip_time_s, fare, driver_pay, event_time

A delivery line is the event's fields plus `delivery_id` and `received_time`. Every time is ISO 8601 with a time zone;
it is written in UTC, fixed width, with milliseconds and a "Z" (2026-11-23T09:00:00.000Z), so strings sort as times.

Decision (README, "one definition of the events"): the generator, the replayer, the team's handlers and the step
code all import these models, so a field renamed in one place fails everywhere at once. `extra="forbid"`: a line
with a field the model does not have is refused, not passed over.

This file is a shared helper (tools/shared_helpers.toml): copy it unchanged into `src/rides/events.py`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, TypeAdapter, ValidationError


def iso(t: datetime) -> str:
    """UTC time as fixed-width ISO 8601 with milliseconds: 2026-11-23T09:00:00.000Z."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse(s: str) -> datetime:
    """An ISO 8601 time with a zone ("Z" or an offset), as an aware UTC datetime."""
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without a time zone: {s}")
    return t.astimezone(timezone.utc)


Time = Annotated[AwareDatetime, PlainSerializer(iso, return_type=str)]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    trip_id: str
    event_time: Time


class TripRequested(_Event):
    event_type: Literal["trip_requested"] = "trip_requested"
    pickup_zone_id: int


class TripAccepted(_Event):
    event_type: Literal["trip_accepted"] = "trip_accepted"
    driver_id: str


class TripCompleted(_Event):
    event_type: Literal["trip_completed"] = "trip_completed"
    driver_id: str
    dropoff_zone_id: int
    trip_miles: float
    trip_time_s: int
    fare: float
    driver_pay: float


Event = Annotated[Union[TripRequested, TripAccepted, TripCompleted], Field(discriminator="event_type")]


class _Delivered(BaseModel):
    delivery_id: str
    received_time: Time


class TripRequestedDelivery(TripRequested, _Delivered):
    pass


class TripAcceptedDelivery(TripAccepted, _Delivered):
    pass


class TripCompletedDelivery(TripCompleted, _Delivered):
    pass


Delivery = Annotated[Union[TripRequestedDelivery, TripAcceptedDelivery, TripCompletedDelivery],
                     Field(discriminator="event_type")]

EVENT = TypeAdapter(Event)
DELIVERY = TypeAdapter(Delivery)
DELIVERY_FIELDS = ("delivery_id", "received_time")


class LineError(ValueError):
    """A line that does not match its model: the file, the line number (from 1) and the field."""

    def __init__(self, file: str, line: int, field: str, message: str):
        self.file, self.line, self.field = file, line, field
        super().__init__(f"{file}, line {line}, field {field}: {message}")


def to_line(model: BaseModel) -> str:
    """One JSON line with sorted keys: the same model gives the same bytes."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True)


def as_delivery(event: BaseModel, delivery_id: str, received_time: datetime) -> BaseModel:
    """The delivery of an event: its fields plus `delivery_id` and `received_time`."""
    return DELIVERY.validate_python({**event.model_dump(), "delivery_id": delivery_id,
                                     "received_time": received_time})


def as_event(delivery: BaseModel) -> BaseModel:
    """The event a delivery carries."""
    return EVENT.validate_python(delivery.model_dump(exclude=set(DELIVERY_FIELDS)))


def validate_line(text: str, adapter: TypeAdapter, file: str = "<line>", line: int = 1) -> BaseModel:
    """Validate one JSON line; a mismatch raises LineError naming the file, the line and the field."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise LineError(file, line, "-", f"not JSON: {err}") from None
    try:
        return adapter.validate_python(data)
    except ValidationError as err:
        first = err.errors()[0]
        names = [str(p) for p in first["loc"] if isinstance(p, str) and p not in
                 ("trip_requested", "trip_accepted", "trip_completed")]
        raise LineError(file, line, names[-1] if names else "event_type", first["msg"]) from None


def read_jsonl(path: str | Path, adapter: TypeAdapter = EVENT) -> list[BaseModel]:
    """Every line of a file validated against its model (EVENT or DELIVERY); the first bad line stops the read."""
    path = Path(path)
    return [validate_line(text, adapter, path.name, n)
            for n, text in enumerate(path.read_text().splitlines(), 1) if text.strip()]
