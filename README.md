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
- Store-and-forward: samples buffer in order while the historian is down;
  Repair issues surface buffer overflow and persistent write failures.
- Opt-in **attribute export**: name the attributes (e.g. `brightness`,
  `current_temperature`) and their numeric values export as
  `<prefix>.<entity_id>.<attribute>` tags — including on attribute-only changes.
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

   > 💡 **Memory tip (Docker, homelab-scale):** the Timebase images run
   > .NET's Server GC default — per-core heaps sized for industrial ingest
   > rates. At home-automation write rates, adding `DOTNET_gcServer: "0"`
   > (workstation GC) plus a container memory limit cut the historian from
   > ~300 to ~96 MiB in our testing, with a 96,000-point query going
   > 69 → 111 ms — negligible. Skip this if you feed it heavy industrial
   > collectors; Server GC is the right default there.
2. HACS → Custom repositories → add this repo (Integration), install
   **Timebase Historian**, restart HA.
3. Settings → Devices & Services → Add Integration → **Timebase Historian**.
   Enter host/port/dataset; the dataset is created with your retention if
   missing.
4. Open the integration's **Configure** dialog to set export filters and
   import tags.

## Using the data in dashboards

Two kinds of data come back from the historian, and they feed **different card
families**:

### 1. Imported statistics → `statistics-graph` cards

Tags you list under **"Measurement tags"** / **"Counter tags"** in the
integration's options are aggregated hourly into HA's native long-term
statistics store, under IDs of the form `timebase:<tag_slug>` (dots become
underscores — find yours under **Developer Tools → Statistics**, filter
"timebase"). These are *statistic IDs, not entities* — they work in any
statistics card:

```yaml
# Measurement tag: hourly mean with a min/max band
type: statistics-graph
title: Pool water — hourly mean / min / max
period: hour
days_to_show: 7
stat_types: [mean, min, max]
entities:
  - entity: timebase:ha_sensor_pool_water_temperature
    name: Pool water
```

```yaml
# Counter tag (energy/water/gas): consumption per hour as bars
type: statistics-graph
title: Energy — hourly consumption
chart_type: bar
period: hour
days_to_show: 2
stat_types: [change]
entities:
  - timebase:energy_meter
```

Counter statistics carry `state` + monotonic `sum` (meter-reset aware) — the
same shape HA's own utility meters produce.

### 2. Live tag sensors → any normal card

Tags listed under **"Tags to expose as live sensors"** become ordinary sensor
entities (named `sensor.timebase_historian_<tag>`) with `quality` and
`source_timestamp` attributes. Use them anywhere an entity works — tiles,
gauges, `history-graph`, conditions in automations:

```yaml
type: tile
entity: sensor.timebase_historian_dataset_writes
name: Historian write rate
icon: mdi:pulse
```

Tip: exposing the historian's own **System tags** (`Dataset.Writes`,
`Dataset.Tags`, `Dataset.Size`, `Process.Memory`) as live sensors gives you a
"historian health" section on any dashboard — the historian monitoring itself
from inside HA.

### Worked example

A complete sections-view dashboard combining all of the above (trend graphs,
counter bars, live health tiles) is in
[`docs/example-dashboard.yaml`](docs/example-dashboard.yaml) — paste the view
into any storage-mode dashboard via the raw configuration editor.

<!-- Screenshots pending:
![Trend and counter cards](docs/images/statistics-cards.png)
![Historian health tiles](docs/images/health-tiles.png)
-->
*(Screenshots coming — the example view above is running live and renders
exactly as configured.)*

## Bonus: Grafana straight from the historian

Not part of this integration, but a natural companion (verified working):
Grafana's signed [Infinity datasource](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
can query the historian's REST API directly — with **quality codes as a
field**, which no generic TSDB path gives you.

Datasource settings (two non-obvious parts marked ⚠):

- Auth: **OAuth2 → Client credentials**; token URL
  `https://<pulse-host>:4542/auth/token`, your client ID + secret
