[![](https://img.shields.io/github/release/zeroth-kp/sensus_analytics_integration/all.svg?style=for-the-badge)](https://github.com/zeroth-kp/sensus_analytics_integration/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![](https://img.shields.io/github/license/zeroth-kp/sensus_analytics_integration?style=for-the-badge)](LICENSE)
[![](https://img.shields.io/badge/ORIGINAL%20AUTHOR-%40zestysoft-red?style=for-the-badge)](https://github.com/zestysoft)
[![](https://img.shields.io/badge/FORK%20MAINTAINER-%40zeroth--kp-blue?style=for-the-badge)](https://github.com/zeroth-kp)
[![](https://img.shields.io/badge/COMMUNITY-FORUM-success?style=for-the-badge)](https://community.home-assistant.io)

# HomeAssistant - Sensus Analytics Integration

A custom Home Assistant integration to monitor your water usage from Sensus Analytics.

## Credit and fork notice

This integration was created by **[zestysoft](https://github.com/zestysoft)**. The original project, [zestysoft/sensus_analytics_integration](https://github.com/zestysoft/sensus_analytics_integration), is the foundation of everything here: the Sensus Analytics API client and login flow, the config flow, the sensor set, and the overall integration design are zestysoft's work. Thank you, zestysoft.

This repository is an independently maintained fork of that project, maintained by [@zeroth-kp](https://github.com/zeroth-kp). It has been significantly modified from the original, including:

- Same-day hourly data (the most recent settled hour instead of the matching hour from the previous day), with a configurable settle delay.
- Long-term statistics import and backfill for hourly and daily water usage.
- A progressive tiered-billing model with an included-gallons allowance and up to four tiers priced per thousand gallons.

This fork is not affiliated with or endorsed by zestysoft. Please report problems with this fork to [this repository's issue tracker](https://github.com/zeroth-kp/sensus_analytics_integration/issues), not to the original project. If you want the original integration, install it from [zestysoft/sensus_analytics_integration](https://github.com/zestysoft/sensus_analytics_integration), which is available in HACS by default.

## Features

- **Daily Usage**: Monitors daily water usage.
- **Usage Unit**: Displays the unit of measurement for water usage.
- **Meter Address**: Shows the address of the water meter.
- **Last Read**: Timestamp of the last meter reading.
- **Meter Longitude**: Longitude coordinate of the meter's location.
- **Meter ID**: Unique identifier for the water meter.
- **Meter Latitude**: Latitude coordinate of the meter's location.
- **Meter Odometer**: The total cumulative usage recorded by the meter.
- **Billing Usage**: Total usage amount that has been billed.
- **Billing Cost**: Total cost of the billed usage.
- **Daily Fee**: Daily fee based on usage.
- **Last Hour Usage**: Water usage for the last hour from the previous day.
- **Last Hour Rainfall**: Rainfall data (in inches) for the last hour from the previous day.
- **Last Hour Temperature**: Temperature data (in °F) for the last hour from the previous day.
- **Last Hour Timestamp**: Timestamp of the last hour's data from the previous day.

## Installation via HACS

### **Prerequisites**

- **HACS (Home Assistant Community Store)**: Make sure HACS is installed in your Home Assistant instance. If not, follow the [HACS Installation Guide](https://hacs.xyz/docs/installation/prerequisites).

### **Steps to Install**

1. **Open Home Assistant UI**

   Navigate to your Home Assistant instance in your web browser.

2. **Access HACS**

   - Click on "**HACS**" in the sidebar.

3. **Add Custom Repository**

   - In HACS, go to the "**Integrations**" tab.
   - Click on the "**⋮**" (three dots) menu in the top right corner and select "**Custom repositories**".

4. **Add the Sensus Analytics Repository**

   - **Repository URL**: `https://github.com/zeroth-kp/sensus_analytics_integration`
   - **Category**: Select "**Integration**" from the dropdown menu.
   - Click "**Add**".

5. **Install the Integration**

   - After adding the repository, return to the "**Integrations**" tab in HACS.
   - Search for "**Sensus Analytics Integration**".
   - Click on the integration and then click "**Install**".
   - Wait for HACS to download and install the integration. You should see a confirmation message once it's complete.

6. **Restart Home Assistant**

   - After installation, it's essential to restart Home Assistant to load the new integration.
   - Go to "**Configuration**" > "**Settings**" > "**System**" > "**Restart**".
   - Confirm the restart.

7. **Configure the Integration via Home Assistant UI**

   - Once Home Assistant has restarted, navigate to "**Configuration**" > "**Integrations**".
   - Click the "**+ Add Integration**" button in the bottom right corner.
   - Search for "**Sensus Analytics**" and select "**Sensus Analytics Integration**".
   - Follow the prompts to enter your credentials and settings:
     - **Base URL**: Enter the base URL for your Sensus Analytics API (e.g., `https://<your_city>.sensus-analytics.com/`).
     - **Username**: Your Sensus Analytics account username.
     - **Password**: Your Sensus Analytics account password.
     - **Account Number**: Your Sensus Analytics account number.
     - **Meter Number**: Your water meter number.
     - **Unit Type**: Choose which unit type you want the data to be used by Home Assistant.
     - **Service Fee**: Flat monthly charge just for having service, independent of usage.
     - **Included Gallons**: Gallons already covered by the service fee, billed at $0 (leave blank if your utility has no included allowance).
     - **Tier 1/2/3 Gallons Cutoff**: The *absolute* cumulative monthly usage (in gallons) at which that tier ends and the next begins - not the width of the tier. If your bill's second bracket runs from just above the included allowance up to some total usage figure, that total figure is what goes in Tier 1 Gallons Cutoff (paired with Tier 1 Price below).
     - **Tier 1/2/3/4 Price**: Price **per 1000 gallons** (matching how water bills are quoted) for that tier's bracket - not price per single gallon. Tier 4 has no gallons cutoff; it's always the unbounded top tier, applied to everything above Tier 3's cutoff. Leave a tier's price blank to stop the schedule there (its price then applies unbounded) - e.g. a simple flat-rate utility only needs Tier 1 Price set.
   - Click "**Submit**" to finalize the configuration.

   Example shape for a typical four-tier residential water bill (a flat base fee that includes a starting allowance, then increasingly expensive per-1000-gallon tiers above it):

   | Field | Value |
   |---|---|
   | Service Fee | `20.00` |
   | Included Gallons | `2000` |
   | Tier 1 Gallons Cutoff | `10000` |
   | Tier 1 Price | `4.50` |
   | Tier 2 Gallons Cutoff | `15000` |
   | Tier 2 Price | `5.50` |
   | Tier 3 Gallons Cutoff | `20000` |
   | Tier 3 Price | `6.50` |
   | Tier 4 Price | `9.50` |

   Read your own utility's rate schedule off your bill or its published tariff sheet - the values above are illustrative only, not real rates.

## Sensor Entities

Below are the sensor entities created by this integration:

- `sensor.sensus_analytics_daily_usage`: Daily water usage.
- `sensor.sensus_analytics_usage_unit`: Native unit of measurement chosen by Sensus Analytics.
- `sensor.sensus_analytics_meter_address`: Street address of the water meter.
- `sensor.sensus_analytics_last_read`: Timestamp of the last meter reading.
- `sensor.sensus_analytics_meter_longitude`: Longitude coordinate of the meter's location.
- `sensor.sensus_analytics_meter_id`: Unique identifier for the water meter.
- `sensor.sensus_analytics_meter_latitude`: Latitude coordinate of the meter's location.
- `sensor.sensus_analytics_meter_odometer`: Total cumulative usage recorded by the meter.
- `sensor.sensus_analytics_billing_usage`: Total usage amount that has been billed.
- `sensor.sensus_analytics_billing_cost`: Total cost of the billed usage.
- `sensor.sensus_analytics_daily_fee`: Daily fee based on usage.
- `sensor.sensus_analytics_last_hour_usage`: Water usage for the last hour from the previous day.
- `sensor.sensus_analytics_last_hour_rainfall`: Rainfall for the last hour from the previous day.
- `sensor.sensus_analytics_last_hour_temperature`: Temperature for the last hour from the previous day.
- `sensor.sensus_analytics_last_hour_timestamp`: Timestamp of the last hour's data from the previous day.

# Be kind

This integration exists because of zestysoft's original work. If you find it useful, consider supporting the original author:

[![Buy me a coffee!](https://www.buymeacoffee.com/assets/img/custom_images/black_img.png)](https://www.buymeacoffee.com/zestysoft)

## License

[Apache 2.0](LICENSE). Original work copyright 2024 Zestysoft; modifications in this fork copyright 2026 zeroth-kp. See [NOTICE](NOTICE) for attribution details.
