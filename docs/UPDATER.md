# In-app branch updates

Settings → **App Updates** can check the installation's current Git branch,
show its pending commit summaries, and install a revision you explicitly approve.
Updates are never installed on a timer. The default remote is `origin`, so this
fork continues to update from your fork rather than the upstream project.

The web app communicates with a small Python helper running on the Docker host.
The helper has no network listener; the web container receives only a file
mailbox, not the Docker socket or the repository. The helper needs Python 3.10+,
Git access to the configured remote, and Docker Compose v2 with `up --wait`.
Only a source-build Compose installation using the standard Dockerfile is
supported. Published-image, Portainer-managed, and development bind-mount
installations must first move to a host-managed source-build Compose checkout.

## First-time setup

Run these commands on the **Docker host**, in the clean deployment checkout
containing this updater. Stop any other deployment automation for this service.
The user running the helper must be able to run Git and Docker without prompts.

```bash
mkdir -p .xsort-updater/control
docker compose -f docker-compose.yml -f docker-compose.updater.yml build \
  --build-arg FDL_BUILD_REVISION="$(git rev-parse HEAD)" fruitdeeplinks
docker compose -f docker-compose.yml -f docker-compose.updater.yml up -d --no-build fruitdeeplinks
python3 bin/xsort_updater.py
```

The last command stays running. Open `/settings#app-updates`, click **Check for
updates**, review the commits, then **Install update**. Installation confirms the
reviewed revision again, so a changed remote branch requires another review.

Use the same Compose project name and files as the existing installation. For
an explicit project name, pass `-p NAME` to both Compose commands and
`--project-name NAME` to the helper. Custom override files must also be passed to
the helper with repeated `--compose-file` arguments, including
`docker-compose.updater.yml`. Omit `docker-compose.dev.yml` and any code mounts.
Keep the updater override in subsequent manual Compose commands.

The helper locks its branch at startup. You can make the selection explicit:

```bash
python3 bin/xsort_updater.py --remote origin --branch codex/xtream-ingestion
```

For a persistent Linux installation, use a service manager. Example systemd unit
(replace the user, checkout path and Python path with your deployment values):

```ini
[Unit]
Description=Xsort app updater
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xsort
WorkingDirectory=/opt/Xsort
ExecStart=/usr/bin/python3 /opt/Xsort/bin/xsort_updater.py --branch codex/xtream-ingestion
Restart=on-failure
RestartSec=5
UMask=0022

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/xsort-updater.service`, then run
`sudo systemctl daemon-reload` and `sudo systemctl enable --now xsort-updater`.
The selected user needs access to the checkout and Docker. On other hosts,
keep the helper running under the host's normal process manager. Restart the
helper after an update to load changes to the helper itself.

## What an installation does

1. Fetch only the configured remote branch. Reject local edits, untracked files,
   branch switches, divergence and downgrades. Never stash, reset or clean work.
2. Confirm the app is idle and its data, outputs and logs use persistent mounts.
   Pause new API writes and scheduled refreshes during installation.
3. Retain the running image and snapshot the existing resolved Compose settings.
   These settings remain authoritative during this update, even if the fetched
   branch changes its Compose file. Deployment configuration changes require
   a separate host review.
4. Fast-forward the checkout to the reviewed revision and build a new image.
   The existing app keeps running during the build.
5. Take a consistent SQLite backup in `update-backups/` alongside the database.
6. Recreate only `fruitdeeplinks` and wait for its Docker health check. Confirm
   the running image's revision matches the approved commit before reporting
   success. The browser reconnects automatically without discarding form edits.

Restarting disconnects active streams. Install between recordings. Docker's
health check verifies app availability; it does not verify provider playback.
Normal reads/playback can continue during the build. Existing background jobs
outside the main refresh pipeline should finish before updating.

Resolved Compose snapshots can contain credentials; they are written only to
`.xsort-updater/private/` with restricted permissions. That directory is excluded
from Git and Docker build contexts and is never mounted into the web container.
The shared control directory contains only status and fixed action requests.
This remains a trusted-LAN administration UI; do not expose it publicly.

## Failures and recovery

- A fetch, validation, build or backup failure leaves the running app in place.
  The Git checkout may already have advanced after a build failure; fix the
  host problem, then check and install again. Checks compare the running image,
  not merely the checkout's HEAD.
- A failed restart or health check attempts to recreate the previous image
  using the saved configuration. The database backup is retained. Image recovery
  does **not** rewind the database or the checkout. If an incompatible migration
  ran, stop the app and restore the database from its backup before restarting.
- If automatic recovery fails, use the saved deployment on the Docker host:

  ```bash
  docker compose --project-directory "$PWD" \
    -f .xsort-updater/private/rollback.json \
    up -d --no-deps --no-build --pull never --wait fruitdeeplinks
  ```

  The saved JSON includes the original project name. Do not share it: it may
  contain credentials. Inspect container logs on the host if recovery fails.
- The helper reports interrupted work after a crash and never replays an install
  request on startup. Check the running container, recover if needed, then check
  for updates again. An offline helper disables the UI buttons.

Backups and tagged previous images are retained for manual cleanup after you
have confirmed the new installation works. No database restore, image pruning,
repository reset or unrelated service restart happens automatically.
