"""Constants for the Sensus Analytics Integration."""

DOMAIN = "sensus_analytics"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # nosec
CONF_ACCOUNT_NUMBER = "account_number"
CONF_METER_NUMBER = "meter_number"
CONF_HOUR_SETTLE_DELAY_MINUTES = "hour_settle_delay_minutes"

DEFAULT_NAME = "Sensus Analytics"
DEFAULT_HOUR_SETTLE_DELAY_MINUTES = 90

CF_TO_GALLON = 7.48052
CF_PER_CCF = 100  # 1 CCF = 100 cubic feet

# Tiered billing prices are $ per this many gallons, matching how water
# utilities quote per-1000-gallon (or per-CCF-equivalent) rates on a bill -
# never $ per single gallon.
GALLONS_PER_PRICING_UNIT = 1000
