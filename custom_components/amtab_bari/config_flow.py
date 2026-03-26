from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DIRECTION_ID,
    CONF_MONITOR_NAME,
    CONF_ROUTE_ID,
    CONF_SCAN_INTERVAL,
    CONF_STOP_ID,
    DEFAULT_SCAN_INTERVAL,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .gtfs_parser import AmtabGtfsData, async_download_gtfs, parse_gtfs

_LOGGER = logging.getLogger(__name__)


class AmtabBariConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> AmtabBariOptionsFlow:
        return AmtabBariOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._gtfs: AmtabGtfsData | None = None
        self._route_id: str | None = None
        self._direction_id: int = DIRECTION_OUTBOUND

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if self._gtfs is None:
            session = async_get_clientsession(self.hass)
            try:
                raw = await async_download_gtfs(session)
                self._gtfs = await self.hass.async_add_executor_job(parse_gtfs, raw)
            except Exception:
                _LOGGER.exception("Failed to download GTFS data")
                errors["base"] = "cannot_connect"

        if user_input is not None and not errors:
            self._route_id = user_input[CONF_ROUTE_ID]
            self._direction_id = int(user_input[CONF_DIRECTION_ID])
            return await self.async_step_select_stop()

        routes = self._gtfs.get_routes() if self._gtfs else []
        route_options = [
            SelectOptionDict(
                value=r["route_id"],
                label=(
                    f"Linea {r.get('route_short_name', r['route_id'])} — "
                    f"{r.get('route_long_name', '')}"
                ).strip(" —"),
            )
            for r in routes
        ]

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROUTE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=route_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_DIRECTION_ID, default=str(DIRECTION_OUTBOUND)
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=str(DIRECTION_OUTBOUND), label="→ Andata"),
                                SelectOptionDict(value=str(DIRECTION_INBOUND), label="← Ritorno"),
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    def _get_headsign(self) -> str:
        """Return the trip_headsign for the selected route+direction, or empty string."""
        assert self._gtfs is not None
        assert self._route_id is not None
        for trip in self._gtfs.trips.values():
            if (
                trip["route_id"] == self._route_id
                and str(trip.get("direction_id", "0")) == str(self._direction_id)
            ):
                headsign = trip.get("trip_headsign", "")
                if headsign:
                    return headsign
        return ""

    async def async_step_select_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        assert self._gtfs is not None
        assert self._route_id is not None

        if user_input is not None:
            stop_id = user_input[CONF_STOP_ID]
            stop = self._gtfs.stops.get(stop_id, {})
            route = self._gtfs.routes.get(self._route_id, {})
            route_short = route.get("route_short_name", self._route_id)
            stop_name = stop.get("stop_name", stop_id)

            headsign = self._get_headsign()
            if headsign:
                default_name = f"Linea {route_short} - {stop_name} → {headsign}"
            else:
                direction_label = "andata" if self._direction_id == DIRECTION_OUTBOUND else "ritorno"
                default_name = f"Linea {route_short} - {stop_name} ({direction_label})"
            monitor_name = user_input.get(CONF_MONITOR_NAME, "").strip() or default_name

            unique_id = f"{self._route_id}_{stop_id}_{self._direction_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=monitor_name,
                data={
                    CONF_ROUTE_ID: self._route_id,
                    CONF_STOP_ID: stop_id,
                    CONF_DIRECTION_ID: self._direction_id,
                    CONF_MONITOR_NAME: monitor_name,
                },
            )

        stops = self._gtfs.get_stops_for_route(self._route_id, self._direction_id)
        if not stops:
            stops = sorted(
                self._gtfs.stops.values(),
                key=lambda s: s.get("stop_name", ""),
            )

        stop_options = [
            SelectOptionDict(value=s["stop_id"], label=s.get("stop_name", s["stop_id"]))
            for s in stops
        ]

        route = self._gtfs.routes.get(self._route_id, {})
        route_short = route.get("route_short_name", self._route_id)
        direction_label = "andata" if self._direction_id == DIRECTION_OUTBOUND else "ritorno"
        headsign = self._get_headsign()
        name_preview = (
            f"Linea {route_short} - ... → {headsign}"
            if headsign
            else f"Linea {route_short} - ... ({direction_label})"
        )

        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=stop_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_MONITOR_NAME,
                        default=name_preview,
                    ): str,
                }
            ),
        )


class AmtabBariOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current_interval): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="min",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
