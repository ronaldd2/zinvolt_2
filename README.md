# Zinvolt 2

An experimental custom Home Assistant integration based on the
[Zinvolt integration](https://www.home-assistant.io/integrations/zinvolt/).
It uses the original `zinvolt` Python client and is not an official integration.

## Installation

Copy `custom_components/zinvolt_2` into your Home Assistant
`config/custom_components/zinvolt_2` directory and restart Home Assistant.
Add **Zinvolt 2** under Settings > Devices & services and sign in with your
Zinvolt account. It is a separate integration; existing Zinvolt configuration
and entity IDs are not migrated.

## Troubleshooting

If a battery unit's details fail to load, its point sensors become unavailable
while the battery's other data remains available. The integration retries on
each refresh and adds point sensors if the unit subsequently becomes available.
Authentication failures still require reauthentication. Battery discovery and
the main battery status are required; if either fails, setup may still fail.
If the main status request fails, its underlying exception is logged.
Check the Home Assistant logs for the underlying error. Remove credentials and
identifying information before sharing logs.

Run the focused tests with `python -m unittest discover -s tests -v`. They use
Home Assistant stubs and do not replace testing against a real installation.

This is a modified copy of Home Assistant's Apache-2.0 licensed Zinvolt
integration. Changes include renaming the domain, custom integration metadata
and translations, and handling failed unit-detail requests. The modified
upstream files are `const.py`, `manifest.json`, `strings.json`, `coordinator.py`,
and `binary_sensor.py`. The original license is included in this repository.
