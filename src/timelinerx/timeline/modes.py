"""Transport modes.

Google Timeline labels every movement segment with an activity type
(``activity.topCandidate.type`` in on-device exports, ``activityType`` in the
legacy Semantic Location History). TimelinerX keeps that label per route hop
so the video can show *how* you travelled: flights become arcs with a plane,
ground and water legs keep their shape.

Where the export has no label (raw signals, some old records) a flight is
inferred only from physics: a hop of at least 150 km covered faster than
250 km/h cannot be ground travel. Nothing else is guessed.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional


class Mode(IntEnum):
    UNKNOWN = 0
    WALK = 1
    CYCLE = 2        # bicycle and motorbike
    ROAD = 3         # car, taxi, bus
    RAIL = 4
    WATER = 5
    FLIGHT = 6


_TYPES = {
    Mode.WALK: ("WALKING", "ON_FOOT", "RUNNING", "HIKING", "WALKING_NORDIC"),
    Mode.CYCLE: ("CYCLING", "IN_BICYCLE", "MOTORCYCLING", "IN_MOTORCYCLE", "SKATEBOARDING",
                 "SKIING", "SNOWBOARDING"),
    Mode.ROAD: ("IN_PASSENGER_VEHICLE", "IN_VEHICLE", "DRIVING", "IN_CAR", "IN_TAXI", "IN_BUS",
                "IN_ROAD_VEHICLE", "IN_FOUR_WHEELER"),
    Mode.RAIL: ("IN_TRAIN", "IN_SUBWAY", "IN_TRAM", "IN_RAIL_VEHICLE", "IN_CABLECAR", "IN_FUNICULAR"),
    Mode.WATER: ("IN_FERRY", "SAILING", "BOATING", "IN_BOAT", "KAYAKING", "ROWING", "SURFING",
                 "SWIMMING", "IN_CRUISE_SHIP"),
    Mode.FLIGHT: ("FLYING", "IN_AIRPLANE", "IN_PLANE", "IN_HELICOPTER"),
}
_LOOKUP = {t: m for m, ts in _TYPES.items() for t in ts}

FLIGHT_MIN_KM = 150.0
FLIGHT_MIN_KMH = 250.0
ARC_MIN_KM = 80.0            # labelled flights shorter than this are drawn straight


def mode_from_type(activity_type: Optional[str]) -> Mode:
    if not isinstance(activity_type, str):
        return Mode.UNKNOWN
    return _LOOKUP.get(activity_type.strip().upper(), Mode.UNKNOWN)


def infer_flight(distance_km: float, seconds: float) -> bool:
    """Physics-only flight inference for unlabelled hops."""
    if distance_km < FLIGHT_MIN_KM or seconds <= 0:
        return False
    return distance_km / (seconds / 3600.0) >= FLIGHT_MIN_KMH


MODE_KEYS = {Mode.UNKNOWN: "unknown", Mode.WALK: "walk", Mode.CYCLE: "cycle", Mode.ROAD: "road",
             Mode.RAIL: "rail", Mode.WATER: "water", Mode.FLIGHT: "flight"}
