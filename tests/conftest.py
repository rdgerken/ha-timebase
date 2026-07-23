"""Shared fixtures for ha-timebase tests.

No autouse fixtures on purpose: `recorder_mock` must be instantiated BEFORE
`hass` (the plugin asserts this), so tests request their fixtures explicitly
in the right order — e.g. (recorder_mock, enable_custom_integrations, hass).
"""
