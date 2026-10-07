# FruitDeepLinks directly into Channels DVR

Fruit can serve one Channels custom source with persistent Xtream channels and
the existing dynamic Fruit sports lanes. Both use one credential pool. Keep
Threadfin and its existing Channels source until the acceptance checks below
pass on your deployment.

```
Xtream accounts A / B / C → FruitDeepLinks → Channels DVR
```

## Endpoints and transport

| Purpose | URL on your Fruit host |
| --- | --- |
| Combined lineup | `/m3u/channels` |
| Combined guide | `/xmltv/channels` |
| Persistent-only lineup / guide | `/m3u/persistent` / `/xmltv/persistent` |
| Persistent tune | `/xtream/channel/<persistent-id>/stream` |
| Dynamic lane tune | `/lane/<lane-id>/stream.m3u8` |
| Account, capacity, lease and recent outcome status | `/api/xtream/pool` |
| Check all accounts | `POST /api/xtream/pool/check` |
| Check one account | `POST /api/xtream/pool/accounts/<account-id>/check` |
| Edit non-secret controls | `PATCH /api/xtream/pool/accounts/<account-id>` |
| Refresh persistent EPG / read status | `POST /api/xtream/epg/refresh` / `GET /api/xtream/epg/status` |

Choose **MPEG-TS** in Channels, including for the historical `.m3u8` lane URL.
Xtream-backed responses now proxy media rather than redirecting. The URL suffix
is retained for compatibility; the response content type is `video/mp2t`.
Existing non-Xtream direct sources retain their behavior and consume no Xtream
leases. App-only sources still require their existing capture/ADB integration;
the pool does not turn app deeplinks into video. Existing Chrome, CH4C, PrismCast,
ADB and Threadfin export routes remain available.

The M3U includes a stable `channel-id`, matching M3U `tvg-id` / XMLTV guide IDs,
`channel-number`, `tvg-chno`, names, groups, and available logos. Persistent
numbers never change. Existing lane-number collisions get a saved alternative
in the unified lineup only; legacy lane exports keep their original numbers.
An existing saved alternative remains stable when channels are disabled or
removed. A persistent guide ID in the reserved `lane.*` namespace is remapped
consistently in the combined outputs. Guide IDs shared by persistent channels
share one XMLTV definition.

