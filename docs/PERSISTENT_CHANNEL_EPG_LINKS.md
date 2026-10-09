# Choosing an alternative EPG link

In **Persistent Channels**, open **Add Channel** or **Edit**. The setup form includes **Alternative EPG link**, a guide-name/ID search field, **Match options**, and **Find EPG links**.

1. Search using the station, network name, or guide ID. The channel's original provider name is filled in initially.
2. Review similar channel names, their guide IDs, and the programme counts from the saved XMLTV feed. Links known only from the channel cache explicitly say their programmes have not been checked.
3. Select **Choose this guide** for a channel carrying the same schedule.
4. Select **Save Channel** to apply it. Clearing the alternative link restores the channel's own provider guide.

Name matching offers suggestions; Fruit does not choose a guide automatically. Regional, alternate, and time-shifted feeds can have different schedules despite similar names.

The search shows **50 guides per page**, with **Previous 50** / **Next 50** controls and a total count. Choose **Broader name matches** to include weaker matches and other station numbers. Choose **All saved guides** and clear the search to browse every saved provider, external XMLTV/zap2xml, and cached channel guide, alphabetically. In that mode, entering text filters by part of a name or guide ID. Changing the query or match option restarts at the first page. A draft selection remains selected across pages until you save or cancel the channel.

`GET /api/xtream/epg/links/search` accepts `q`, `mode=similar|broad|all`, `limit` (1–100, default 50), and `offset` (nonnegative, default 0). Only `all` permits an empty query. Responses include `candidates`, `total`, `offset`, `limit`, and `has_more`. All pages are read from saved snapshots.

Browsing uses the saved channel cache and XMLTV link index without provider requests. Refresh Channel Cache or Refresh EPG Link Index when newer links are needed. Saving an enabled channel still requests the normal persistent-guide refresh.

The alternative is stored as `epg_source_id`. It overrides the provider XMLTV source while preserving the original channel identity, stream, and exported guide ID. Changing it clears cached programmes from the previous source. If the alternative is unavailable, Fruit reports that and retains only unexpired programmes previously obtained from that same selection; it does not silently switch to the original stream's schedule.

Verification: 58 persistent-channel and EPG/lineup tests passed, including offline link search, deduplication across the two saved caches, short searches matching longer station names without mixing different station numbers, replacement of a native source, stable exported IDs, clearing old schedules, and restoring the native source. Safari preview verified candidate display and draft selection inside Edit Channel. The chooser was also verified in the live FOX 5 setup form.

## External XMLTV / zap2xml

### Browse the imported file and assign stations

Open **Persistent Channels → External XMLTV / zap2xml → Browse imported guide & assign channels**. Search by any imported display name or station ID; clear the search to browse all stations, 50 at a time. Each station lists its imported programme count and the persistent channels already using it.

Select **Preview & assign** to see up to 20 current/upcoming programmes, with titles, descriptions, and times in your browser's local timezone. Choose a saved channel from **Assign to persistent channel**, select **Assign guide**, and review the confirmation. A successful assignment immediately copies that station's saved schedule into the enabled channel's guide. A disabled channel keeps its assignment for when it is enabled. Empty schedules are shown explicitly. The same station can serve multiple channels carrying the same schedule.

This browser and assignment use the existing imported snapshot without downloading XMLTV or contacting IPTV providers. Assignments preserve the stream, channel number, and exported guide identity. If another operation changed the channel's guide since you opened the browser, assignment stops and asks you to refresh the list. Existing links can also be changed or cleared in **Edit Channel**.

API: `GET /api/xtream/epg/external/stations?q=&offset=0` pages imported stations and current assignments; `GET /api/xtream/epg/external/station?guide_id=xmltv:1:20367` previews a saved station. `POST /api/xtream/epg/external/assign` accepts `persistent_id`, `guide_id`, and optional `expected_source_id`; it validates both selections, changes only that channel's source, and copies only its selected schedule.

### Add and manage multiple sources

Under **Persistent Channels → External XMLTV / zap2xml**, enter a **Source name** and the HTTP(S) **XMLTV file URL**, then choose **Add & import source**. Repeat for each guide file. Each source has its own station/programme counts, refresh time, and failure status. If the initial download fails, its configuration remains saved; use **Refresh** to retry. No TrueNAS login or additional IPTV account is required.

Each source row offers **Refresh**, **Browse**, **Edit source**, and **Remove**. Edit can rename a source without changing its assignments. A URL change or removal is blocked while any enabled or disabled channel uses that source; reassign or clear those links first. Changing an unassigned source URL clears its old snapshot. Removed source IDs are never reused.

The imported-station browser has a **Guide source** filter: browse all sources together or choose one. Station rows and previews show the source name. Identical raw station IDs from different feeds stay separate through source-qualified identities such as `xmltv:1:20367` and `xmltv:2:20367`. Existing single-source data and selected channel links migrate automatically, preserving exported channel IDs and cached schedules.

External stations also appear in **Find EPG links**. When the channel has no provider-supplied guide ID, matching imported XMLTV / zap2xml stations are shown before provider suggestions. Match thresholds still apply; a guide is assigned only when you choose it and save. Channels with a provider guide ID keep the normal similarity ordering. Source names distinguish matching stations from different feeds. In channel setup, choose **Filter sources** beside **Find EPG links** to show **Provider guides only**, **All imported XMLTV / zap2xml guides**, or one named feed. Choose **All sources** to clear the filter. Filtering applies before pagination and works with Similar names, Broader name matches, and All saved guides; changing the source starts at the first page and preserves the unsaved guide selection.

Regular guide refreshes check each source independently at most once every six hours; its **Refresh** button forces a check. HTTP downloads have a 128 MiB decoded size limit, read timeout and overall download deadline. Parsing and staging use disk storage; only a completely valid XMLTV snapshot replaces that source's cache. Failed imports keep its last snapshot without changing other feeds. Future schedules are limited to 31 days; no programmes are fabricated.

API: `GET /api/xtream/epg/external/sources` lists sources; `POST` with `{"name":"Cable guide","url":"http://your-nas:8098/cable.xml"}` saves one without downloading. `PATCH /sources/<id>` edits its name/URL; `DELETE` removes an unassigned source. `POST /sources/<id>/refresh` imports only that feed and applies its selected schedules. The station browser accepts an optional `source_id` filter. Legacy `/api/xtream/epg/external` remains available for the first source. Source management, exports, guide searches, preview and assignment stay offline; only explicit imports and due guide refreshes download XMLTV. Neither import path checks IPTV accounts.
