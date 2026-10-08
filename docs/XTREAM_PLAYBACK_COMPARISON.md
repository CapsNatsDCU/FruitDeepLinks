# Manual playback comparison

Fruit's Settings **Test Playback** button tests the usual playback path, including
its existing conditional curl/HLS fallbacks. It does not independently compare
all three methods. Use `bin/xtream_compare_playback.py` for that comparison.

The script reads Fruit's configured secrets and database inside the running
container. It tests one account, one host and one channel sequentially through
Python TS, curl TS, and FFmpeg HLS. It does not contact the account/catalog APIs,
switch credentials, or automatically switch hosts. It uses Fruit's account gate
throughout, so a busy or disabled account is skipped and other Fruit operations
cannot overlap the test on that account. It does not update cached account health,
cooldowns, host preference or channel quality. The pool schema may be initialized
if needed, and ordinary activity pauses optional background probes.

Stop playback/downloads using that account in other apps first. Fruit cannot
reserve a slot against external applications. Pick a channel that works in the
other app so both tests refer to the same provider stream ID.

## Run without rebuilding Fruit

Download/copy `bin/xtream_compare_playback.py` to the TrueNAS host. From the NAS
shell, in the directory containing that file:

```sh
docker cp xtream_compare_playback.py ix-xsort-fruitdeeplinks-1:/tmp/xtream_compare_playback.py
docker exec -e PYTHONPATH=/app/bin ix-xsort-fruitdeeplinks-1 python3 /tmp/xtream_compare_playback.py --list
```

The list contains configured account IDs and up to 50 saved channel IDs,
provider stream IDs and names. It makes no provider calls. Replace
`YOUR_CHANNEL_ID` below with the numeric **channel_id** from that list that
matches the working channel; `account_2` is an example configured account ID:

```sh
docker exec -e PYTHONPATH=/app/bin ix-xsort-fruitdeeplinks-1 python3 /tmp/xtream_compare_playback.py --account account_2 --channel-id YOUR_CHANNEL_ID
```

For a channel not saved in Fruit, replace `--channel-id YOUR_CHANNEL_ID` with
`--stream-id YOUR_PROVIDER_STREAM_ID`. Use the provider stream ID, not a channel
number or Fruit's saved channel ID. Add `--host alternate` to repeat all three
tests on that account's configured fallback host. Each invocation pins the host
for all three methods. No hostname or credential needs to be pasted into commands.

The default uses Fruit's provider-specific root-path URLs. If the working app's
actual media URL uses `/live/USERNAME/PASSWORD/ID`, add `--path-style live` to
test that prefix explicitly. Root-path HLS failure does not establish that the
provider has no HLS endpoint. Compare the path the other app actually uses;
do not share the authenticated URL. This option changes only the diagnostic
run, not Fruit's normal playback configuration.

If your Docker container has another name, substitute the name shown by
`docker ps`. The copy goes into `/tmp` and is temporary; no rebuild/restart is
needed. The script imports Fruit's existing account-lock and media helpers.

## Results

The script prints JSON suitable for sharing. It reports a separate pass/fail,
fixed error category, time to first TS bytes, sample size and detected video
dimensions for each method. Python also reports HTTP status, redirect count and
time to response headers. Curl/FFmpeg HTTP status and redirect counts are `null`
because Fruit's existing media helpers do not expose those values. They are
unknown, not successful responses. Authenticated URLs, raw provider errors and
credentials are suppressed. Each sample targets two seconds of media and is
limited to 4 MiB. The media watchdog defaults to eight seconds per method;
local video analysis has an additional eight-second limit. Children are closed
before the next method, and watchdogs stop surviving media processes.

If eight seconds is insufficient, repeat with `--seconds 20` to give **every**
method the same larger media deadline (allowed range: 5–30 seconds). First-media
timings include transport buffering; they are not a DNS or connection breakdown.

| Result | What to investigate |
| --- | --- |
| Python TS fails, curl TS passes | HTTP client compatibility |
| Both TS methods fail, HLS passes | TS support, endpoint selection or Fruit's TS-first assumption |
| All fail | Account availability, exact host/stream ID, deployed network and the chosen deadline |
| All pass | Intermittent failures, provider contention or ordinary tune-path differences |

Short video detection does not prove sustained playback, correct programme
identity or provider capacity. The script exits 0 if any method passes, 1 for
all-failed/skipped comparisons, and 2 for setup errors. Ctrl+C cancels the worker
group and exits 130.
