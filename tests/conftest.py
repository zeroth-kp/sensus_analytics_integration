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

pytest_plugins = "pytest_homeassistant_custom_component"
