"""Shared pytest fixtures for the Sensus Analytics integration tests.

Deliberately NOT wrapping `enable_custom_integrations` in an autouse
fixture here: autouse fixtures are resolved before explicitly-requested
ones of the same scope, which forces `hass` to be instantiated before
`recorder_mock` gets a chance to configure the recorder - and
pytest-homeassistant-custom-component asserts `hass` hasn't been set up
yet when `recorder_mock` runs. Tests that need custom_components
discoverable should request `enable_custom_integrations` explicitly,
after `recorder_mock` in the parameter list when both are needed.
"""

from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _disable_statistics_poll(request):
    """Keep the statistics importer's poll hook from running unless a test opts in.

    The hook starts a background sync after every coordinator refresh. Tests
    that don't expect it would otherwise leave that sync reading the recorder
    database after the test ends and the database is closed. Tests of the
    hook itself use the ``statistics_poll`` marker and wait for its task.
    Patching only - this fixture deliberately does not request ``hass``.
    """
    if request.node.get_closest_marker("statistics_poll"):
        yield
        return
    with patch("custom_components.sensus_analytics.statistics.WaterStatisticsImporter.async_handle_coordinator_update"):
        yield
