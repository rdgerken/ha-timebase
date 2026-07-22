# Timebase Historian for Home Assistant

Make [Flow Software's Timebase](https://timebase.flow-software.com/) — a free,
industrial-grade time-series historian — a first-class long-term store for
Home Assistant.

> Timebase speaks OPC UA, MQTT, Sparkplug, and REST, handles 150k writes/sec,
> has no tag-based licensing, runs in Docker on Linux, and ships an MCP server
> so AI agents can query your history. This integration wires it to HA in both
> directions.

## What it does

```
                 ┌──────────────────────────────┐
  state_changed  │  Timebase Historian (:4511)  │   other collectors
 HA ────────────►│  dataset: HomeAssistant      │◄── (OPC UA / MQTT /
     TVQ writes  │  full-res history, purge     │     Sparkplug / Telegraf)
                 │  rules, quality codes        │
 HA ◄────────────│                              │
     hourly stats└──────────────────────────────┘
     + live sensors
```

**Export (HA → Timebase)** — streams every matching entity's state changes as
timestamped TVQ (time/value/quality) samples:

- Timestamps are the entity's `last_updated` — true event time, not receive time.
- Tags are auto-provisioned with the entity's friendly name and unit.
- Boolean-ish states (`on`/`off`, `open`/`closed`, …) map to `1`/`0`.
- Full TVQ quality mapping: `unavailable`/`unknown` writes a hold-last-value
  sample flagged **quality 24** (source comms lost) so trends show the gap
  instead of interpolating across it; HA shutdown posts a **quality 28**
  (collector shutdown) dataset status marker.
- Store-and-forward: samples buffer in order while the historian is down.
- Include/exclude filtering by domain and entity glob.

**Import (Timebase → HA)** — for tags collected from *other* sources (plant or
pool equipment via OPC UA, MQTT devices, …):

- **Long-term statistics**: hourly aggregates inserted via HA's external
  statistics API (`timebase:*` IDs) — visible in native statistics cards.
  Measurement tags import as mean/min/max; **counter tags** (energy, water,
  gas meters) import as state + cumulative sum — meter-reset aware, resumable
  without double-counting, and usable in the Energy dashboard.
- **Live sensors**: any tag as a polling sensor entity with quality + source
  timestamp attributes.

**Services**

- `timebase.write` — historize a computed value from any automation/script.
- `timebase.flush` — force the export buffer to write now.

## Why not just keep everything in recorder?

You keep recorder — smaller. Recorder stays the hot store backing the UI
(per [ADR-0018](https://github.com/home-assistant/architecture/blob/master/adr/0018-supported-databases.md)
it is not replaceable), while Timebase holds full-resolution history with its
own retention. A typical pairing:

```yaml
recorder:
  purge_keep_days: 7   # recorder = hot cache; Timebase = archive
```

Long-term statistics stay in recorder forever (they're tiny), so native energy
and statistics features keep working.

## Installation

1. Run a Timebase historian —
   [Docker quick-start](https://timebase.flow-software.com/en/knowledge-base/quick-start-for-docker)
   or the Windows installer. Note the REST API port (default **4511**).
2. HACS → Custom repositories → add this repo (Integration), install
   **Timebase Historian**, restart HA.
3. Settings → Devices & Services → Add Integration → **Timebase Historian**.
   Enter host/port/dataset; the dataset is created with your retention if
   missing.
4. Open the integration's **Configure** dialog to set export filters and
   import tags.

## Authentication (Timebase Pulse)

Open historians need no credentials. If your system is secured with
**Timebase Pulse** (Timebase's OAuth 2.0 / OIDC identity provider), give the
integration a Pulse client that is allowed the **client-credentials** grant
with the **Historian** audience — a fresh Pulse instance pre-creates suitable
clients (the Collector client fits; a dedicated `HomeAssistant` client is
cleaner). Configure:

- **Pulse URL** — base URL of the Pulse server; the token endpoint is found
  via standard OIDC discovery (`/.well-known/openid-configuration`), with
  `/connect/token` and `/oauth/token` as fallbacks.
- **Client ID / secret** — from the Pulse client.

Tokens are cached, refreshed ~60 s before expiry, and re-fetched once
automatically if the historian returns 401.

## Known limitations (v0.2)

- **Timebase ignores out-of-order writes** — samples older than a tag's
  newest point are dropped by the historian. The exporter preserves order, but
  historical backfill into existing tags is not possible by design.
- Counter imports establish their baseline on first import (the first
  imported hour contributes 0 to the sum); consumption accumulates from
  there.
- State **attributes** are not exported (roadmap).
- Read-response parsing is defensive; verify against your historian's
  Swagger at `http://<host>:4511/api/help` if data looks off.

## Roadmap

- [ ] One-time recorder → Timebase migration tool (fresh tags only)
- [ ] Attribute export
- [ ] Home Assistant **Add-on** wrapping the official Timebase Docker image
      (one-click for HAOS users)
- [ ] Repair issues for buffer overflow / persistent write failures
- [ ] Tests + Integration Quality Scale checklist

## Disclaimer

Not affiliated with Flow Software. "Timebase" is a product of Flow Software
(not to be confused with Deltix TimeBase).

MIT licensed — contributions welcome.
