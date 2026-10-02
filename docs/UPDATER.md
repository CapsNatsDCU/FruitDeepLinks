# In-app branch updates

Settings → **App Updates** can check the installation's current Git branch,
show its pending commit summaries, and install a revision you explicitly approve.
Updates are never installed on a timer. The default remote is `origin`, so this
fork continues to update from your fork rather than the upstream project.
After setup, updates do not require editing YAML, changing a commit hash, or
running a rebuild command. The buttons perform the check, build and restart.

## TrueNAS Custom App: one-time setup

For an existing Docker-based TrueNAS Custom App (24.10 or newer), use
`bin/xsort_truenas_updater.py`. It keeps the app managed by TrueNAS. It uses the
NAS's preinstalled local API client and never places a NAS API key or Docker
socket in the web container. API compatibility is based on the native
[app configuration/update API](https://api.truenas.com/v25.04.1/api_methods_app.update.html)
and [startup task API](https://api.truenas.com/v25.04.1/api_methods_initshutdownscript.create.html).

Run the following **once in the TrueNAS shell**, after this code has been
published to your branch. Use an unused checkout directory on your persistent
Apps dataset. This example uses the existing app name `xsort`; adjust the name
if yours differs. Finish any recordings and refresh before running setup.

```bash
sudo git clone --branch codex/xtream-ingestion --single-branch \
  https://github.com/CapsNatsDCU/FruitDeepLinks.git /mnt/Apps/fruit-updater
sudo /usr/bin/python3 /mnt/Apps/fruit-updater/bin/xsort_truenas_updater.py \
  --app xsort --setup
```

Do not clone over an existing directory or use your data/secrets directory as
the checkout. If you already have a clean dedicated checkout containing this
script, run its setup command instead. The host process needs root access to
the local TrueNAS API and Docker. The checkout must remain on the NAS.

Setup reads the existing Custom App configuration, requires exactly one
`fruitdeeplinks` service and persistent data/out/log mounts, and rejects active
streams or refreshes. It builds the selected branch revision, backs up the
database and old image, and adds the updater mailbox automatically. It preserves
existing ports, credentials, secrets mounts, storage, networks and other app
settings. It replaces the remote `build.context` with a locally built versioned
image. TrueNAS's native `app.update` applies that image; the helper does not edit
TrueNAS-generated files or run a second Compose project alongside the app.

It also registers a named **POSTINIT** task in TrueNAS that starts the helper
under a transient systemd service with restart enabled. No TrueNAS OS packages
or API keys need to be installed. After each successful update, the helper
restarts itself so its own updated code is loaded too.

Open **Settings → App Updates**. From now on, use **Check for updates**, review
the changes, then **Install update**. The helper follows the configured branch,
builds the exact reviewed revision, and asks TrueNAS to restart this app. It
waits for the image's health check and verifies the running revision. You do
not replace `build.context` or paste commit hashes for subsequent updates.

The checkout must be clean. Catalog apps, multi-service Custom Apps, development
source mounts, and nonstandard Docker build options are not supported by this
mode. An edit to the TrueNAS app during a build aborts installation rather than
overwriting the newer settings. Only pushed branch revisions are available.

If a new image fails, the helper asks TrueNAS to restore the retained old image
with the saved app configuration. Database backups are retained separately;
image rollback never silently rewinds data. Private app configuration backups
may contain credentials and stay in `.xsort-updater/private/` with restricted
permissions. Do not share those files.

If automatic recovery fails, stop the helper and restore its saved image/config:

```bash
sudo systemctl stop xsort-updater-xsort.service
sudo /usr/bin/python3 /mnt/Apps/fruit-updater/bin/xsort_truenas_updater.py \
  --app xsort --recover
```

This does not restore the database. For diagnostics, use
`sudo journalctl -u xsort-updater-xsort.service`. The TrueNAS POSTINIT task is
named `Fruit updater: xsort`; disable it and stop the service to disconnect the
helper. Do not delete its checkout or backups while it is in use.

The automated tests exercise configuration preservation, repeat updates,
branch checks, image health failures and recovery with simulated TrueNAS and
Docker calls. They do not establish successful installation on a real NAS.

## Docker Compose: one-time setup

The web app communicates with a small Python helper running on the Docker host.
The helper has no network listener; the web container receives only a file
mailbox, not the Docker socket or the repository. The helper needs Python 3.10+,
Git access to the configured remote, and Docker Compose v2 with `up --wait`.
This mode requires a source-build Compose installation using the standard
Dockerfile. Published-image, Portainer-managed, and development bind-mount
installations must first move to a host-managed source-build Compose checkout.
TrueNAS Custom Apps use the separate setup above.

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
