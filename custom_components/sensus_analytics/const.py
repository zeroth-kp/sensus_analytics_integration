"""Constants for the Sensus Analytics Integration."""

DOMAIN = "sensus_analytics"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # nosec
CONF_ACCOUNT_NUMBER = "account_number"
CONF_METER_NUMBER = "meter_number"
CONF_HOUR_SETTLE_DELAY_MINUTES = "hour_settle_delay_minutes"
CONF_STATISTICS_TARGET = "statistics_target"

DEFAULT_NAME = "Sensus Analytics"
DEFAULT_HOUR_SETTLE_DELAY_MINUTES = 90

# Which statistic the hourly water-statistics importer writes. "shadow" is a
# separate statistic nothing reads, for running alongside the legacy writers;
# "live" is the Daily Usage sensor's own statistic, and disables the legacy
# writers so the importer is its only writer.
STATISTICS_TARGET_SHADOW = "shadow"
STATISTICS_TARGET_LIVE = "live"
STATISTICS_TARGETS = (STATISTICS_TARGET_SHADOW, STATISTICS_TARGET_LIVE)
DEFAULT_STATISTICS_TARGET = STATISTICS_TARGET_SHADOW

CF_TO_GALLON = 7.48052
CF_PER_CCF = 100  # 1 CCF = 100 cubic feet

# Water-usage statistics importer (statistics.py).
# Days before now that every poll-triggered sync rewrites, so late Sensus
# corrections and the midnight rollover are picked up automatically.
SYNC_TRAILING_DAYS = 3
# How far back the retention probe searches for the oldest day with hourly data.
PROBE_MAX_DAYS = 450
# Upper bound on a single hour's usage, in gallons. Above the maximum
# continuous flow of any residential meter (a 1" meter tops out around
# 50 gpm, roughly 3000 gal/hour), so it only rejects corrupt or
# misattributed data, never real usage.
MAX_PLAUSIBLE_HOURLY_USAGE_GAL = 5000
# Hours written per recorder job, keeping each database transaction short.
IMPORT_CHUNK_HOURS = 31 * 24
# Retries per day when fetching history, with exponential backoff (seconds).
FETCH_RETRIES = 3
FETCH_RETRY_DELAY_SECONDS = 2.0
# Pause between day requests on multi-day fetches.
FETCH_DAY_DELAY_SECONDS = 0.5

# Tiered billing prices are $ per this many gallons, matching how water
# utilities quote per-1000-gallon (or per-CCF-equivalent) rates on a bill -
# never $ per single gallon.
GALLONS_PER_PRICING_UNIT = 1000
