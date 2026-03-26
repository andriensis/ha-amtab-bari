from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import aiohttp
from aiohttp import ClientSession

from .const import GTFS_URL

_LOGGER = logging.getLogger(__name__)


@dataclass
class AmtabGtfsData:
    routes: dict[str, dict] = field(default_factory=dict)
    trips: dict[str, dict] = field(default_factory=dict)
    stops: dict[str, dict] = field(default_factory=dict)
    # (trip_id, stop_id) -> stop_time row
    stop_times_index: dict[tuple[str, str], dict] = field(default_factory=dict)
    # service_id -> calendar row (from calendar.txt, may be absent)
    calendar: dict[str, dict] = field(default_factory=dict)
    # (service_id, date_str) -> exception_type ("1"=added, "2"=removed)
    calendar_dates: dict[tuple[str, str], str] = field(default_factory=dict)
    # (route_id, direction_id_str) -> ordered list of stop dicts
    route_stops: dict[tuple[str, str], list[dict]] = field(default_factory=dict)

    def get_routes(self) -> list[dict]:
        def _key(r: dict) -> tuple:
            name = r.get("route_short_name", r["route_id"])
            # Extract only the leading digit run so "C1" sorts after numeric routes
            leading = ""
            for c in name:
                if c.isdigit():
                    leading += c
                else:
                    break
            if leading:
                return (0, int(leading), name)
            return (1, 0, name)

        return sorted(self.routes.values(), key=_key)

    def get_stops_for_route(self, route_id: str, direction_id: int) -> list[dict]:
        return self.route_stops.get((route_id, str(direction_id)), [])

    def get_active_service_ids(self, today: date) -> set[str]:
        today_str = today.strftime("%Y%m%d")
        day_name = today.strftime("%A").lower()

        active: set[str] = set()
        for service_id, cal in self.calendar.items():
            start = datetime.strptime(cal["start_date"], "%Y%m%d").date()
            end = datetime.strptime(cal["end_date"], "%Y%m%d").date()
            if start <= today <= end and cal.get(day_name, "0") == "1":
                active.add(service_id)

        for (sid, exc_date), exc_type in self.calendar_dates.items():
            if exc_date == today_str:
                if exc_type == "1":
                    active.add(sid)
                elif exc_type == "2":
                    active.discard(sid)

        return active

    def get_next_departures(
        self,
        route_id: str,
        stop_id: str,
        direction_id: int,
        n: int = 5,
    ) -> list[dict]:
        now = datetime.now()
        today = now.date()
        active_sids = self.get_active_service_ids(today)

        candidates: list[tuple[datetime, dict]] = []
        for trip_id, trip in self.trips.items():
            if trip["route_id"] != route_id:
                continue
            if str(trip.get("direction_id", "0")) != str(direction_id):
                continue
            if trip["service_id"] not in active_sids:
                continue

            st = self.stop_times_index.get((trip_id, stop_id))
            if st is None:
                continue

            dep_str = st.get("departure_time") or st.get("arrival_time", "")
            dep_dt = _parse_gtfs_time(dep_str, today)
            if dep_dt is not None and dep_dt > now:
                candidates.append((
                    dep_dt,
                    {
                        "time": dep_dt,
                        "departure_time": dep_dt.strftime("%H:%M"),
                        "headsign": trip.get("trip_headsign", ""),
                        "trip_id": trip_id,
                    },
                ))

        candidates.sort(key=lambda x: x[0])
        return [c[1] for c in candidates[:n]]

    def get_eta_for_approaching_bus(
        self,
        route_id: str,
        stop_id: str,
        direction_id: int,
        vehicles: list[dict],
        delays: dict[str, int],
    ) -> dict | None:
        today = datetime.now().date()
        best: tuple[datetime, dict] | None = None

        for vehicle in vehicles:
            veh = vehicle.get("Vehicle", {})
            trip_info = veh.get("Trip", {})
            if trip_info.get("RouteId") != route_id:
                continue

            trip_id = trip_info.get("TripId", "")
            trip = self.trips.get(trip_id)
            if trip is None:
                continue
            if str(trip.get("direction_id", "0")) != str(direction_id):
                continue

            current_seq = int(veh.get("CurrentStopSequence", 0))
            st = self.stop_times_index.get((trip_id, stop_id))
            if st is None:
                continue

            stop_seq = int(st.get("stop_sequence", 0))
            if stop_seq <= current_seq:
                continue

            arr_str = st.get("arrival_time") or st.get("departure_time", "")
            arr_dt = _parse_gtfs_time(arr_str, today)
            if arr_dt is None:
                continue

            delay_secs = delays.get(trip_id, 0)
            eta_dt = arr_dt + timedelta(seconds=delay_secs)

            if best is None or eta_dt < best[0]:
                pos = veh.get("Position", {})
                best = (
                    eta_dt,
                    {
                        "eta": eta_dt,
                        "scheduled_arrival": arr_dt.strftime("%H:%M"),
                        "delay_seconds": delay_secs,
                        "bus_id": vehicle.get("Id", ""),
                        "latitude": pos.get("Latitude"),
                        "longitude": pos.get("Longitude"),
                        "speed": round(pos.get("Speed", 0), 1),
                        "trip_id": trip_id,
                    },
                )

        return best[1] if best else None


