# Xtream Movies & TV downloads

Open **Movies & TV** at `/movies-tv`. **Refresh Catalog** deliberately reads the provider's movie and series lists and saves a local snapshot. Search, category browsing, episode browsing, and queue status use SQLite without provider traffic. **Load Episodes** reads only the selected series and saves its episode list. A failed catalog or episode refresh keeps the last successful snapshot.

Movie search results and saved episode rows show file size and resolution when those values are present in the provider response. The standard Xtream list may omit both fields, so the UI says **Size unavailable** or **Resolution unavailable** rather than estimating from duration, bitrate, or title text. Refresh Catalog updates movie details; Load Episodes updates the selected show's episode details. Existing saved rows without these fields remain browsable and show the unavailable labels until refreshed.

Choose **Download movie** or **Download episode** to add a title to **NAS downloads**. This creates a saved, staged job and does not contact the provider or begin a transfer. In NAS downloads, choose **Movies** or **TV Shows** and press **Start**. Movies default to Movies and episodes default to TV Shows; either choice can be changed before starting. **Remove** discards a staged job. The selected folder is saved with the started job and shown in the download list. The server accepts only IDs, formats, and folder choices from its saved catalog and allowed destinations, constructs the provider URL with deployment secrets, and never sends the authenticated URL to the browser. One worker runs one started job at a time. It acquires a normal Xtream pool lease for the entire transfer, so it waits for a free account and appears alongside other media leases. Cancel requests a stop; an idle upstream read may delay cleanup until its timeout. The worker then removes the partial file. Completed files are renamed atomically from `.part`; interrupted jobs return to the queue after a server restart and restart from the beginning.

The default download root is `/app/out/xtream-downloads`, which is `./out/xtream-downloads` on the Docker host with the standard Compose mount. New jobs go into its `Movies` or `TV Shows` subfolder. To use a different NAS directory, set `XTREAM_DOWNLOAD_DIR` to an absolute **container** path and bind mount that path with write access for the app. If your actual folder names differ, set `XTREAM_MOVIES_FOLDER` and `XTREAM_TV_FOLDER` to their individual folder names (no slashes). Do not put downloaded media in the SQLite data directory. The app creates the selected folder when a job starts. Jobs queued before folder selection was added keep their original root destination.

New episode jobs use Plex's season-based layout under the selected destination: `TV Shows/Show Name (Year)/Season 01/Show Name (Year) - s01e02 - Episode Title [xtream-ID].mkv`. The year is included only when the saved series title contains one. Before downloading, the app checks for the exact show folder, then for one unambiguous existing folder with the same title and a year or Plex ID suffix. It reuses that folder; otherwise it creates the show and season folders. If more than one existing folder could match, the job fails rather than choosing the wrong show. Season `00` is used for specials. Earlier queued jobs retain their original flat destination and filename. [Plex's TV file naming guide](https://support.plex.tv/articles/naming-and-organizing-your-tv-show-files/) explains the structure.

## newStormy TrueNAS Library

The media dataset you identified is `/mnt/newStormy/library` on the TrueNAS host (lowercase `library`). `/mnt/newStormy/Library` is a different directory. Mount the lowercase dataset at `/Library` **inside the Fruit container** and set `XTREAM_DOWNLOAD_DIR=/Library`. With the default folder names, movies save under `/mnt/newStormy/library/Movies` and episodes started with **TV Shows** save under `/mnt/newStormy/library/TV Shows/<show>/Season XX` on the host.

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
- `GET /api/xtream/vod/downloads` — last 100 jobs, download root, and Movies/TV Shows destinations.
- `POST /api/xtream/vod/downloads/stage` — save `{"kind":"movie|episode","item_id":"..."}` as ready to start, without provider traffic.
- `POST /api/xtream/vod/downloads/<id>/start` — start a staged job with `{"folder":"movies|tv"}`.
- `POST /api/xtream/vod/downloads` — existing direct API for immediately queuing `{"kind":"movie|episode","item_id":"...","folder":"movies|tv"}`. Omitting `folder` defaults by content type.
- `DELETE /api/xtream/vod/downloads/<id>` — remove a staged job or cancel an active or queued job.

This uses the existing app access boundary. Keep port 6655 restricted to trusted users and networks; the app has no login gate for these or other admin routes.
