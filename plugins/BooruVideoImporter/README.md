# Booru Video Importer

The plugin maintains one persistent **SQLite e621 video catalog** and reuses it for every Stash video.

## Database location

No manual database setup is required. The plugin creates SQLite automatically in:

`<Stash config directory>/booru-video-importer/e621_video_catalog.sqlite`

On a typical Linux/Docker Stash install this is commonly:

`/root/.stash/booru-video-importer/e621_video_catalog.sqlite`

The local Stash pHash cache is stored in the same persistent folder.

These files are outside the plugin install directory so a plugin update does not erase them.

## Build / Update e621 Video Catalog

Run this task before expecting complete pHash matching coverage.

The first historical build:

- walks e621 WebM and MP4 posts once;
- stores post ID, URL, MD5 and exact duration;
- records deterministic 10%, 50%, and 90% frame timecodes;
- generates three real 64-bit DCT perceptual hashes;
- saves per-format ascending e621 cursors;
- retries transient frame/hash failures up to a bounded number of attempts.

The task is resumable. Later runs only add newer e621 posts plus retry pending failures.

A partially built catalog can already be used for matching, but only indexed/hashed rows are available to the pHash matcher.

## Main matching workflow

For each filtered Stash video:

1. MD5 lookup first.
2. If MD5 does not match, use exact duration to query SQLite.
3. Compare local 10/50/90 pHashes against only that exact-duration bucket.
4. Discard non-close candidates locally.
5. Pull only plausible candidate URLs.
6. Verify one live 50% frame at the identical timecode.
7. Fetch the real e621 post and import metadata after verification.

## Imported metadata

Verified matches merge:

- e621 post URL and source URLs;
- tags;
- characters as Performers;
- first artist as Studio when Studio is blank;
- additional artists as tags;
- e621 post date when date is blank;
- `Booru Video Imported`.

Existing Details text remains unchanged.

## Tasks

**1. Build / Update e621 Video Catalog** — initial historical build and future incremental updates.

**2. Show e621 Video Catalog Status** — inspect row/hash/cursor status.

**3. Preview Next Stash Video Match (No Changes)** — test one local video.

**4. Match Filtered Stash Videos Against e621 Catalog** — main importer.

## Removed legacy workflow

The plugin no longer performs a complete e621 history crawl separately for every Stash video. The catalog exists specifically to eliminate that repeated work.
