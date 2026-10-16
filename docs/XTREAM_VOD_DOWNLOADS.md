# Xtream Movies & TV downloads

Open **Movies & TV** at `/movies-tv`. **Refresh Catalog** deliberately reads the provider's movie and series lists and saves a local snapshot. Search, category browsing, episode browsing, and queue status use SQLite without provider traffic. **Load Episodes** reads only the selected series and saves its episode list. A failed catalog or episode refresh keeps the last successful snapshot.

Movie search results and saved episode rows show file size and resolution when present in provider metadata. They also infer likely audio language from saved titles without another provider request. `EN - Blade Runner 2049` shows **Likely audio: English**. `DE - Queen's Blade (2009) (JP) (Ger Sub)` shows **Audio language unavailable · Subtitles: German**: `JP` is a country code, while the [ISO 639-1 code for Japanese is `ja`](https://www.loc.gov/standards/iso639-2/php/code_list-utf8.php). The `DE` listing prefix does not establish German audio when the title explicitly identifies German subtitles. `NF -` is not a language code. An episode without its own language hint can use the parent show's title. Title-based inferences are not verified audio tracks. **Check details** makes one explicit `get_vod_info` request for a movie and can add separately labeled **Audio tags** from provider track metadata; **Load Episodes** does the same for a series. Browsing saved results stays provider-free, including for titles cached before this feature. Known provider details remain cached across catalog refreshes when the item ID, title, and format match. Missing size, resolution, or language is labeled unavailable rather than inferred from duration or bitrate.

Choose **Download movie** or **Download episode** to add a title to **NAS downloads**. This creates a saved, staged job and does not contact the provider or begin a transfer. In NAS downloads, choose **Movies** or **TV Shows** and press **Start**. Movies default to Movies and episodes default to TV Shows; either choice can be changed before starting. **Remove** discards a staged job. The selected folder is saved with the started job and shown in the download list. The server accepts only IDs, formats, and folder choices from its saved catalog and allowed destinations, constructs the provider URL with deployment secrets, and never sends the authenticated URL to the browser. One worker runs one started job at a time. It acquires a normal Xtream pool lease for the entire transfer, so it waits for a free account and appears alongside other media leases. Cancel requests a stop; an idle upstream read may delay cleanup until its timeout. The worker then removes the partial file. Completed files are renamed atomically from `.part`; interrupted jobs return to the queue after a server restart and restart from the beginning.

The default download root is `/app/out/xtream-downloads`, which is `./out/xtream-downloads` on the Docker host with the standard Compose mount. New jobs go into its `Movies` or `TV Shows` subfolder. To use a different NAS directory, set `XTREAM_DOWNLOAD_DIR` to an absolute **container** path and bind mount that path with write access for the app. If your actual folder names differ, set `XTREAM_MOVIES_FOLDER` and `XTREAM_TV_FOLDER` to their individual folder names (no slashes). Do not put downloaded media in the SQLite data directory. The app creates the selected folder when a job starts. Jobs queued before folder selection was added keep their original root destination.

Completed media files retain the permissions and inherited ACL granted when the destination dataset creates the temporary file. The downloader does not force owner-only `0600` permissions. Give the Plex app and the intended SMB users read access to files and read/traverse access to their parent folders in the TrueNAS dataset ACL. Files downloaded by earlier app versions may still have owner-only permissions and need their access corrected separately; updating the app does not change existing files.

New episode jobs use Plex's season-based layout under the selected destination: `TV Shows/Show Name (Year)/Season 01/Show Name (Year) - s01e02 - Episode Title [xtream-ID].mkv`. The year is included only when the saved series title contains one. Before downloading, the app checks for the exact show folder, then for one unambiguous existing folder with the same title and a year or Plex ID suffix. It reuses that folder; otherwise it creates the show and season folders. If more than one existing folder could match, the job fails rather than choosing the wrong show. Season `00` is used for specials. Earlier queued jobs retain their original flat destination and filename. [Plex's TV file naming guide](https://support.plex.tv/articles/naming-and-organizing-your-tv-show-files/) explains the structure.

New movie jobs use Plex's standalone movie filename form under the selected destination: `Movies/Blade Runner (1982).mp4`. Known provider language or `NF -` prefixes and labels after a parenthesized release year are removed from the filename. If the saved title has no release year, the app does not invent one. A movie job fails before downloading if that filename already exists, and it never replaces an existing file that appears during transfer. Completed files are not renamed retroactively. [Plex's movie file naming guide](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/) documents the format.

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
- `POST /api/xtream/vod/movie/<id>/details/refresh` — explicit provider detail read for one saved movie.
- `GET /api/xtream/vod/series/<id>/episodes` — saved episodes.
- `POST /api/xtream/vod/series/<id>/episodes/refresh` — explicit provider episode refresh.
- `GET /api/xtream/vod/downloads` — last 100 jobs, download root, and Movies/TV Shows destinations.
- `POST /api/xtream/vod/downloads/stage` — save `{"kind":"movie|episode","item_id":"..."}` as ready to start, without provider traffic.
- `POST /api/xtream/vod/downloads/<id>/start` — start a staged job with `{"folder":"movies|tv"}`.
- `POST /api/xtream/vod/downloads` — existing direct API for immediately queuing `{"kind":"movie|episode","item_id":"...","folder":"movies|tv"}`. Omitting `folder` defaults by content type.
- `DELETE /api/xtream/vod/downloads/<id>` — remove a staged job or cancel an active or queued job.

This uses the existing app access boundary. Keep port 6655 restricted to trusted users and networks; the app has no login gate for these or other admin routes.