- ⚠ **Auth style: "In Params"** — Pulse only accepts credentials in the POST
  body; the default (HTTP Basic) fails with *"Invalid client secret provided"*
- Skip TLS verify (private Pulse CA), and add the historian + Pulse hosts to
  allowed hosts
- ⚠ If provisioning from YAML, the secret key is camelCase:
  `secureJsonData: { oauth2ClientSecret: ... }`, and auth style is
  `jsonData: { oauth2: { authStyle: 1 } }`

Query (type JSON, backend parser, format time series):

```
URL:           https://<historian>:4512/api/datasets/<dataset>/data?tagname=<tag>&start=${__from:date:iso}&end=${__to:date:iso}
Root selector: tl[0].d
Columns:       t → timestamp | v → number | q → number
```

## Authentication (Timebase Pulse)

Open historians need no credentials. If your system is secured with
**Timebase Pulse** (Timebase's OAuth 2.0 / OIDC identity provider), give the
integration a Pulse client that is allowed the **client-credentials** grant
with the **Historian** audience — a fresh Pulse instance pre-creates suitable
clients (the Collector client fits; a dedicated `HomeAssistant` client is
cleaner). Configure:

- **Pulse URL** — base URL of the Pulse server, e.g. `https://pulse-host:4542`.
  Pulse nests its IdP under `/auth` (discovery at
  `/auth/.well-known/openid-configuration`, tokens at `/auth/token`); the
  integration finds it automatically from the bare base URL.
- **Client ID / secret** — from the Pulse client.

Tokens are cached, refreshed ~60 s before expiry, and re-fetched once
automatically if the historian returns 401.

### Enabling auth on a fresh Timebase stack (verified on 1.3.x)

Auth ships **disabled**. To turn it on you need three things, or the
historian rejects every token with issuer errors (IDX10204):

1. **Pulse settings** (`/settings/settings.config` in the Pulse container):
   replace the `Auth.Issuer` placeholder (`https://<YourIssuerDomain>`) with
   a real value, e.g. `https://pulse:4542`.
2. **Historian settings**: set `Auth.Enabled: true` and `Auth.ClientSecret`
   to the Historian client's secret (Pulse stores the generated client
   secrets in its `config/clients.config`, MessagePack-encoded).
3. **TLS hostname**: the historian validates Pulse's certificate against the
   host it dials (`Auth.IdP.Host`). Pulse mints its cert SANs from its
   *machine hostname* — in Docker, set `hostname: pulse` on the Pulse
   container (matching the historian's `IdP.Host`) and delete the generated
   leaf cert (`certificates/timebase-generated.pfx`, keep the `-ca` files)
   so it re-mints with the right SAN. Then restart the historian so it
   re-fetches the discovery metadata.

## Known limitations

- **Timebase drops out-of-order writes — silently.** Empirically verified
  against a live historian: any sample older than a tag's newest stored point
  is rejected, even by 5 minutes within the current hour block, and **the API
  still returns 200** (the only signal is a "late data rejected" warning in
  the historian's log). The exporter therefore preserves strict per-tag order;
  historical backfill works into *fresh* tags only.
- Counter imports establish their baseline on first import (the first
  imported hour contributes 0 to the sum); consumption accumulates from
  there.
- API payload shapes were verified against a live Timebase **1.3.x**
  historian (they differ from the published docs in several places); other
  versions may drift — `http://<host>:4511/api/help` is the ground truth.

## Roadmap

- [ ] One-time recorder → Timebase migration tool (fresh tags only)
- [ ] Home Assistant **Add-on** wrapping the official Timebase Docker image
      (one-click for HAOS users)
- [ ] Integration Quality Scale tiering (test suite + hassfest/HACS CI ship
      today; the formal checklist climb is ongoing)

## Disclaimer

Not affiliated with Flow Software. "Timebase" is a product of Flow Software
(not to be confused with Deltix TimeBase).

MIT licensed — contributions welcome.
