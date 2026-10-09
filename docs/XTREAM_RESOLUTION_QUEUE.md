# Queue channel resolution checks

Open **Persistent Channels** and choose **Queue test** beside a configured channel or a cached channel search result. Requests are saved in Fruit's database, so closing the browser or restarting Fruit does not discard them. You do not have to save a search result as a permanent channel to queue it.

The **Resolution check queue**, at the bottom of Persistent Channels, shows manual tests and the next 25 automatic checks in worker order. It shows Waiting, Testing, Completed, Failed, and Cancelled requests. Completed requests show the measured dimensions and frame rate; these measurements also appear beside the channel. Cancel a waiting request using **Cancel**. A short sample already running finishes before another check starts. Choose **Queue test** again to retry a failed or completed request. Duplicate clicks on waiting/running requests do not add more work. The queue holds up to 200 active requests and retains 50 recent finished requests.

Check order:

1. Manually queued requests, in the order you queued them.
2. Permanent channels awaiting their first automatic attempt, newest additions first. A previously cached resolution does not remove this priority.
3. Channels still without a resolution measurement, oldest attempt first.
4. Routine repeat checks only when no enabled, available channel is awaiting an initial check or resolution measurement.

After its first media-check attempt, a new channel leaves newest-first priority. If that attempt fails to measure resolution, it stays ahead of routine rechecks. While an unmeasured channel waits for its retry interval, routine rechecks wait too. Disabled or unavailable channels do not block rechecks. A valid measurement from normal playback also satisfies its first check and completes a matching pending manual request. This ordering is saved through the channel creation timestamp and durable attempt records, so it survives restarts.

Operator requests take priority over the normal background scan. The worker checks every **five seconds** and makes at most one attempt per tick. Queued checks use **10 seconds** of quiet time after activity, **10 seconds** between successful checks, and **10 seconds** before reusing an account for another queued request. Provider checks, media collection and local analysis add time to each check. Failures keep the **two-minute backoff**. Automatic checks use the same ten-second spacing. A routine channel becomes due for another automatic recheck ten minutes after its last attempt or valid playback measurement; this does not delay manual requests or checks of newly added channels.

Checks still defer during app updates, refreshes, active playback/recordings, and account activity. They require an enabled, healthy, free account marked **Reserved for Fruit**, two fresh provider readings confirming zero connections before media, and the existing bounded sample. Normal playback can interrupt an optional sample. Queueing and reading the queue do not contact the provider. Cached catalog presence is not playback proof. Missing/occupied provider activity leaves a manual request waiting for a later eligible attempt. Old ten-minute activity markers are shortened once to the new ten-second policy.

## Automatic measurement when a stream starts

Every Xtream playback or recording served by Fruit's proxy captures up to **4 MiB / eight seconds** of the media already being delivered. It does not open another provider stream or consume another account slot. Local ffprobe analysis runs separately from playback, and valid measurements update saved channel/catalog resolution, frame rate and codec. Short/invalid samples and analysis failures retain the previous valid measurement. HEAD availability checks and bodies never consumed by the client do not measure video. Streams opened directly in another app, bypassing Fruit, cannot be observed by Fruit.

Successful manual account playback tests also save the resolution of their existing sample, without a second media connection. The page refreshes saved quality with its queue polling. **Detect quality** and bulk detection controls are removed; use **Queue test** when you want a channel checked without starting playback. Local analysis is bounded to two simultaneous jobs and 16 captures/jobs per web worker. At that limit a measurement can be skipped while playback continues normally.

An interrupted running request is returned to the queue after the probe gate and surviving leases confirm that no previous sample is still active. Failure details use fixed messages, without provider credentials or authenticated URLs. Existing measurements and permanent-channel identity are preserved when a request is cancelled.

API: `GET /api/xtream/persistent-channels/quality/queue` reads requests, completed measurements, the next automatic candidates, active automatic checks and wait reason. `POST` with `{"category_id":"...","stream_id":"..."}` queues a saved/cached channel and returns HTTP 202. `DELETE /api/xtream/persistent-channels/quality/queue/<id>` cancels a waiting request. Arbitrary uncached stream IDs are rejected.