Channels documents its custom-source fields, MPEG-TS/HLS support, a 750-channel
playlist limit, and merging behavior for numbers shared by sources in its
[Custom Channels guide](https://getchannels.com/docs/channels-dvr-server/how-to/custom-channels/).
Keep the selected persistent channels plus lane count within that supported
limit. Fruit does not silently truncate the lineup.

## Configuration and secrets

All accounts must access the **same logical provider catalogue**: a stream ID
must identify the same channel on every account. Separate server URLs are
allowed for equivalent provider endpoints. Different provider catalogues need
separate deployments; Fruit does not guess cross-account stream-ID mappings.
Do not configure the same credentials twice to inflate capacity. Account IDs
must be stable, unique, non-secret labels such as `account_a`; do not rename an
ID when merely changing its friendly label or password.

Preferred configuration is a read-only deployment secret file. Start from
[xtream-accounts.example.json](xtream-accounts.example.json), which contains fake
credentials only. The JSON accepts an `accounts` list (or a top-level list),
with these fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier, letters/digits/underscore/dash, up to 64 characters |
| `label` | Friendly account name |
| `enabled` | Boolean, defaults to true; deployment-disabled accounts cannot be re-enabled by UI |
| `server_url` | HTTP(S) provider base URL, no userinfo/query/fragment |
| `fallback_server_url` | Optional second base URL for the same account and catalogue; tried only when the first host fails |
| `username`, `password` | Deployment-only credentials |
| `capacity_override` | Optional positive integer; omit to discover the provider limit |

Environment settings:

| Variable | Use |
| --- | --- |
| `XTREAM_ENABLED=true` | Enable the provider; a saved Settings value takes precedence |
| `XTREAM_ACCOUNTS_FILE=/run/secrets/xtream-accounts.json` | Preferred secret file |
| `XTREAM_ACCOUNTS_JSON` | Alternative JSON supplied by the deployment secret/environment system |
| `XTREAM_CAPACITY_OVERRIDE` | Optional override for the legacy single-account fallback |
| `XTREAM_STREAM_IDLE_TIMEOUT=60` | Media inactivity timeout, bounded to 10–600 seconds |
| `XTREAM_BACKGROUND_QUALITY_ENABLED=false` | Disable the slow automatic resolution sampler (enabled by default) |
| `XTREAM_HTTP_PROXY=http://172.16.6.1:8888` | Optional HTTP proxy for Xtream account checks, catalogue, EPG, and media only; use an address reachable from the Fruit container |
| `FRUIT_LANES=50` | Initial virtual lane count, 1–750; the saved Settings value takes precedence |

Configure **one** of FILE or JSON. A configured but malformed/missing file fails
closed; it does not silently fall back to old credentials. An explicit empty
account list disables the pool. When neither multi-account setting is present,
`XTREAM_SERVER_URL`, `XTREAM_USERNAME`, and `XTREAM_PASSWORD` continue as the
stable `legacy` account. Existing database-backed non-secret provider settings
still take precedence over environment defaults.

Capacity is the administrator override, else discovered `max_connections`,
else a conservative one connection for an authenticated account whose provider
does not report a usable limit. Zero/unlimited/malformed provider limits do not
authorize unlimited concurrency. Test accounts and set an override if needed.
For example, discovered capacities 1 + 1 + 1 produce 3; overrides 2 + 1 + 4
produce 7. SQLite UI overrides take precedence over file overrides; clearing a
UI override returns to the file override/discovered limit. Provider checks never
overwrite either override.

For a separate Gluetun container, enable its HTTP proxy and allow port 8888
through the Gluetun firewall. Publish the proxy on a private Docker-facing host
address, then set `XTREAM_HTTP_PROXY` on Fruit to that address. The example IP
above is specific to one Docker network; check Fruit's current gateway before
using it, and update the value if that network is recreated. Fruit sends only
Xtream provider traffic through this setting; Channels, ESPN, and other local
integrations keep their existing routes. The proxy URL must have no credentials
or path. Restart Fruit after changing the environment value. A healthy account
check confirms provider metadata access; verify a real `GET` tune in Channels
separately to confirm video playback.

Settings → **Xtream Account Pool** shows health, discovered and effective limits,
active/available capacity, last successful check, leases with stream IDs/ages,
and recent termination/failure reasons. It edits only labels, enabled state and
capacity. There are no readback password or username fields. Change credentials
in deployment secrets. Restart after changing environment values; secret files
are read by new operations. Existing streams finish using their original account.
To use an account in another app, disable and save it in the pool first. Fruit
requires the account to be idle before that change succeeds, then sends no new
account checks, catalog/EPG requests, quality probes or streams on it. Test
Account reports when a test was skipped because the account is disabled or
occupied. Re-enable it only after the other app has stopped.

For `account_2` and `account_3`, the previously tested
`cf.gxtrm.xyz`/`cf.business-cdn-8k.com` pair is recognized automatically when
`server_url` is exactly one of those hosts. The file's `server_url` remains the
configured primary; either host can be primary. An explicit
`fallback_server_url` in the secret file takes precedence. Account checks,
catalogue requests, XMLTV, quality samples and media tunes close a failed
attempt before trying the second host with the **same account credentials and
the same account lock**. A successful alternate is preferred for five minutes,
then the configured primary is tried again. No host is added for other account
IDs or an unrelated configured host. Test Account reports when its successful
check used the alternate host or both hosts failed; a healthy check still does not prove playback,
which requires a real media `GET`.

The background resolution sampler considers only enabled, available **saved
persistent channels**. It wakes once a minute but attempts at most one channel
per tick, one provider activity preflight per account per ten minutes, and one
media sample per channel per ten minutes. It runs only for accounts explicitly
marked **Reserved for Fruit** in Settings; the default is off per account, and
the reservation resets if that account's credentials change. Clear the setting
before using the account elsewhere. It requires no Fruit stream or other
account operation to be active, a cached healthy account with a free slot, an
account lease, and two fresh `player_api.php` `active_cons=0` readings two
seconds apart. Missing, malformed, nonzero or unreachable provider activity
data skips media. The media sample remains limited to eight seconds and 4 MiB;
the existing quality-probe lock also prevents overlap with manual checks.
Measurements are saved to the same cache shown under Persistent Channels.
`active_cons` is a provider snapshot, so another external player could start
after the second reading; exclusive account allocation is the only way to
eliminate that external race.

Settings → **Lanes → Number of Lanes** controls how many dynamic lanes appear in
the legacy and unified Channels lineups. Use **Save & Rebuild Lanes** to save the
value and regenerate lane assignments/exports without scraping providers. A full
manual or scheduled refresh also applies it. Reducing the value removes excess
lanes on that rebuild; increasing it creates the new stable lane identities.

Credentials and full authenticated responses are not persisted in SQLite.
The database stores an opaque credential fingerprint to invalidate obsolete
health after rotation, never a plaintext credential. Account objects hide
secrets from their Python representation. Output metadata is redacted for raw
and encoded credentials; transport errors are reduced to fixed safe messages.
HTTP-library logging is redacted, FFmpeg stderr is discarded, and authenticated
HLS URLs go to FFmpeg stdin rather than command-line arguments. As with the
existing application, keep the administrative interface on a trusted LAN or
behind your authenticated reverse proxy; this change does not add login.

## Allocation and failure behavior

Each downstream playback/recording owns one upstream connection, including two
clients on the same channel. There is no fan-out cache in this release. Select
the least occupied eligible account relative to its capacity, with stable ID
tie-breaking. Persistent and dynamic Xtream tunes reserve from exactly the same
pool. Only successful account checks enable initial allocation; a rejected
account is skipped while healthy accounts continue.

SQLite `BEGIN IMMEDIATE` serializes reservations across threads and processes.
Each live reservation holds a kernel file lock for its complete socket/process
lifetime. The lease remains until the upstream closes, then releases on client
disconnect, EOF, socket/read errors, exceptions, or unsuccessful tune. WSGI
`close()` also covers responses whose iterator was never started. Process death
releases the kernel lock; the next tune/status read reclaims the stale row.
Elapsed time alone never expires a live lease. Disabling an account is rejected
while it is occupied; stop the recording or wait for a provider request to
finish, then retry. Reducing capacity prevents new allocations without
interrupting existing recordings.
An HLS remux child inherits the lease lock: a killed Python worker cannot release
capacity while its FFmpeg child is still closing the upstream connection. The
pool also retains the lease row if a curl or FFmpeg child remains alive when
the parent finishes, so active capacity still reflects that media process.
Each account also has a credential-fingerprint file lock shared by media,
account tests, catalog and EPG requests. A provider request holds it until its
response is finished; a media stream holds it for its full lifetime, including
inherited curl/FFmpeg children. Requests skip or fail over while the account is
occupied. A refresh rechecks the saved enabled state before each request, so a
config selected before an account was disabled cannot contact it. This protects
activity inside this Fruit deployment. Fruit cannot see another app's connection
without contacting the provider, and provider-reported connection counts can
race with a later tune. Manual disable reserves an account for external use.

Use one Fruit deployment with its database and `.xtream-locks` directory on the
same **local** data filesystem. Workers on that host share reservations; separate
hosts/replicas and network filesystems with unreliable locking are unsupported.
Do not delete the lock directory while Fruit is running. Restore a database
backup only while stopped. The standard Docker entrypoint uses threaded Flask.
If running another WSGI/reverse-proxy stack, enable streaming, enough concurrent
threads, and no total request timeout; disable response buffering.

The proxy first requests Xtream's `.ts` transport and sends bounded chunks with
backpressure; it does not collect a recording in memory. If a provider returns
HLS, or its advertised HLS-only stream rejects `.ts`, FFmpeg remuxes into MPEG-TS
with codec copy. **No video/audio transcoding occurs.** FFmpeg is the one added OS
dependency and is included in the Docker image. Bare-host installs need FFmpeg
on PATH for HLS. Unsupported media fails rather than sending provider HTML,
JSON, or credential-bearing playlists to Channels.

Connect timeout is 10 seconds. Read timeout is inactivity between chunks, **not
a maximum session duration**; an active recording may run for hours. A stalled
upstream is closed after the idle timeout, so detecting a client disconnect
during an upstream stall can take that long. A rejected/transiently failing
account gets a cooldown and tune retries another eligible account; a failed
authentication is rechecked after five minutes or immediately by Test Account.
Transient errors retry after 30 seconds and do not permanently disable a
previously healthy account. Automatic health checks are bounded per account.

| Situation | HTTP behavior |
| --- | --- |
| No eligible/free account, disabled provider | `503`, `Retry-After: 5` |
| Upstream connection/auth/media failure across attempted accounts | `502`, `Retry-After: 5` |
| Missing/disabled persistent channel or no active lane playable | `404` |
| Persistent channel marked unavailable / needing reconciliation | `503` |
| HEAD of a configured enabled pool | Local `200` probe; opens no upstream stream or lease |
| EOF/error after headers have been sent | Body closes; outcome recorded; Channels may retry |

Only connections opened by Fruit can be counted locally. Other applications
using the same credentials consume provider capacity outside this pool. Reserve
those accounts/slots for those applications or stop that usage during testing.
The derived pool capacity also feeds lane scheduling/simulation. The old
single-provider `provider_capacities` row is preserved but superseded for Xtream
after pool initialization; edit per-account limits in Settings. Other providers'
manual capacities are unchanged. Scheduler capacity is a planning limit; actual
live/persistent demand is always checked again at tune time.

## Persistent guide

Refresh runs fetch provider `xmltv.php` once, parsing it incrementally and
retaining only explicitly mapped persistent guide IDs. The parser has a 128 MiB
decoded-input bound and retains up to 31 days/10,000 programmes per selected
guide. If XMLTV or a mapped channel is unavailable, the existing Xtream client
requests that stream's full EPG table, then short EPG. Base64 API text is decoded.
Programme start/stop must parse, stop must follow start, and a title must exist.
Provider-local naive API times use `XTREAM_TIMEZONE`; XMLTV offsets and epoch
timestamps are converted to UTC. Set the provider timezone correctly.

The provider `epg_channel_id` is stored separately from an administrator's
export `guide_id`. A custom output ID therefore does not lose its source mapping.
No display-name matching is used. Missing guide IDs can still use the explicit
per-stream API; an explicit conflicting channel ID is rejected. Provider title,
subtitle, description, category, icon, episode and new/repeat data are retained
when present, including rich XMLTV metadata. No programme information is invented.

Exports read cached data and do not contact providers. Refreshes preserve
unexpired cached programmes on transport/malformed-data failures, report status,
and exclude cache for changed stream/guide identities. A confirmed empty response
clears that channel's guide. Use **Refresh Persistent Guide** after adding or
editing a channel. Regular daily refresh also refreshes persistent EPG, including
deployments with no dynamic category selection. Missing provider data remains a
visible empty guide. Dynamic programme generation and provenance gates are reused.

## Exact Docker / TrueNAS update procedure

Substitute your existing application dataset path and LAN address. Run from the
existing checkout/directory containing `docker-compose.yml`; do not recreate the
data volume. No deployment is performed by the implementation task.

1. Note the current image tag and record your TrueNAS app configuration. When no
   recordings are running, stop only Fruit. Create a TrueNAS snapshot of its data
   dataset, or copy the stopped data directory:

   ```sh
   cd /mnt/POOL/APPS/FruitDeepLinks
   docker compose stop fruitdeeplinks
   cp -a data "data.backup.$(date +%Y%m%d-%H%M%S)"
   ```

2. Put this updated checkout at that location (or build/publish your own image
   containing these changes). The existing release compose file points at an
   upstream `latest` image; do not assume it includes this work. For a source
   checkout use the base compose file, which builds locally:

   ```sh
   mkdir -p secrets
   chmod 700 secrets
   cp docs/xtream-accounts.example.json secrets/xtream-accounts.json
   chmod 600 secrets/xtream-accounts.json
   ```

   Edit that private file in your editor with the real provider/account values.
   Keep it out of Git and support bundles. Leave overrides absent to discover
   account limits. The standard `secrets/xtream-accounts.json` mount is detected
   automatically, so `.env` only needs:

   ```dotenv
   XTREAM_ENABLED=true
   SERVER_URL=http://YOUR_TRUENAS_LAN_IP:6655
   XTREAM_STREAM_IDLE_TIMEOUT=60
   XTREAM_HTTP_PROXY=http://YOUR_FRUIT_DOCKER_GATEWAY:8888
   ```

   If the account file uses another mounted path, set `XTREAM_ACCOUNTS_FILE`
   to that path. Do not set `XTREAM_ACCOUNTS_JSON` at the same time.

   For a TrueNAS Custom App rather than CLI Compose, use the equivalent custom
   image, map existing data/out/log host paths to `/app/data`, `/app/out`,
   `/app/logs`, add a **read-only** host mount for the secrets directory at
   `/run/secrets`, and expose container port 6655 on your chosen LAN port. Give
   the container identity read access to the secret file and write access to the
   existing data directory. Do not include credentials in screenshots or logs.

3. Build, migrate, and start. Migration is additive and repeatable:

   ```sh
   docker compose build fruitdeeplinks
   docker compose run --rm --no-deps fruitdeeplinks python3 /app/bin/migrate_add_xtream_pool.py --db /app/data/fruit_events.db
   docker compose up -d fruitdeeplinks
   ```

   New tables: `xtream_account_state`, `xtream_leases`, `xtream_stream_history`,
   `xtream_epg_programmes`, `xtream_epg_status`, `channels_lane_numbers`. Persistent
   channels gain `epg_channel_id`, initially backfilled from their existing guide
   ID and refreshed from provider metadata. Existing events, lanes, settings,
   channel IDs/numbers and provider capacities survive. Standard daily refresh
   also invokes the migration; new endpoints initialize their required schema.

4. Open `http://YOUR_TRUENAS_LAN_IP:6655/settings`. Confirm the saved Server URL
   is reachable from Channels (a saved value overrides `.env`). Enable Xtream
   if the saved setting was disabled. Test all accounts: three healthy accounts
   reporting one slot each must show capacity 3. Check any override and provider
   timezone. Confirm persistent channels and category selections survived.

5. Run Refresh Persistent Guide, then the usual refresh for dynamic events.
   Inspect `/m3u/channels`, `/xmltv/channels`, `/api/xtream/pool` and guide status.
   No URLs in the playlist should point directly to the Xtream media provider.
   Downloaded XML must parse and each playlist `tvg-id` must have a matching
   channel definition. Programme availability depends on actual provider EPG.

Rollback: stop Fruit, restore the recorded old image/configuration, and if needed
restore the stopped database backup/snapshot. Do not restore/delete lease files
under an active server. Existing legacy endpoints remain available throughout
the upgrade; restoring the old version restores its old redirect behavior.

## Parallel Channels DVR acceptance test and Threadfin removal

1. Leave the existing Threadfin source and its recording rules in place. In
   Channels DVR web admin → Settings → Sources → Add Source → Custom Channels,
   create a **new** source named `Fruit Direct Test`. Select MPEG-TS, use
   `http://YOUR_TRUENAS_LAN_IP:6655/m3u/channels`, and set its XMLTV guide URL to
   `http://YOUR_TRUENAS_LAN_IP:6655/xmltv/channels`. If a tuner/stream limit field is
   offered by your Channels version, set it to the pool's effective capacity.
   Fruit independently enforces capacity even without that client setting.

2. Inspect names, logos, guide data, persistent numbers and lane numbers. Shared
   numbers across sources may merge in Channels; use source selection/priority
   for the test so playback actually comes from `Fruit Direct Test`. Keep the
   old source available and avoid changing all recording rules yet. Do not
   accidentally judge an old Threadfin stream as a successful Fruit test.

3. Tune one persistent channel. Confirm video/audio and a pool lease showing
   that persistent ID/account. Stop playback: active should return to zero.
   Change channels rapidly several times and check no lease count accumulates.

4. With three one-slot accounts, start two different persistent channels and a
   currently scheduled Xtream lane, using playback/recordings that actually
   request three streams from Fruit. Confirm three distinct accounts and active
   3/available 0. A fourth independent request must receive 503, never a fourth
   reservation. Channels itself may share a tuning session; if it does, use a
   fourth distinct channel or separate client to test exhaustion.

5. Stop one stream/recording. Its lease must release, and the fourth request must
   then succeed. Stop all clients; active must become zero. Test a bad/disabled
   account while the others continue. Restore it and Test Account to recover.

6. Schedule overlapping recordings and keep a real recording playing for several
   hours. Verify start/end, duration, continuity, seeking after recording, audio,
   quality, and the final account release. Exercise an upstream outage, a stopped
   client, a Channels retry, and a Fruit restart when no important recording is
   running. A restart interrupts active media; new requests must reclaim dead
   leases rather than appearing permanently full.

7. Compare the real provider schedule with the persistent guide, including local
   timezone/DST, guide-ID alignment and a missing-EPG channel. Confirm dynamic
   sports/event scheduling and source eligibility still match your expectations.

Remove Threadfin only after the direct source passes all checks, your normal
overlapping recordings and an extended recording complete reliably, guide
refreshes work, no secrets appear downstream, and active leases consistently
return to zero. Move recording/source priority deliberately, observe normal
usage, then disable the old Channels source. Keep the Threadfin configuration
and Fruit data snapshot for rollback before stopping/removing its container.

## Verification and remaining limitations

Automated tests use fake accounts/providers. They cover configuration, health,
overrides, capacity races, worker death, rapid retunes, shared persistent/lane
allocation, streaming/error/disconnect cleanup, credential redaction, EPG identity
and metadata/time handling, non-destructive schema, stable numbering and unified
guide alignment. Existing redirect assertions now verify the intended proxy
contract and internal URL encoding; they are not removed. The pre-existing
duplicate progress `status` argument was fixed because it triggered legacy
scheduler fallback. Two stale baseline tests were corrected to use a future
fixture and the established explicit Hidden filter.

Run the suite in an environment with `requirements.txt` plus `pytest` installed:

```sh
python3 -m pytest -q
RUN_XTREAM_SOCKET_TESTS=1 python3 -m pytest -q
git diff --check
```

Final local verification on 2026-09-29 completed with the socket/FFmpeg flag:
**351 tests passed, plus 62 unittest subtests**. The 13 warnings are existing
`datetime.utcnow()` deprecation warnings. Python bytecode compilation, Compose
YAML parsing, and `git diff --check` also passed.

The second command also exercises real loopback HTTP sockets and the installed
FFmpeg with generated test media. It requires local socket permission and FFmpeg.
No real provider or Channels DVR credentials are used. Passing these tests is
not proof of Channels/TrueNAS deployment acceptance or hours-long real recording
reliability; perform the owner tests above. Same-stream fan-out, different
provider catalogues, distributed-host leasing, transcoding, and replacing
non-Xtream app capture are outside this release.
