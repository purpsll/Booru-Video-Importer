# Booru Video Importer

**Booru Video Importer** is a standalone Stash plugin that matches local Stash video scenes against **e621 video posts** and imports authoritative e621 metadata only after the actual videos are verified.

The plugin is intentionally Stash-first: it works on one local video at a time.

## How matching works

For each eligible local Stash video:

1. Read the MD5 fingerprint from the **exact primary Stash video file** being processed.
2. Ask e621 directly for that MD5. A byte-identical e621 video match imports metadata immediately with no duration crawl or frame extraction.
3. If MD5 is unavailable or finds no e621 video, read the local duration and normalize it to integer milliseconds.
4. Search e621 **video posts only**, first WebM history and then MP4 history, from newest to oldest.
5. Reject every e621 video whose duration does not exactly match the local video's normalized millisecond duration.
6. Only for an exact-duration candidate, open the remote video and extract comparison frames.
7. Compare local and e621 frames at the same timecodes.
8. If the candidate fails, continue to the next exact-duration e621 video.
9. On the first strict verified match, import the e621 metadata and move to the next local Stash video.
10. If all e621 WebM and MP4 history is exhausted without a verified match, move to the next local Stash video.

A single similar frame is not enough. Exact-duration candidates must also pass strict aligned multi-frame verification before metadata is written.

## Exact-file MD5 fast path

When the Stash video is byte-for-byte identical to the file hosted by e621, the importer uses a direct e621 MD5 lookup first. This is definitive and avoids the historical scan entirely.

The fingerprint is read from the same primary Stash file being processed, so another file attached to a multi-file scene cannot supply the MD5 accidentally. If no MD5 is available or e621 has no exact-file match, the importer automatically falls back to the exhaustive duration/frame workflow.

## Efficient local frame cache

The plugin keeps an internal local frame-hash cache so it does not repeatedly extract the same Stash comparison frames.

This cache is automatic and lazy:

- there is no manual "Build Index" task;
- hashes are created only when a local video becomes active;
- unchanged local files reuse their cached hashes;
- the cache is only an optimization and does not change matching rules.

## Persistent e621 history progress

Searching all of e621 can take many runs, so progress is saved.

The saved state belongs to the **current local Stash video**:

- if a run reaches its configured e621 page budget, the next run resumes that same local video at the next older e621 page;
- WebM and MP4 are scanned separately so interleaved post histories cannot be skipped;
- the plugin does not advance to the next local video until the current one either matches or exhausts both e621 video histories;
- when the next local video starts, e621 history starts from newest again.

Progress is stored separately for each **Stash Tag Scope**.

## Dynamic Stash Tag Scope

Set **Stash Tag Scope (any tag or alias)** to any existing Stash tag name or alias.

For example, entering:

`furry`

means only local video scenes already carrying the Stash tag `furry` are processed.

The tag is resolved live from Stash. There is no hardcoded list, so custom tags and aliases work automatically. Leave the setting blank to process all eligible local videos.

## Protect Organized Scenes

Enable **Protect Organized Scenes** if Stash's Organized flag means the scene should no longer be modified.

When enabled, Organized scenes are excluded completely.

## Metadata imported

After a strict verified e621 match, the plugin merges:

- canonical e621 post URL;
- explicit source URLs from the e621 post;
- e621 post date when the Stash scene does not already have a date;
- general, species, copyright, and lore tags;
- e621 character tags as Stash Performers;
- first e621 artist as Stash Studio when the scene has no Studio;
- additional artists as tags;
- `Booru Video Imported` marker tag.

Existing Stash Studio and date values are preserved.

## Tasks

### Preview Next Stash Video Match (No Changes)

Tests the next eligible local video against a limited e621 history window. It makes no metadata changes and does not change saved progress.

### Match Filtered Stash Videos Against e621

This is the main importer.

It keeps one local Stash video active, walks e621 WebM and MP4 history, filters by exact duration before opening any remote video, checks exact-duration candidates in sequence, and imports metadata on the first strict verified match.

### Reset Matching Progress

Clears saved progress for the currently selected Stash Tag Scope. The next run starts again from the first eligible local video and newest e621 WebM history.

## Settings

### e621 History Pages per Stash Video per Run

Set this to **0** or leave it blank for continuous scanning. In continuous mode, the importer keeps the active local Stash video and scans until it either finds a strict verified e621 match or exhausts all WebM + MP4 video history. It then automatically starts the next eligible local video.

Set a positive number only when you intentionally want to cap a run to that many e621 pages. If a capped or interrupted run stops early, saved progress resumes the same local file safely on the next run.

### e621 credentials

An e621 username and API key are recommended for authenticated API access.

The plugin no longer uses ERIS, SauceNAO, Rule34, Fast Scan, Deep Reverse Search, Review queues, No-Match queues, or a manual frame-index task.

## Install through Stash

Add this plugin source in **Stash → Settings → Plugins**:

`https://purpsll.github.io/Booru-Video-Importer/main/index.yml`

Refresh plugin sources and install or update **Booru Video Importer**.

## Safety

Back up your Stash database before large bulk metadata operations.

## License

GNU Affero General Public License v3.0.
