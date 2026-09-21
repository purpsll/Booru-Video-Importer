# Booru Video Importer

**Booru Video Importer** matches local Stash videos against a persistent local catalog of **e621 WebM and MP4 posts**.

Instead of rescanning e621 history for every Stash video, the plugin builds one reusable SQLite database containing e621 video metadata, exact duration, MD5, deterministic sample timecodes, and perceptual frame hashes.

## First-time setup

Run:

**1. Build / Update e621 Video Catalog**

The plugin creates the SQLite database automatically. You do not create or configure SQLite yourself.

On a normal Stash install the persistent files are stored outside the plugin install directory under:

`<Stash config directory>/booru-video-importer/`

For example, a Docker/default Linux Stash install commonly resolves to:

`/root/.stash/booru-video-importer/e621_video_catalog.sqlite`

The local Stash pHash cache is stored beside it as:

`booru_video_hash_index.json`

Keeping these files outside `plugins/BooruVideoImporter` means updating or reinstalling the plugin does not discard a catalog that may have taken a long time to build.

The first complete historical catalog build is the expensive operation. It is **resumable**. If interrupted, rerun the task and it continues from the saved WebM/MP4 e621 cursors and retries transient pHash failures. After the historical catalog is current, later runs only add newer e621 videos.

The matcher can use a partially built catalog, but a complete catalog gives complete historical coverage.

## What is stored for each e621 video

Each SQLite row stores:

- e621 post ID;
- video format (WebM or MP4);
- exact normalized duration in milliseconds;
- original e621 video URL;
- e621 MD5;
- exact 10%, 50%, and 90% sample timecodes in milliseconds;
- three real 64-bit DCT perceptual hashes from those frames;
- hash version, retry count, and any transient hash error.

SQLite indexes duration and MD5 so matching does not scan every row.

## Matching workflow

For each eligible Stash video:

1. Check the MD5 fingerprint from the exact primary Stash video file.
2. If the local catalog contains the same MD5, fetch that e621 post and import immediately. A byte-identical file needs no frame comparison.
3. If necessary, also try the direct e621 MD5 lookup in case the catalog has not yet indexed a very new post.
4. If MD5 does not match, calculate/reuse the local video's exact duration and 10%, 50%, and 90% pHashes.
5. Query SQLite for **only e621 rows with the exact same duration in milliseconds**.
6. Compare the three cached pHashes locally. Candidates that are not perceptually close are discarded without opening their e621 URLs.
7. Rank plausible candidates by pHash distance.
8. Open only a plausible candidate's video URL and extract the **50% frame at the same timecode**.
9. Compare that live midpoint frame against the Stash video's midpoint pHash.
10. If it verifies, fetch the authoritative e621 post metadata and import it.
11. If it fails, try the next locally ranked candidate.

This means routine matching no longer walks e621 history for each Stash file.

## Metadata imported after a verified match

Both MD5 and pHash matches use the same authoritative metadata-import path. The plugin merges:

- canonical e621 post URL;
- source URLs supplied by e621;
- general, species, copyright, and lore tags;
- e621 character tags as Stash Performers;
- the first e621 artist as the Stash Studio when the scene has no Studio;
- additional artists as tags;
- e621 post date when the Stash date is blank;
- `Booru Video Imported` marker tag.

Existing Stash date and Studio values are preserved. Existing **Details** text is also left untouched; e621 tags/source data are not dumped into the narrative Details field.

## Stash Tag Scope

**Stash Tag Scope (any tag or alias)** accepts any current Stash tag name or alias.

For example, `furry` restricts matching to local video scenes already carrying that tag. Leave it blank to process all eligible local videos.

## Protect Organized Scenes

When **Protect Organized Scenes** is enabled, Organized scenes are excluded completely and are not modified.

## Tasks

### 1. Build / Update e621 Video Catalog

Creates or resumes the catalog. It crawls e621 video posts in ascending ID order, stores metadata, and generates the three pHashes for each video.

### 2. Show e621 Video Catalog Status

Reports total rows, successfully hashed rows, failed hash rows, duration buckets, and the saved WebM/MP4 crawl cursors.

### 3. Preview Next Stash Video Match (No Changes)

Runs the matcher for one eligible Stash video without changing Stash metadata.

### 4. Match Filtered Stash Videos Against e621 Catalog

Main metadata importer. Uses MD5 first, then exact-duration SQLite lookup, cached pHash filtering, and one live midpoint-frame verification.

## e621 credentials

An e621 username and API key are recommended for catalog crawling and post lookups.

## Install through Stash

Add this plugin source in **Stash → Settings → Plugins**:

`https://purpsll.github.io/Booru-Video-Importer/main/index.yml`

Refresh plugin sources and install or update **Booru Video Importer**.

## Safety

Back up your Stash database before large bulk metadata operations.

## License

GNU Affero General Public License v3.0.
