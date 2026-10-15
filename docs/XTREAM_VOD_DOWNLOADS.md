# Xtream Movies & TV downloads

Open **Movies & TV** at `/movies-tv`. **Refresh Catalog** deliberately reads the provider's movie and series lists and saves a local snapshot. Search, category browsing, episode browsing, and queue status use SQLite without provider traffic. **Load Episodes** reads only the selected series and saves its episode list. A failed catalog or episode refresh keeps the last successful snapshot.

Choose **Download movie** or **Download episode** to queue a direct media file on the NAS. The server accepts only IDs and formats from its saved catalog, constructs the provider URL with deployment secrets, and never sends the authenticated URL to the browser. One worker runs one job at a time. It acquires a normal Xtream pool lease for the entire transfer, so it waits for a free account and appears alongside other media leases. Cancel requests a stop; an idle upstream read may delay cleanup until its timeout. The worker then removes the partial file. Completed files are renamed atomically from `.part`; interrupted jobs return to the queue after a server restart and restart from the beginning.

The default destination is `/app/out/xtream-downloads`, which is `./out/xtream-downloads` on the Docker host with the standard Compose mount. To use a different NAS directory, set `XTREAM_DOWNLOAD_DIR` to an absolute **container** path and bind mount that path with write access for the app. Do not put downloaded media in the SQLite data directory. The app creates the directory when a job starts.

## newStormy TrueNAS Library

The media dataset you identified is `/mnt/newStormy/library` on the TrueNAS host (lowercase `library`). `/mnt/newStormy/Library` is a different directory. Mount the lowercase dataset at `/Library` **inside the Fruit container** and set `XTREAM_DOWNLOAD_DIR=/Library`. Downloaded movie and episode files currently go directly into the root of `/mnt/newStormy/library`.

For a deployment already managed with this repository's Docker Compose files, include `docker-compose.truenas-library.yml` alongside the active Compose files on the next service update.

For the existing **TrueNAS Custom App**, edit its Compose configuration in TrueNAS instead. Add these entries to its `fruitdeeplinks` service, preserving all existing environment values and mounts:

```yaml
environment:
  XTREAM_DOWNLOAD_DIR: /Library
volumes:
  - type: bind
    source: /mnt/newStormy/library
    target: /Library
```

If that service already uses list-style environment entries, add `- XTREAM_DOWNLOAD_DIR=/Library` to the list rather than creating a second `environment` key. Likewise, append the bind mount to its existing `volumes` list. The in-app updater preserves the Custom App's configured environment and mounts, but changing repository Compose files alone does not change that running app. After applying the TrueNAS configuration, confirm **Movies & TV** shows `/Library` as the destination and verify container write access before starting a download.

The downloader supports direct `mp4`, `mkv`, `avi`, `ts`, `mov`, and `m4v` files advertised in Xtream metadata. It rejects playlists and error pages rather than saving them as videos. Some providers omit file sizes, in which case progress shows downloaded bytes without a percentage. Downloads may still contend with use of the same subscription in another app; Fruit's pool cannot see or reserve external sessions.

API routes:

- `GET /api/xtream/vod/catalog` — saved titles, categories, and pagination (`kind`, `q`, `category_id`, `limit`, `offset`).
- `POST /api/xtream/vod/catalog/refresh` — explicit provider catalog refresh.
- `GET /api/xtream/vod/series/<id>/episodes` — saved episodes.
- `POST /api/xtream/vod/series/<id>/episodes/refresh` — explicit provider episode refresh.
- `GET /api/xtream/vod/downloads` — last 100 jobs and destination.
- `POST /api/xtream/vod/downloads` — queue `{"kind":"movie|episode","item_id":"..."}`.
- `DELETE /api/xtream/vod/downloads/<id>` — cancel an active or queued job.

This uses the existing app access boundary. Keep port 6655 restricted to trusted users and networks; the app has no login gate for these or other admin routes.
