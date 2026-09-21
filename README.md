# ESPHome MCP Bridge

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jrigling&repository=esphome-mcp-integration&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=esphome_mcp_bridge)

Drive a **full ESPHome development cycle from an AI agent** (e.g. Claude Code
via Home Assistant's MCP server). This repo ships a Home Assistant custom
integration that registers a custom LLM API exposing ESPHome tools, backed by a
small reusable PyPI client library.

```
AI agent ── MCP ──► Home Assistant ──► ESPHome Builder LLM API
                                          │
                          ┌───────────────┴───────────────┐
                          ▼                                ▼
                 /config/esphome (files)        ESPHome dashboard add-on
                                                (stable / beta / dev) via
                                                Supervisor internal network
```

## Why this works without sidecars

The integration runs **inside Home Assistant Core**, so it has:

- **Native filesystem access** to `/config/esphome` for reading, creating, and
  writing YAML — reliable and independent of which add-on is installed.
- **Internal network access** to the ESPHome dashboard add-on through the
  Supervisor, reaching the dashboard's container directly (no ingress auth, no
  exposed ports) for validate / compile / upload / run / logs.

No changes to the ESPHome add-on and no external containers required.

## The development-cycle tools

The `ESPHome Builder` LLM API exposes these tools:

| Tool | Purpose |
|------|---------|
| `esphome_list_addons` | Discover installed ESPHome channels (stable/beta/dev), versions, state |
| `esphome_list_devices` | Inventory devices: name, version, platform, integrations, **online** status |
| `esphome_read_yaml` | Read a config from `/config/esphome` |
| `esphome_create_config` | Create a **new** config (fails if it exists) |
| `esphome_write_yaml` | Overwrite an existing config |
| `esphome_patch_yaml` | Edit an existing config via exact-match `{old_string, new_string}` replacements, without resending the whole file |
| `esphome_add_secret` | Insert a key into `secrets.yaml` — **insert-only, write-only** (never reads/returns values; errors if the key exists) |
| `esphome_validate` | Validate a config without building |
| `esphome_compile` | Compile firmware (runs to completion, returns log + exit code) |
| `esphome_upload` | Flash firmware to a device (`port` defaults to `OTA`) |
| `esphome_run` | Compile **and** flash in one step (dashboard "Install") |
| `esphome_logs` | Capture a bounded window of live device logs — streams **directly from the device** (works on classic dashboard *and* Device Builder) when it's adopted in HA, else falls back to the dashboard |
| `esphome_clean` | Remove cached build artifacts |

A typical agent flow: **list devices → read/create config → write → validate →
compile → run → check logs**.

Each build tool accepts an optional `addon_slug` to target a specific channel;
omit it to use the default (stable preferred over beta over dev).

## Multi-channel discovery

`esphome_list_addons` queries the Supervisor for every installed add-on whose
slug or name matches `esphome` (`5c53de3b_esphome`, `…_esphome-beta`,
`…_esphome-dev`). The bridge resolves each add-on's internal hostname and port
from the Supervisor, so it adapts automatically rather than hard-coding a URL.

## Requirements

- Home Assistant with the Supervisor (HA OS or Supervised install)
- An ESPHome dashboard add-on installed (classic dashboard **or** the new
  Device Builder — both speak the protocol this bridge uses)
- Home Assistant 2024.4.0+ (LLM API helpers)

## Installation

### HACS (integration)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jrigling&repository=esphome-mcp-integration&category=integration)

1. Click the badge above (or add this repository as a custom HACS repository,
   type *Integration*).
2. Install **ESPHome MCP Bridge** and restart Home Assistant.
3. Add the integration — click the badge below, or go to **Settings → Devices &
   Services → Add Integration**, search **ESPHome MCP Bridge**, and select
   **Submit**. There's nothing to configure — it's a single-instance,
   confirm-only setup.

   [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=esphome_mcp_bridge)
