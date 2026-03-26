from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DIRECTION_ID,
    CONF_MONITOR_NAME,
    CONF_ROUTE_ID,
    CONF_STOP_ID,
    DEPARTURES_TO_SHOW,
    DOMAIN,
)
from .coordinator import AmtabCoordinator, AmtabData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AmtabCoordinator = hass.data[DOMAIN][entry.entry_id]

    route_id: str = entry.data[CONF_ROUTE_ID]
    stop_id: str = entry.data[CONF_STOP_ID]
    direction_id: int = int(entry.data[CONF_DIRECTION_ID])
    monitor_name: str = entry.data[CONF_MONITOR_NAME]

    async_add_entities([
        NextDepartureSensor(coordinator, entry, route_id, stop_id, direction_id, monitor_name),
        RealtimeEtaSensor(coordinator, entry, route_id, stop_id, direction_id, monitor_name),
        ActiveBusesSensor(coordinator, entry, route_id, monitor_name),
    ])


def _device_info(entry: ConfigEntry, monitor_name: str) -> DeviceInfo:
    route_id = entry.data.get(CONF_ROUTE_ID, "")
    direction_id = entry.data.get(CONF_DIRECTION_ID, 0)
    direction_label = "andata" if int(direction_id) == 0 else "ritorno"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=monitor_name,
        manufacturer="AMTAB Bari",
        model=f"Linea {route_id} — {direction_label}",
        configuration_url="https://www.amtab.it",
    )


class _AmtabSensorBase(CoordinatorEntity[AmtabData], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AmtabCoordinator,
        entry: ConfigEntry,
        route_id: str,
        monitor_name: str,
        entity_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{entry.entry_id}_{entity_suffix}"
        self._attr_device_info = _device_info(entry, monitor_name)


class NextDepartureSensor(_AmtabSensorBase):
    _attr_translation_key = "next_departure"
    _attr_icon = "mdi:bus-clock"
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AmtabCoordinator,
        entry: ConfigEntry,
        route_id: str,
        stop_id: str,
        direction_id: int,
        monitor_name: str,
    ) -> None:
        super().__init__(coordinator, entry, route_id, monitor_name, "next_departure")
        self._stop_id = stop_id
        self._direction_id = direction_id

    def _departures(self) -> list[dict]:
        data = self.coordinator.data
        if data is None:
            return []
        return data.gtfs.get_next_departures(
            self._route_id, self._stop_id, self._direction_id, DEPARTURES_TO_SHOW
        )

    @property
    def native_value(self) -> int | None:
        departures = self._departures()
        if not departures:
            return None
        return max(0, int((departures[0]["time"] - datetime.now()).total_seconds() / 60))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        departures = self._departures()
        route = data.gtfs.routes.get(self._route_id, {}) if data else {}
        stop = data.gtfs.stops.get(self._stop_id, {}) if data else {}

        return {
            "stop_name": stop.get("stop_name", self._stop_id),
            "route_name": (
                f"Linea {route.get('route_short_name', '')} {route.get('route_long_name', '')}".strip()
            ),
            "direction": "andata" if self._direction_id == 0 else "ritorno",
            "next_departure_time": departures[0]["departure_time"] if departures else None,
            "next_headsign": departures[0]["headsign"] if departures else None,
            "upcoming_departures": [
                {"time": d["departure_time"], "headsign": d["headsign"]} for d in departures
            ],
            "gtfs_updated": (
                self.hass.data.get(DOMAIN, {}).get("gtfs_cache", {}).get("updated_at")
            ),
        }


class RealtimeEtaSensor(_AmtabSensorBase):
    _attr_translation_key = "realtime_eta"
    _attr_icon = "mdi:bus-marker"
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AmtabCoordinator,
        entry: ConfigEntry,
        route_id: str,
        stop_id: str,
        direction_id: int,
        monitor_name: str,
    ) -> None:
        super().__init__(coordinator, entry, route_id, monitor_name, "realtime_eta")
        self._stop_id = stop_id
        self._direction_id = direction_id

    def _eta_info(self) -> dict | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.gtfs.get_eta_for_approaching_bus(
            self._route_id, self._stop_id, self._direction_id, data.vehicles, data.delays
        )

    @property
    def native_value(self) -> int | None:
        eta_info = self._eta_info()
        if eta_info is None:
            return None
        return max(0, int((eta_info["eta"] - datetime.now()).total_seconds() / 60))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        eta_info = self._eta_info()
        active_count = len(data.vehicles) if data else 0
        rt_updated = data.updated_at.strftime("%H:%M:%S") if data else None

        if eta_info is None:
            if active_count == 0:
                status = "Nessun bus attivo sulla linea"
            else:
                status = (
                    f"{active_count} bus attivi, "
                    "nessuno in avvicinamento alla fermata"
                )
            return {
                "active_buses_on_line": active_count,
                "status": status,
                "rt_updated": rt_updated,
            }

        delay = eta_info.get("delay_seconds", 0)
        return {
            "active_buses_on_line": active_count,
            "bus_id": eta_info.get("bus_id"),
            "scheduled_arrival": eta_info.get("scheduled_arrival"),
            "delay_seconds": delay,
            "delay_minutes": round(delay / 60, 1),
            "latitude": eta_info.get("latitude"),
            "longitude": eta_info.get("longitude"),
            "speed_kmh": eta_info.get("speed"),
            "rt_updated": rt_updated,
        }


class ActiveBusesSensor(_AmtabSensorBase):
    _attr_translation_key = "active_buses"
    _attr_icon = "mdi:bus-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AmtabCoordinator,
        entry: ConfigEntry,
        route_id: str,
        monitor_name: str,
    ) -> None:
        super().__init__(coordinator, entry, route_id, monitor_name, "active_buses")

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        return len(data.vehicles) if data else 0

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}

        buses = []
        for v in data.vehicles:
            veh = v.get("Vehicle", {})
            pos = veh.get("Position", {})
            trip_id = veh.get("Trip", {}).get("TripId", "")
            buses.append({
                "id": v.get("Id", ""),
                "speed_kmh": round(pos.get("Speed", 0), 1),
                "latitude": pos.get("Latitude"),
                "longitude": pos.get("Longitude"),
                "trip_id": trip_id,
                "delay_seconds": data.delays.get(trip_id, 0),
                "current_stop_sequence": veh.get("CurrentStopSequence"),
            })

        return {
            "buses": buses,
            "rt_updated": data.updated_at.strftime("%H:%M:%S"),
        }
