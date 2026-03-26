"""Constants for the AMTAB Bari integration."""

DOMAIN = "amtab_bari"

# AMTAB data URLs
GTFS_URL = "https://www.amtabservizio.it/gtfs/google_transit.zip"
RT_BASE_URL = "https://avl.amtab.it/WSExportGTFS_RT/api/gtfs"
RT_VEHICLES_ENDPOINT = f"{RT_BASE_URL}/VechiclePosition"
RT_DELAYS_ENDPOINT = f"{RT_BASE_URL}/TripUpdates"

# GTFS cache refresh
GTFS_REFRESH_INTERVAL_HOURS = 24

# Config entry keys (stored in entry.data)
CONF_ROUTE_ID = "route_id"
CONF_STOP_ID = "stop_id"
CONF_DIRECTION_ID = "direction_id"
CONF_MONITOR_NAME = "monitor_name"

# Config entry options keys (stored in entry.options)
CONF_SCAN_INTERVAL = "scan_interval_minutes"
DEFAULT_SCAN_INTERVAL = 1  # minutes
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 60

# How many upcoming departures to show in attributes
DEPARTURES_TO_SHOW = 5

# Directions
DIRECTION_OUTBOUND = 0
DIRECTION_INBOUND = 1

# Shared GTFS cache key in hass.data
GTFS_CACHE_KEY = "gtfs_cache"