4. The `ESPHome Builder` LLM API is now registered. To actually expose it to an
   AI agent, point Home Assistant's MCP Server at it — see
   [Connecting an AI agent](#connecting-an-ai-agent-mcp) below.

### Manual

Copy `custom_components/esphome_mcp_bridge/` into your HA
`config/custom_components/`, restart, then add it from **Settings → Devices &
Services → Add Integration** as above.

> Home Assistant installs the `esphome-mcp-client` PyPI dependency
> automatically (declared in the integration manifest).

## Connecting an AI agent (MCP)

Installing this integration only **registers** the *ESPHome Builder* LLM API —
it does not expose it to your agent by itself. Home Assistant's separate
**Model Context Protocol Server** integration is what serves an API's tools to
MCP clients (Claude Desktop, Claude Code, …).

1. Install and add **ESPHome MCP Bridge** (above) so the *ESPHome Builder* API
   is registered.
2. Add the **Model Context Protocol Server** integration: **Settings → Devices &
   Services → Add Integration → Model Context Protocol Server**.
3. In its dialog, under **Control Home Assistant**, tick **ESPHome Builder**
   (you can leave **Assist** ticked too — it's multi-select), then **Submit**.
4. Point your MCP client at Home Assistant's MCP server endpoint (the MCP Server
   integration's docs give the SSE URL + token), then refresh its tool list. The
   ESPHome tools (`esphome_list_devices`, `esphome_write_yaml`,
   `esphome_compile`, …) will appear.

> **Already had the MCP Server set up before installing this?** The API list is
> read when that integration is configured, so **ESPHome Builder won't be
> listed**. Delete the Model Context Protocol Server integration and re-add it
> so the checkbox appears.

If **ESPHome Builder** is missing from the checkbox list in step 3, the
integration isn't loaded — check the logs and confirm it's on the latest version.

## Security

- File tools are confined to `/config/esphome`; directory traversal (`..`, `/`,
  `\`) is rejected outright.
- `secrets.yaml` / `secrets.yml` are blocked from read, write, create, and build.
- Writes/creates accept only `.yaml` / `.yml`.
- Supervisor requests use the `SUPERVISOR_TOKEN` injected at runtime — no
  credentials are stored.

## Repository layout

- `esphome_mcp_client/` — reusable async transport library → published to PyPI
  as **`esphome-mcp-client`** (see [PYPI_SETUP.md](PYPI_SETUP.md)).
- `custom_components/esphome_mcp_bridge/` — the Home Assistant integration
  (LLM tool definitions + API registration).
- `tests/` — unit tests for the client library.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check esphome_mcp_client tests
pytest                                   # unit tests (fast)
pip install esphome                      # only needed for the lines below
pytest tests/integration -m integration  # smoke tests vs a real dashboard
```

## Testing approach

Two layers, both deliberately avoiding fakes of the things most likely to drift:

**Unit tests run against a real in-process `aiohttp` server, not a mocking
library.** We originally used [`aioresponses`](https://github.com/pnuckowski/aioresponses),
but `aiohttp` 3.12 added a required `stream_writer` argument to `ClientResponse`
that aioresponses doesn't pass — and there is no fixed release. Worse, it passed
locally (our dev venv happened to pin an older `aiohttp` transitively) and only
failed in CI on the latest `aiohttp`. A mock that reimplements `aiohttp`'s
internals can silently fall out of sync with `aiohttp`; a tiny real server
(`tests/conftest.py`) cannot. So we serve canned responses from an actual
`aiohttp.web` app on a loopback port and exercise the real client against it.

**Integration smoke tests run against a real `esphome dashboard`**
(`tests/integration/`), launched on a temp config dir, exercising the REST
endpoints and the WebSocket spawn protocol end to end. They auto-skip when
ESPHome isn't installed, so the unit run is unaffected.

### Detecting upstream API drift

The dashboard protocol is an external contract ESPHome controls, so we watch it
actively rather than waiting for user reports:

- **Scheduled smoke workflow** ([`smoke-test.yml`](.github/workflows/smoke-test.yml))
  runs the integration tests weekly across the **stable / beta / dev** ESPHome
  channels. If ESPHome changes the API, a build goes red before users hit it.
  (The `dev` channel is allowed-failure; `compile` is gated behind a manual
  dispatch as it downloads a full toolchain.)
- **Defensive client logging** — `DashboardClient.inventory()` logs a warning if
  the `/devices` response loses its expected shape, and `version()` surfaces the
  dashboard's ESPHome version, so drift becomes a visible log line for triage.

## License

MIT © Justin Rigling
