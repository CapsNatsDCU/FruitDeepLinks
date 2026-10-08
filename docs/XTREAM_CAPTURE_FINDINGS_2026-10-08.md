# Strimix and Fruit playback comparison — 2026-10-08

## Confirmed result

Strimix successfully played **US: MONUMENTAL SPORTS NETWORK HD**, provider stream **324969**, on the user's Mac. A private, process-filtered packet capture recorded the opening HTTP requests. A temporary HMAC equality check verified that its username and password match **Fruit Account 2**. Both use `cf.gxtrm.xyz`.

Strimix's media request used:

- HTTP on port 80 over IPv6.
- `/live/<username>/<password>/324969.ts`.
- `User-Agent: KSPlayer` and `Range: bytes=0-`.
- No Cookie or Authorization header.
- A 302 redirect to a media host, followed by HTTP 200 with `Content-Type: video/mp2t`.

The app displayed moving video and reported 1080p, 60 FPS, H264, and AAC. Its metadata requests used `player_api.php` with query authentication and received HTTP 200.

## Controlled replay results

Strimix was stopped before subsequent probes. Each probe used the captured account and the same stream, sequentially. Mac replays used Python's standard HTTP library with an explicitly selected address family. Successful Mac probes verified the first two MPEG-TS sync bytes; these short probes did not perform ffprobe analysis.

| Location | Address family | Path | User agent | Range | Result |
| --- | --- | --- | --- | --- | --- |
| Mac | IPv4 | `/live/` | KSPlayer | `bytes=0-` | Cloudflare HTTP 403 |
| Mac | IPv6 | `/live/` | KSPlayer | `bytes=0-` | HTTP 200; MPEG-TS starts |
| Mac | IPv6 | Root | KSPlayer | `bytes=0-` | HTTP 200; MPEG-TS starts |
| Mac | IPv6 | `/live/` | KSPlayer | Absent | HTTP 200; MPEG-TS starts |
| Mac | IPv6 | `/live/` | python-requests/2.31.0 | Absent | Cloudflare HTTP 403 |
| Mac | IPv6 | Root | python-requests/2.31.0 | Absent | Cloudflare HTTP 403 |
| Mac | IPv6 | `/live/` | python-requests/2.31.0 | `bytes=0-` | Cloudflare HTTP 403 |
| Fruit container | IPv4 | `/live/` | KSPlayer | `bytes=0-` | HTTP 403; no media |
| Fruit container | IPv4 | `/live/` | KSPlayer | Absent | HTTP 403; no media |
| Fruit container | IPv4 | `/live/` | Default requests agent | `bytes=0-` | HTTP 403; no media |
| Fruit container | IPv4 | Root | KSPlayer | `bytes=0-` | HTTP 403; no media |
| Fruit container | Forced IPv6 | `/live/` | KSPlayer | `bytes=0-` | Connection error |

An additional Mac control used KSPlayer with explicit `Accept-Encoding: identity`, without Range, and received HTTP 200 MPEG-TS. The equivalent Python user-agent request received 403. Removing explicit identity encoding from the Python-agent request also received 403. This isolates user-agent behavior from Range and identity encoding in these tests.

## Fruit network check

A TCP connection from Fruit to the captured provider IPv6 address failed with **errno 101 / ENETUNREACH**. Its container reported **zero IPv6 interfaces other than loopback**. Account 2's local busy flag was false after the probes.

## Interpretation and limits

The captured credentials and provider stream ID are valid. Both root and `/live/` URL forms work with the successful Mac conditions. Range is optional in the tested requests.

Two compatibility differences remain relevant: the Python user agent was rejected in the controlled IPv6 comparison, and the captured request was rejected over the tested IPv4 connection even with KSPlayer headers. Fruit currently cannot use the successful IPv6 route. This is evidence of request and network compatibility failures; it does not establish the provider's exact Cloudflare rules or that every possible IPv4 route is blocked.

The subsequent authorized fix adds a configurable KSPlayer user agent across all three transports and an opt-in IPv6 bridge. On the NAS, IPv6 autoconfiguration was enabled, but `accept_ra=1` with forwarding enabled suppressed gateway discovery. A reversible `accept_ra=2` test obtained a global IPv6 address and default route. The setting was then submitted through TrueNAS's SYSCTL tunable API. Fruit's deployment and real playback verification are tracked separately below.

## Handling of evidence

Raw captures and analysis helpers are stored in a private temporary directory on the Mac with restrictive permissions. Raw packet data can contain account credentials and has not been included in this report. Published summaries and screenshots redact credentials. The diagnostic scripts copied into the Fruit container remain temporary.
