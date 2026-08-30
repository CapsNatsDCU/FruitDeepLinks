# FruitDeepLinks

<img src="templates/logo.png" alt="FruitDeepLinks" width="320">

**Universal Sports Streaming Aggregator — v2.0.0**

FruitDeepLinks scrapes Apple TV's Sports aggregation API plus 10 regional services to build a unified sports EPG with deeplinks to 24+ streaming apps. Export M3U/XMLTV for Channels DVR, ADBTuner, CC4C, and PrismCast.

[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

---

## 🎯 The Problem

Sports streaming is fragmented:

- NFL on Prime Video (Thursday), ESPN+ (Monday), Peacock (Sunday)
- MLS exclusively on Apple TV
- College sports scattered across ESPN+, Paramount+, Peacock, and more
- You have multiple subscriptions but need to check multiple apps just to find games

## ✨ The Solution

FruitDeepLinks creates virtual TV channels in Channels DVR with deeplinks that launch directly into your streaming apps.

**One EPG. All your sports. All your services.**

---

## 🆕 What's New in v2.0.0

### v2 Server Refactor

- **Flask app factory** — clean blueprint-based routing replaces the monolithic server
- **Settings page** (`/settings`) — configure server URL, DVR IP, lane counts, refresh schedule, and per-scraper toggles directly in the UI; no `.env` edits required
- **Service catalog** (`core/service_catalog.py`) — single source of truth for all display names, internal priorities, and user-facing defaults
- **DB access layer** (`db/`) — `get_conn()` context manager, `preferences.py` CRUD, `stats.py` for the dashboard
- **Per-scraper on/off toggles** — disable Kayo, Fanatiz, beIN, NESN, Victory+, Gotham, or ESPN individually from the Settings page; env vars still work as hard overrides
- **Structured progress tracking** — refresh pipeline emits structured JSON markers for real-time step tracking in the dashboard

### Previously Added

- **Amazon Channel Integration** — identifies which Prime Video channel (NBA League Pass, DAZN, FOX One, Max, ViX, etc.) each event requires; stored in `amazon_channels` table
- **ESPN Watch Graph API** — enriches ESPN events with Fire TV-compatible deeplinks (~70% match rate); falls back to Apple TV deeplinks for unmatched events
- **ADB Lanes with device profiles** — `/m3u/adb` for Fire TV/Android (scheme URLs), `/m3u/adb?profile=apple` for Apple TV (HTTPS URLs); per-provider variants available
- **Regional scrapers** — Kayo Sports, Fanatiz Soccer, beIN Sports, NESN, Victory+, Gotham Sports (MSG/YES)

---

## 🚀 Quick Start (Portainer – Recommended)

These steps assume you already have **Docker** and **Portainer** running on your server.

### 1. Add a Git-backed stack in Portainer

1. Open Portainer in your browser.
2. Go to **Stacks → Add stack**.
3. Choose the **Repository** method.
4. Fill in:
   - **Name:** `fruitdeeplinks`
   - **Repository URL:** `https://github.com/kineticman/FruitDeepLinks.git`
   - **Repository reference:** `main`
   - **Compose path:** `docker-compose.yml`

### 2. Set environment variables

Most users only need these four:

```env
SERVER_URL=http://192.168.1.100:6655     # IP of this server, as seen by your devices
FRUIT_HOST_PORT=6655
CHANNELS_DVR_IP=192.168.1.100           # IP of your Channels DVR server
TZ=America/New_York
```

Additional optional variables:

```env
CHANNELS_SOURCE_NAME=fruitdeeplinks-direct   # must match your Channels Custom Channels source name

# Override scraper defaults (also configurable in the Settings page)
KAYO_ENABLED=false         # disable if you don't have Kayo
FANATIZ_ENABLED=false      # disable if you don't watch Fanatiz
BEIN_ENABLED=false
NESN_ENABLED=false
VICTORY_ENABLED=false
GOTHAM_ENABLED=false
ESPN_ENABLED=true          # ESPN Watch Graph enrichment

# Optional Xtream IPTV ingestion. Never commit real credentials.
XTREAM_ENABLED=false
XTREAM_SERVER_URL=http://iptv-provider.example:8080
XTREAM_USERNAME=your-username
XTREAM_PASSWORD=your-password
XTREAM_CATEGORY_IDS=123,456       # required; the full catalogue is never imported
XTREAM_TIMEZONE=America/New_York  # for dated names/timestamps without an offset
XTREAM_DEFAULT_DURATION_MINUTES=180
```

> **Tip:** After first launch, visit `/settings` to configure everything from the UI — server URL, DVR IP, refresh schedule, lane counts, and scraper toggles. Settings saved there persist across restarts and take precedence over env vars (except scraper env vars, which remain a hard override).

### 3. Deploy and open the dashboard

1. Click **Deploy the stack**.
2. Open in your browser:

```
http://<your-server-ip>:6655
```

Run an initial refresh from the dashboard to populate the database.

---

## ➕ Alternative: Docker Compose (without Portainer)

```bash
git clone https://github.com/kineticman/FruitDeepLinks.git
cd FruitDeepLinks

cp .env.example .env
# Edit .env with your LAN IP, timezone, Channels DVR IP

docker compose up -d
# Web UI: http://localhost:6655
```

---

## ➕ Alternative: Plain `docker run` (no Compose, no Portainer)

If you're just pulling and running the published image directly, mount `./data`, `./out`, and `./logs` yourself — without these, your database and settings are lost on every `docker pull` / container recreation:

```bash
mkdir -p data out logs

docker run -d \
  --name fruitdeeplinks \
  -p 6655:6655 \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/out:/app/out" \
  -v "$(pwd)/logs:/app/logs" \
  -e TZ=America/New_York \
  --restart unless-stopped \
  ghcr.io/kineticman/fruitdeeplinks:latest

# Web UI: http://localhost:6655
```

`SERVER_URL` and `CHANNELS_DVR_IP` default to `localhost` / unset — set them via `-e` or on the [Settings page](#️-settings-page) if this server isn't reachable at `localhost` from your other devices (e.g. Fire TV, Channels DVR).

---

## ⚙️ Settings Page

Visit `/settings` to configure the server without editing environment variables:

| Section | Settings |
|---|---|
| **Server** | Server URL, Channels DVR IP, Channels source name |
| **Lanes** | Number of virtual lanes |
| **Favorite Teams & Broadcasters** | Ranking toggle, team cards, match preview, and backup/restore |
| **Persistent Channels** | Browse configured Xtream categories; add, edit, disable, or delete stable channels |
| **Pipeline** | Days ahead, padding minutes |
| **Auto Refresh** | Enable/disable, daily refresh time |
| **Scrapers** | Per-scraper on/off toggles |
| **Xtream IPTV** | Enable, server URL, selected category IDs, event timezone, inferred duration |
| **Advanced** | Lane/direct channel start numbers, headless mode, log level |

Changes take effect immediately and persist across container restarts.

Xtream username and password are intentionally environment-only: they are not
returned by the settings API or stored in SQLite. The server URL and non-secret
ingestion controls can be managed on the Settings page. Xtream entries are
imported only when their metadata or name contains a reliable start date and
time. Supported dynamic-name forms include
`NFL | 05 - 8/28 6pm Commanders at Ravens`, `8/29 7pm`,
`08/29 7:00 PM`, `8/30 3pm`, and 24-hour forms such as `8/30 15:00`. When the year is omitted,
FruitDeepLinks compares previous/current/next-year candidates in
`XTREAM_TIMEZONE` and accepts the closest only when it falls within the
configured `FRUIT_DAYS_AHEAD` window. A name containing only a time (for
example, `7:00 PM`) is skipped rather than being assigned an invented date. If
a reliable start has no end, the configured default duration is used and the
event metadata marks it inferred.

Xtream API calls use the normal Python HTTP client first. If the provider
rejects that request or returns an unusable response, the adapter retries with
`curl -4 -sS -L`. Neither transport's credential-bearing URL or error text is
written to application logs.

### Xtream motorsports events

Configured Xtream categories can also supply scheduled Formula 1 sessions in a
pipe-delimited form such as:

```text
NEXT | EXAMPLE GRAND PRIX: RACE | Sun 06 Sep 07:50 EDT (US) | UHD | F1 PPV 1
```

When the category or stream metadata identifies `F1`, `Formula 1`, or
`Formula One`, FruitDeepLinks removes provider/quality noise and produces a
title such as `F1 - Example Grand Prix - Race`. Race, Sprint, Qualifying,
Practice/FP1–FP3, Sprint Qualifying, and Sprint Shootout sessions are
recognized. `EDT` and `EST` are converted explicitly; unknown abbreviations
fall back to `XTREAM_TIMEZONE`. Yearless dates use the same bounded
previous/current/next-year inference as other Xtream events and must fall
within `FRUIT_DAYS_AHEAD`.

Placeholder slots such as `NO EVENT STREAMING`, `OFFLINE`, or `TBA` are
counted separately and do not create events or playables. Add the relevant
category to `XTREAM_CATEGORY_IDS`; FruitDeepLinks never scans the full provider
catalogue by default.

### Persistent Xtream channels

The **Persistent Channels** card on `/settings` is for stable team and network
feeds whose names do not contain an event date. Click **Browse Xtream
Channels**, choose one of the category IDs already configured under **Xtream
IPTV**, search by name, and add the result. Users never need to type a stream
ID. Each saved channel has a display name, unique channel number, optional
channel/guide IDs, logo override, favorite-team association, notes, and an
enabled switch.

Add the persistent source to Channels DVR with:

- **M3U URL:** `http://your-server-ip:6655/m3u/persistent`
- **XMLTV URL:** `http://your-server-ip:6655/xmltv/persistent`

Playlist entries point to
`/xtream/channel/<persistent-channel-id>/stream`; they never contain provider
credentials. At tune time FruitDeepLinks reads `XTREAM_USERNAME` and
`XTREAM_PASSWORD` from the environment, reconstructs the provider URL, and
returns a non-cacheable redirect. Persistent XMLTV includes channel records
only when no schedule is available; it does not invent programmes.

During each enabled Xtream refresh, persistent channels are checked against
their saved category. A missing stream ID is automatically replaced only when
exactly one normalized upstream name matches the saved original name. Zero
matches marks the channel unavailable; multiple matches mark it as needing
attention. The saved row is retained in both cases. Dynamic event ingestion
continues independently and still rejects date-free stream names.

### Favorite-team broadcaster preference

The optional **Prefer Favorite-Team Broadcaster** setting changes how
FruitDeepLinks ranks multiple playable feeds for the same event. It does not
filter feeds. If the preferred feed is missing or ineligible, the next playable
in the existing service/provider ranking remains available and wins instead.
The setting defaults to **off**, so existing users and users with no enabled
favorite-team entries keep the pre-feature ordering exactly.

Add one or more entries under **Settings → Favorite Teams & Broadcasters**.
The responsive card editor works without editing JSON, environment variables,
or SQLite. Each entry supports:

- a display/canonical team name;
- one-per-line event aliases;
- preferred broadcaster or feed terms;
- explicit avoid terms; and
- an enabled/disabled state.

For example, a Washington Capitals entry can use `Capitals, WSH` as aliases
and `WASHINGTON CAPITALS, MONUMENTAL` as preferred terms. These are only
configuration examples; no teams or networks are hard-coded into the ranker.
Settings are stored as non-secret JSON in the existing `user_preferences`
table under `setting:favorite_teams`; the global switch uses
`setting:prefer_favorite_team_broadcaster`. There is deliberately no
environment-variable fallback for either value. The exported backup shape is:

```json
{
  "schema_version": 1,
  "enabled": true,
  "teams": [
    {
      "team": "Example City Comets",
      "aliases": ["Example City Comets", "Comets", "ECC"],
      "preferred_terms": ["COMETS BROADCAST", "LOCAL SPORTS NETWORK"],
      "avoid_terms": ["RIVAL FEED"],
      "enabled": true
    }
  ]
}
```

Matching is case-insensitive and token/phrase based, not a raw substring
search. Event titles and available team/sport/provider metadata identify which
favorite teams are involved. Feed scoring uses service/provider/title,
`feed_name`/`feed_type`, network/category fields, and Xtream's non-secret
`stream_metadata_json` (including original stream and category names).

The team-affinity score is applied ahead of the existing deterministic ranking,
which remains the tie-breaker: `+100` favorite-team feed, `+70` configured
preferred term, `+40` an unambiguous favorite home/away feed, `+10` neutral or
national feed, `-30` opponent-specific feed, and `-50` explicit avoid term.
Categories are additive. When two configured favorite teams play each other,
either named team feed can receive the same bonus and the existing ordering
breaks the tie. Event Inspector shows the score and reason for the selected
feed and other scored playables. Xtream credentials are never read from or
written to these preferences.

Use **Edit / Test** on a team card to rename it, change its terms, or run a
match preview with an event title and feed/broadcaster name. The preview calls
the same server-side matcher and scorer used by actual playable selection and
shows the matched event term, total score, and scoring reasons. Event Inspector
uses those same explanations and shows the selected playable, existing service
priority, team score, and final rank.

**Export JSON** downloads only the global toggle and favorite-team data. To
restore or move it, choose **Import JSON**; imports are normalized and validated
before replacing the current favorite-team configuration. Names and values are
trimmed, repeated values are de-duplicated case-insensitively, and duplicate or
blank team names are rejected. **Reset Favorite Team Preferences** requires
confirmation and clears only this feature. If old stored JSON is malformed, the
page shows a recovery warning without overwriting it; import a valid backup or
explicitly reset to recover.

---

## 📡 Add to Channels DVR

### Direct Channels (recommended)

One channel per event — best for browsing specific games.

1. In Channels DVR: **Settings → Sources → Add Source → Custom Channels**
2. Create a source (e.g. `fruitdeeplinks-direct`):
   - **M3U URL:** `http://your-server-ip:6655/m3u/direct`
   - **XMLTV URL:** `http://your-server-ip:6655/xmltv/direct`
3. Set **Stream Format** to **`STRMLINK`**
4. Refresh guide data

### Virtual Lane Channels

Scheduled multi-provider virtual channels — one event per time slot per lane.

- **M3U URL:** `http://your-server-ip:6655/m3u/lanes`
- **XMLTV URL:** `http://your-server-ip:6655/xmltv/lanes`

### ADB Provider Lanes

Per-provider lanes for ADBTuner; supports device profiles:

| Profile | URL | Deeplink format |
|---|---|---|
| Fire TV / Android (default) | `/m3u/adb` | `aiv://`, `sportscenter://` etc. |
| Apple TV / web | `/m3u/adb?profile=apple` | `https://` |

Per-provider: `/out/adb_lanes_aiv.m3u`, `/out/adb_lanes_aiv_apple.m3u`, etc.

---

## 📋 Supported Streaming Services

### Tier 1: Fully Integrated

| Service | Notable Content |
|---------|-----------------|
| **ESPN+ / ESPN Linear** | MLS, college sports, select UFC, Monday Night Football |
| **Peacock** | Premier League, NBC Sports, college sports, Sunday Night Football |
| **Paramount+** | Champions League, college football, NFL on CBS |
| **Max** | Turner sports (TNT, TBS, truTV) |
| **Apple TV+** | MLS Season Pass, Apple MLB Friday, select NBA/NHL/F1 |
| **Prime Video** | Thursday Night Football, select sports |
| **Amazon Channels** | NBA League Pass, DAZN, FOX One, Max, ViX Premium, Peacock, and more |
| **Kayo Sports** | Cricket, AFL, NRL, Supercars (Australia) |
| **DAZN** | Combat sports, select leagues (regional) |
| **ViX** | Liga MX, Copa América, international soccer |
| **F1 TV** | Formula 1 |
| **NFL+** | NFL games |
| **NBA / Gametime** | NBA League Pass |
| **NHL.TV** | Hockey |
| **MLB.TV** | Baseball |
| **FOX Sports** | NFL, college sports |
| **CBS Sports / CBS** | NFL, college sports |
| **NBC Sports** | Various |
| **NCAA March Madness** | College basketball tournament |
| **Marquee Sports Network** | Chicago Cubs |

### Tier 2: Experimental

| Service | Notable Content |
|---------|-----------------|
| **Kayo Sports** | Australian sports — Cricket, AFL, NRL |
| **Fanatiz Soccer** | Latin American soccer leagues |
| **beIN Sports** | International soccer, rugby, motorsports |
| **Gotham Sports (MSG/YES)** | Knicks, Rangers, Islanders, Devils, Yankees, Nets |
| **NESN** | Red Sox, Bruins |
| **Victory+** | WHL, LOVB, niche sports |
| **Xtream IPTV** | Configured live sports/event categories with reliable schedule times |

> Experimental services: event data scrapes successfully; deeplink patterns still being refined. Community feedback welcome.

---

## 🛠️ Architecture

### Component Overview

1. **Scrapers** — Apple TV Sports API (Selenium + HTTP hybrid), ESPN Watch Graph API, Kayo, Fanatiz, beIN, NESN, Victory+, Gotham, configured Xtream categories, Amazon GTI mapping

2. **Pipeline** (`daily_refresh.py`) — orchestrates scrape → migrate → import → enrich → build lanes → export; runs on schedule or manually via dashboard

3. **Filter Engine** — user-configurable service preferences, sport/league selection, multi-service priority resolution, Amazon channel expansion

4. **Export Engine** — generates M3U + XMLTV for direct channels, virtual lanes, and ADB lanes; applies device profiles (Fire TV scheme vs. HTTPS)

5. **Web Dashboard** (Flask v2) — blueprint-based routes; pages: Events, Filters, ADB Config, Settings, API Helper, Admin/Logs

### Data Flow

```
Apple TV Sports API ──┐
Kayo / Fanatiz / beIN ├──> Scrapers ──> SQLite (fruit_events.db)
NESN / Victory+ / etc ┘                       │
Xtream selected categories ───────┬────> normalized events + direct playables
                                  └────> persistent channel reconciliation
ESPN Watch Graph API ─────────────────> Enrich playables
Amazon GTI mapping ───────────────────> amazon_channels table
                                               │
                                    Filter Engine (user prefs)
                                               │
                                       Export Scripts
                                               │
                         ┌─────────────────────┼─────────────────────┐
                 direct.m3u/.xml  lanes.m3u/.xml  adb_lanes.m3u/.xml  persistent M3U/XMLTV
                         │                     │                     │
                  Channels DVR          Channels DVR            ADBTuner
                         │
                  Your Streaming Apps (via Deeplinks)
```

---

## 🎯 Filtering Examples

### Budget Sports Fan

Enable only Prime Video + Peacock → ~200 events filtered to ~40.

### Soccer Enthusiast

Enable Paramount+ (Champions League), ViX (Liga MX), Peacock (Premier League). Disable Basketball, Baseball, Hockey → only soccer events from your services.

### Disable Scrapers You Don't Need

Turn off Kayo, Fanatiz, beIN from the Settings page → scrape time drops from ~13 min to ~5 min for US-only setups.

---

## 🐛 Troubleshooting

### Container won't start

```bash
docker logs fruitdeeplinks
# Common: port 6655 already in use, invalid env vars
```

### No events showing

```bash
# Trigger a manual refresh
curl -X POST http://localhost:6655/api/refresh

# Check event count
docker exec fruitdeeplinks sqlite3 /app/data/fruit_events.db "SELECT COUNT(*) FROM events"

# Check filters aren't blocking everything — visit /filters
```

### Deeplinks not working

- Verify the streaming app is installed and logged in on your device.
- Fire TV test: `adb shell am start -a android.intent.action.VIEW -d "scheme://..."`
- Try the HTTP deeplink variant (apple profile) if scheme URLs don't work on your device.
- Check `/events` in the dashboard — the event detail view shows the best available deeplink and its source.

### Dashboard not loading

```bash
docker exec fruitdeeplinks ps aux | grep fruitdeeplinks_v2
docker port fruitdeeplinks
```

---

## 📊 Performance

```
Database: ~1,500–3,000 events (varies by season and enabled scrapers)
After filtering: 100–400 events (depends on service selection)

Scrape time (all scrapers): ~13 minutes
Scrape time (Apple + ESPN only): ~5 minutes
Filter/export only (--skip-scrape): ~30 seconds
Memory usage: ~600 MB
Database size: ~20 MB
```

---

## 🗓️ Roadmap

### Completed in v2.0.0

- [x] Flask v2 app factory with blueprint routing
- [x] Settings page — full UI config, no .env required
- [x] Service catalog — single source of truth for all service metadata
- [x] Per-scraper on/off toggles (UI + env var hard override)
- [x] ADB lanes with Apple/Fire TV device profiles
- [x] Amazon Channel integration (GTI → channel code mapping)
- [x] ESPN Watch Graph enrichment for Fire TV deeplinks
- [x] Regional scrapers: Kayo, Fanatiz, beIN, NESN, Victory+, Gotham
- [x] XMLTV standards compliance (`<live/>`, `<new/>`, structured categories)

### Coming Soon

- [ ] Stabilize deeplinks for experimental services (Fanatiz, beIN, Gotham, Victory+, NESN)
- [ ] User-selectable Amazon Prime Video channel filtering
- [ ] Team-based filtering
- [ ] Time-of-day event filters

---

## 🤝 Contributing

Contributions and feedback welcome. The most useful contributions right now:

- Verified deeplink patterns for experimental services (Fanatiz, beIN, Gotham, Victory+)
- Additional regional scrapers
- Bug reports with logs (`/api/logs` or `docker logs fruitdeeplinks`)

### Development Setup

```bash
git clone https://github.com/kineticman/FruitDeepLinks.git
cd FruitDeepLinks

# Run in Docker (recommended)
docker compose up -d

# Or run locally
pip install -r requirements.txt
cd bin
python fruitdeeplinks_v2.py
```

---

## 📄 License

MIT License — see `LICENSE` for details.

---

## 🙏 Acknowledgments

- Apple TV Sports APIs (reverse-engineered)
- Channels DVR community
- Contributor bnhf for the scraper auto-disable logic (PR #19)

---

## ⚠️ Disclaimer

This project is for personal use only. Users must have legitimate subscriptions to all streaming services accessed. FruitDeepLinks does not provide, host, or distribute any copyrighted content — it only aggregates publicly available scheduling data and generates deeplinks to official streaming services.

Use of this software may violate the Terms of Service of various platforms. Use at your own risk.

---

## 🔗 Links

- **Repository:** https://github.com/kineticman/FruitDeepLinks
- **Channels DVR:** https://getchannels.com

---

**Made with ❤️ for sports fans tired of app-hopping**
