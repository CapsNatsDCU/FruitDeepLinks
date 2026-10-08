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

## Fruit network check before the fix

A TCP connection from Fruit to the captured provider IPv6 address failed with **errno 101 / ENETUNREACH**. Its container reported **zero IPv6 interfaces other than loopback**. Account 2's local busy flag was false after the probes.

## Interpretation and limits

The captured credentials and provider stream ID are valid. Both root and `/live/` URL forms work with the successful Mac conditions. Range is optional in the tested requests.

Two compatibility differences were relevant: the Python user agent was rejected in the controlled IPv6 comparison, and the captured request was rejected over the tested IPv4 connection even with KSPlayer headers. Before the fix, Fruit could not use the successful IPv6 route. This is evidence of request and network compatibility failures; it does not establish the provider's exact Cloudflare rules or that every possible IPv4 route is blocked.

The subsequent authorized fix adds a configurable KSPlayer user agent across all three transports and an opt-in IPv6 bridge. On the NAS, IPv6 autoconfiguration was enabled, but `accept_ra=1` with forwarding enabled suppressed gateway discovery. A reversible `accept_ra=2` test obtained a global IPv6 address and default route. The setting was then submitted through TrueNAS's SYSCTL tunable API. Fruit's deployment and real playback verification are tracked separately below.

## Deployed fixes and verification

The existing TrueNAS app was updated in place through Fruit's updater. Its persistent data, settings, and secret mounts were preserved. The IPv6 bridge configuration was applied with a saved original deployment and rollback check; the host updater was restarted afterward. Native TrueNAS SYSCTL tunable ID 1 records `net.ipv6.conf.enp2s0.accept_ra=2`, enabled. Forwarding remains enabled. Fruit uses both its original default network and the IPv6 bridge, with a scoped address-selection configuration and the KSPlayer user agent.

The first deployed revision, `52a6893`, made curl playback work but exposed another independent bug: Python stopped a chunked media response after the first 376 bytes. A temporary initial `iter_content()` generator was being closed before the rest of the response could be consumed. Revision **`f128490a48f1`** retains that generator through streaming cleanup and is confirmed running in Fruit's Settings page.

From that deployed container, Account 2 and Monumental Sports produced:

| Transport | Result | Media bytes | Decoded video | First media |
| --- | --- | --- | --- | --- |
| Python TS | Passed; HTTP 200 after one redirect | 4,194,304 | H264, 1920×1080, 59.94 fps | 0.472 s |
| curl TS | Passed | 4,194,304 | H264, 1920×1080, 59.94 fps | 0.599 s |
| FFmpeg HLS | Failed; transport error | 0 | None | None |

The live HLS failure is unresolved; it does not establish whether the provider supports the tested `.m3u8` endpoint. TS playback is verified. Local HTTP fixture tests verify HLS headers through redirects and relative segments, which is separate from provider availability.

Fruit's **Test All Playback** was then run manually. It selected the saved NBC 4 Washington channel and decoded 1920×1080 video on **Accounts 2 and 3**. **Account 1 timed out**. The test completed with zero active leases. Its cached account API health remains degraded; successful media does not establish fresh `player_api.php` account health.

## Same-channel concurrency and remaining blocker

Three clients requested the same Fruit URL, `/xtream/channel/3/stream`, concurrently. Two received HTTP 200 and 12,032 bytes with MPEG-TS sync markers. While those clients remained open, the pool showed two distinct leases: Account 2 and Account 3, both stream `324969`, source `persistent:3`. The third client received HTTP 502 after Account 1 failed. Closing the clients returned the pool to zero active leases. No duplicate saved channel was created.

Account 1's configured host, `cf.business-cdn-8k.com`, had no IPv6 DNS result in the deployed container. Its IPv4 TCP port 80 connection timed out after 3 seconds, before any HTTP path or credentials could be sent. The same diagnostic reached `cf.gxtrm.xyz` over IPv6; Python and curl media then passed there using Account 2. All three Account 1 transport comparisons failed with timeouts. A working provider address for Account 1 is needed before three simultaneous playable streams can be verified. Credentials were not sent to an unconfigured alternative host.

## Local regression checks

The final chunked-response fix passed 64 focused transport, proxy, quality, playback, and comparison tests. Its new real HTTP fixture uses `Transfer-Encoding: chunked` and verifies the entire media body after the initial prefix; the previous implementation fails that fixture. Six opt-in socket integration tests also passed, including three fixture clients sharing one channel across three accounts and lease cleanup. Those fixture results establish pool behavior under controlled conditions; the live test above establishes only two working accounts.

## Handling of evidence

Raw captures and analysis helpers are stored in a private temporary directory on the Mac with restrictive permissions. Raw packet data can contain account credentials and has not been included in this report. Published summaries and screenshots redact credentials. The reusable comparison script is checked into the repository; private capture helpers and the host verification scripts remain temporary.
