# Zinvolt 2

An experimental custom Home Assistant integration based on the
[Zinvolt integration](https://www.home-assistant.io/integrations/zinvolt/).
It uses the original `zinvolt` Python client and is not an official integration.

## Installation

For HACS, add `https://github.com/ronaldd2/zinvolt_2` as a custom repository
with type **Integration**, then download **Zinvolt 2** and restart Home Assistant.

For manual installation:

Copy `custom_components/zinvolt_2` into your Home Assistant
`config/custom_components/zinvolt_2` directory and restart Home Assistant.
Add **Zinvolt 2** under Settings > Devices & services and sign in with your
Zinvolt account. It is a separate integration; existing Zinvolt configuration
and entity IDs are not migrated.

## Energy estimates

Four sensors report approximate charged/discharged energy, both total and today,
in kWh. They integrate net AC socket power (positive API output means discharge)
using the previous sample over each interval. These are not battery-side meters:
solar charging, conversion losses, and flows between polls are not measured.
Totals start at zero on installation and are saved locally; today's counters
reset at midnight in Home Assistant's configured timezone. Missing readings,
gaps over two minutes, and downtime are not estimated. Counters are saved roughly
once per minute and on orderly shutdown/unload; a crash can lose recent energy.

## Power limits and local modes

The new **Configured charge power limit** and **Configured discharge power
limit** numbers are persistent ceilings, separate from the existing live API
power controls. Their initial ceiling is 2000 W, bounded by the advertised
discharge maximum. Lowering a ceiling clamps the live setting immediately;
raising it does not raise the live setting. HA power commands above a ceiling
are rejected. App-side changes above a ceiling are corrected on the next poll.
The existing Maximum output number now reports the actual configured output,
including on unlocked devices.

Every mode selection in HA copies both configured limits to the live charge
and discharge power settings, bounded by the hardware maximum. This deliberately
replaces any lower live setpoints left by the previous mode or the app.
Mode-specific zero overrides take precedence:

- **Inactive:** selects CHARGED with Maximum charge power set to zero, matching
  the reported app behavior. The configured charge limit itself is preserved.
- **Charge only:** sets Maximum output to zero and selects SELF_USE, with charge
  power copied from the configured charge limit.
- **Discharge only (experimental):** selects SELF_USE. If the API reports negative
  socket output (charging), it switches to Inactive (CHARGED with zero charge
  power) for three minutes, then retries SELF_USE. The retry also restores both
  configured limits, including changes made during the pause. Charging detected
  on a subsequent poll starts another pause.

Leaving Inactive restores the configured charge power instead of leaving it
at zero. Selecting Fast charge still uses CHARGED, but with the configured charge
power, so it remains distinct from Inactive. The live charge control is limited
to zero while Inactive or during a discharge-only pause; the separate configured
limit can still be edited for the next mode switch.

This is best-effort polling control, not a safety interlock. It cannot enforce
limits while HA is stopped, offline, or unable to reach the API. Local modes and
limits survive restarts; native mode changes in the app can be overridden while
a local mode is selected. Choose a native mode in HA to stop the local policy.

## API mode diagnostics

The pinned `zinvolt==1.1.1` client defines DYNAMIC, CHARGED, PERFORMANCE, DEFAULT,
FEED, CUSTOM, and SELF_USE, but **no IDLE**. This integration preserves the raw
API string, even when it is unknown to that enum. Inactive is implemented using
CHARGED plus zero charge power, not a new API enum value. The mode selector also
recognizes that combination when it is reported by the app.

1. Select a native mode in HA (for example Self-use) to leave any local mode.
2. Select inactive in the Zinvolt app.
3. Wait for the next poll (normally 30 seconds) and inspect **API smart mode**
   in the battery's diagnostic entities.

For logs, enable the following and look for `Zinvolt API smart mode changed to`:

```yaml
logger:
  logs:
    custom_components.zinvolt_2.control: info
```

Only the mode string is logged by this message, not account credentials or the
full API response. Downloaded integration diagnostics also contain the mode.

## Troubleshooting

If a battery unit's details fail to load, its point sensors become unavailable
while the battery's other data remains available. The integration retries on
each refresh and adds point sensors if the unit subsequently becomes available.
Authentication failures still require reauthentication. Battery discovery and
the main battery status are required; if either fails, setup may still fail.
If the main status request fails, its underlying exception is logged.
Check the Home Assistant logs for the underlying error. Remove credentials and
identifying information before sharing logs.

Run the focused tests with `python -m unittest discover -s tests -v` in an
environment with `zinvolt==1.1.1` installed. They exercise the real client models
but use Home Assistant stubs, and do not replace testing on a real installation.

This is a modified copy of Home Assistant's Apache-2.0 licensed Zinvolt
integration. Changes include renaming the domain, custom integration metadata
and translations, handling failed unit-detail requests, energy estimates,
power ceilings, local modes, and mode diagnostics. Modified upstream files are
`__init__.py`, `const.py`, `manifest.json`, `strings.json`, `coordinator.py`,
`binary_sensor.py`, `number.py`, `sensor.py`, and `select.py`. The original license
is included in this repository. The client adapter reuses the upstream transport
helpers because the public API parses smart modes as a closed enum; the
dependency remains pinned and the raw-status parser is tested.
