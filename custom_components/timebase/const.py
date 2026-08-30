"""Constants for the Timebase Historian integration."""

from __future__ import annotations

DOMAIN = "timebase"

# --- Connection (config entry data) ---
CONF_DATASET = "dataset"
CONF_RETENTION_DAYS = "retention_days"

CONF_USE_SSL = "use_ssl"
CONF_VERIFY_SSL = "verify_ssl"

# Pulse (Timebase's OIDC identity provider) — optional, for secured historians.
CONF_PULSE_URL = "pulse_url"
CONF_PULSE_CLIENT_ID = "pulse_client_id"
CONF_PULSE_CLIENT_SECRET = "pulse_client_secret"

DEFAULT_PORT = 4511
DEFAULT_DATASET = "HomeAssistant"
DEFAULT_RETENTION_DAYS = 1825  # 5 years; Timebase purge-age (pa) on the dataset

# --- Export (options) ---
CONF_EXPORT_ENABLED = "export_enabled"
CONF_TAG_PREFIX = "tag_prefix"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITY_GLOBS = "include_entity_globs"
CONF_EXCLUDE_ENTITY_GLOBS = "exclude_entity_globs"
CONF_EXPORT_STRING_STATES = "export_string_states"
CONF_EXPORT_ATTRIBUTES = "export_attributes"

DEFAULT_EXPORT_ENABLED = True
DEFAULT_TAG_PREFIX = "ha"
DEFAULT_FLUSH_INTERVAL_SECONDS = 10
MAX_BUFFERED_TVQS = 100_000  # store-and-forward bound; oldest dropped beyond this

# --- Import / statistics (options) ---
CONF_IMPORT_TAGS = "import_tags"
CONF_IMPORT_COUNTER_TAGS = "import_counter_tags"
CONF_LIVE_TAGS = "live_tags"

DEFAULT_IMPORT_INTERVAL_MINUTES = 30
DEFAULT_LIVE_SCAN_INTERVAL_SECONDS = 30
IMPORT_MAX_LOOKBACK_HOURS = 24 * 7  # cap first import / catch-up window

# --- Timebase quality codes (OPC-DA style) ---
QUALITY_GOOD = 192
QUALITY_COMMS_LOST = 24  # source comms lost — used for unavailable entities
QUALITY_COLLECTOR_SHUTDOWN = 28  # posted as dataset status on HA stop

# HA states mapped to numeric TVQs for boolean-ish entities.
BINARY_STATE_MAP = {
    "on": 1.0,
    "off": 0.0,
    "true": 1.0,
    "false": 0.0,
    "home": 1.0,
    "not_home": 0.0,
    "open": 1.0,
    "closed": 0.0,
    "locked": 1.0,
    "unlocked": 0.0,
}

# --- Services ---
SERVICE_FLUSH = "flush"
SERVICE_WRITE = "write"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TAG = "tag"
ATTR_VALUE = "value"
ATTR_TIMESTAMP = "timestamp"
