from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientError, ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    GTFS_CACHE_KEY,
    GTFS_REFRESH_INTERVAL_HOURS,
    RT_DELAYS_ENDPOINT,
    RT_VEHICLES_ENDPOINT,
)
from .gtfs_parser import AmtabGtfsData, async_download_gtfs, parse_gtfs

_LOGGER = logging.getLogger(__name__)

HEADERS = {"User-Agent": "ha-amtab-bari/1.0"}
_RT_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Key for the asyncio.Lock stored in hass.data[DOMAIN]
_GTFS_LOCK_KEY = "_gtfs_lock"


@dataclass
class AmtabData:
    gtfs: AmtabGtfsData
    vehicles: list[dict]
    delays: dict[str, int]  # trip_id -> delay seconds
    updated_at: datetime


class AmtabCoordinator(DataUpdateCoordinator[AmtabData]):
    def __init__(
        self,
        hass: HomeAssistant,
        route_id: str,
        scan_interval_minutes: int,
        entry_id: str,
    ) -> None:
        self._route_id = route_id
        self._session: ClientSession = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry_id}",
            update_interval=timedelta(minutes=scan_interval_minutes),
        )

    async def _async_update_data(self) -> AmtabData:
        gtfs = await self._get_gtfs()
        vehicles = await self._fetch_vehicles()
        delays = await self._fetch_delays()
        return AmtabData(gtfs=gtfs, vehicles=vehicles, delays=delays, updated_at=datetime.now())

    async def _get_gtfs(self) -> AmtabGtfsData:
        """Return GTFS data from shared cache, re-downloading if stale (>24 h)."""
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        lock: asyncio.Lock = domain_data.setdefault(_GTFS_LOCK_KEY, asyncio.Lock())
        cache: dict = domain_data.setdefault(GTFS_CACHE_KEY, {})

        last_update: datetime | None = cache.get("updated_at")
        stale = last_update is None or (
            datetime.now() - last_update > timedelta(hours=GTFS_REFRESH_INTERVAL_HOURS)
        )

        if not stale and "data" in cache:
            return cache["data"]

        async with lock:
            # Re-check inside the lock — another coordinator may have just refreshed
            last_update = cache.get("updated_at")
            stale = last_update is None or (
                datetime.now() - last_update > timedelta(hours=GTFS_REFRESH_INTERVAL_HOURS)
            )
            if not stale and "data" in cache:
                return cache["data"]

            _LOGGER.info("Downloading fresh GTFS data")
            try:
                raw = await async_download_gtfs(self._session)
                cache["data"] = await self.hass.async_add_executor_job(parse_gtfs, raw)
                cache["updated_at"] = datetime.now()
            except (ClientError, OSError) as exc:
                if "data" in cache:
                    _LOGGER.warning("GTFS refresh failed, keeping cached data: %s", exc)
                else:
                    raise UpdateFailed(f"Cannot download GTFS data: {exc}") from exc

        return cache["data"]

    async def _fetch_vehicles(self) -> list[dict]:
        try:
            async with self._session.get(
                RT_VEHICLES_ENDPOINT,
                params={"lineCode": self._route_id},
                headers=HEADERS,
                timeout=_RT_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (ClientError, OSError, ValueError) as exc:
            _LOGGER.debug("RT vehicles fetch failed (route %s): %s", self._route_id, exc)
            return []

        seen: set[str] = set()
        result: list[dict] = []
        for entity in data.get("Entities", []):
            vid = entity.get("Id", "")
            if vid not in seen:
                seen.add(vid)
                result.append(entity)
        return result

    async def _fetch_delays(self) -> dict[str, int]:
        try:
            async with self._session.get(
                RT_DELAYS_ENDPOINT,
                params={"lineCode": self._route_id},
                headers=HEADERS,
                timeout=_RT_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (ClientError, OSError, ValueError) as exc:
            _LOGGER.debug("RT delays fetch failed (route %s): %s", self._route_id, exc)
            return {}

        out: dict[str, int] = {}
        for e in data.get("Entities", []):
            tu = e.get("TripUpdate")
            if not tu or not tu.get("Trip"):
                continue
            trip_id = tu["Trip"].get("TripId", "")
            if not trip_id:
                continue
            try:
                out[trip_id] = int(float(tu.get("Delay", 0)))
            except (TypeError, ValueError):
                pass
        return out