def _parse_gtfs_time(time_str: str, base_date: date) -> datetime | None:
    """Parse a GTFS time string; handles values >= 24:00 for overnight trips."""
    if not time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return datetime.combine(base_date, time(0, 0)) + timedelta(hours=h, minutes=m, seconds=s)
    except (ValueError, IndexError, TypeError):
        return None


async def async_download_gtfs(session: ClientSession) -> bytes:
    """Download the GTFS zip and return raw bytes."""
    _LOGGER.debug("Downloading GTFS feed from %s", GTFS_URL)
    async with session.get(
        GTFS_URL, timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        resp.raise_for_status()
        return await resp.read()


def parse_gtfs(raw: bytes) -> AmtabGtfsData:
    """Parse raw GTFS zip bytes. CPU-bound — run in an executor."""
    data = AmtabGtfsData()

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())

        def _read(filename: str) -> list[dict]:
            if filename not in names:
                return []
            with zf.open(filename) as f:
                return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

        routes_raw = _read("routes.txt")
        trips_raw = _read("trips.txt")
        stops_raw = _read("stops.txt")
        stop_times_raw = _read("stop_times.txt")
        calendar_raw = _read("calendar.txt")
        calendar_dates_raw = _read("calendar_dates.txt")

    data.routes = {r["route_id"]: r for r in routes_raw}
    data.stops = {s["stop_id"]: s for s in stops_raw}
    data.trips = {t["trip_id"]: t for t in trips_raw}
    data.calendar = {c["service_id"]: c for c in calendar_raw}

    for row in calendar_dates_raw:
        data.calendar_dates[(row["service_id"], row["date"])] = row["exception_type"]

    _LOGGER.debug("Indexing %d stop_time rows", len(stop_times_raw))

    # Build stop_times_index and a temporary per-trip list for route_stops
    stop_times_by_trip: dict[str, list[dict]] = {}
    for st in stop_times_raw:
        trip_id = st["trip_id"]
        stop_id = st["stop_id"]
        stop_times_by_trip.setdefault(trip_id, []).append(st)
        data.stop_times_index[(trip_id, stop_id)] = st

    # Sort each trip's stops once, then build route_stops using the first trip per route+direction
    sorted_trip_stops: dict[str, list[dict]] = {
        tid: sorted(stops, key=lambda x: int(x.get("stop_sequence", 0)))
        for tid, stops in stop_times_by_trip.items()
    }

    seen: set[tuple[str, str]] = set()
    for trip in trips_raw:
        route_id = trip["route_id"]
        direction_id = str(trip.get("direction_id", "0"))
        key = (route_id, direction_id)
        if key in seen:
            continue
        trip_stops = sorted_trip_stops.get(trip["trip_id"], [])
        if trip_stops:
            data.route_stops[key] = [
                data.stops[st["stop_id"]]
                for st in trip_stops
                if st["stop_id"] in data.stops
            ]
            seen.add(key)

    _LOGGER.info(
        "GTFS loaded: %d routes, %d trips, %d stops",
        len(data.routes),
        len(data.trips),
        len(data.stops),
    )
    return data
