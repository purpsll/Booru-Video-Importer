# Booru Video Importer

A focused Stash video importer that matches local videos against **e621 video history**.

## Exact-file MD5 first

Before any duration crawl or frame extraction, the importer reads the MD5 fingerprint from the exact primary Stash video file and asks e621 directly for that MD5.

A returned e621 video with the identical MD5 is a byte-for-byte file match, so metadata is imported immediately. If there is no MD5 fingerprint, the lookup fails, or e621 has no exact-file match, the same task automatically continues with the exhaustive duration/frame workflow.

## Primary workflow

The importer processes one eligible Stash video at a time:

1. Check the primary local video's MD5 directly against e621.
2. If the MD5 matches an e621 video, import immediately and move to the next local file.
3. If MD5 does not match, get the local video's duration and normalize it to integer milliseconds.
4. Walk e621 WebM video history from newest to oldest.
5. Skip posts with missing duration.
6. Skip every video whose duration is not exactly equal to the local duration.
7. For each exact-duration candidate, extract frames from the local and remote videos at the same timecodes.
8. If the frames do not match, continue to the next exact-duration e621 video.
9. If the candidate passes strict aligned verification, import all supported e621 metadata and stop searching for that local file.
10. If WebM history is exhausted, repeat through MP4 history.
11. If both histories are exhausted with no verified match, move to the next local Stash video and begin again from newest.

## Why exact duration is first

Remote e621 video data is not opened until the API-reported duration exactly matches the local Stash duration after millisecond normalization.

Examples:

- local `60.000 s`, e621 `60.000 s` → candidate may be frame-scanned;
- local `60.000 s`, e621 `59.999 s` → skipped;
- local `60.000 s`, e621 `60.001 s` → skipped;
- missing e621 duration → skipped.

## Local frame cache

The plugin maintains an internal lazy frame-hash cache for Stash videos.

There is no manual index task. The cache is created only when a local video is actually processed, and unchanged files reuse cached hashes.

## Continuous scanning

The main matching task defaults to continuous mode. There is no plugin-level maximum page count in continuous mode.

## Saved progress

A complete e621-history search may span multiple plugin runs.

The plugin saves:

- active Stash scene;
- active e621 format (WebM or MP4);
- current e621 history cursor;
- completed local scene IDs.

The next run resumes the same local video until it either matches or exhausts both e621 histories.

Each Stash Tag Scope has independent progress.

## Stash Tag Scope

**Stash Tag Scope (any tag or alias)** accepts any live Stash tag name or alias.

For example, `furry` restricts the importer to local video scenes already carrying that tag.

Leave it blank to process all eligible local videos.

## Organized protection

When **Protect Organized Scenes** is enabled, Organized scenes are ignored completely.

## Imported metadata

For a strict verified e621 match the plugin merges:

- e621 canonical post URL;
- source URLs;
- post date if the Stash scene date is blank;
- general/species/copyright/lore tags;
- character tags as Performers;
- first artist as Studio when Studio is blank;
- additional artists as tags;
- `Booru Video Imported` marker.

## Tasks

### Preview Next Stash Video Match (No Changes)

Checks one local video against a small e621 history window without changing Stash or saved progress.

### Match Filtered Stash Videos Against e621

Main task. Repeated runs continue the current local video until a match is found or e621 video history is exhausted.

### Reset Matching Progress

Resets the current Stash Tag Scope to the first eligible local video and newest e621 history.

## Removed legacy workflows

The rebuilt importer no longer includes:

- the old separate Fast MD5 task (MD5 is now an internal first-stage optimization);
- Deep Reverse Search;
- SauceNAO;
- e621 ERIS reverse-image search;
- Rule34 matching;
- Review/No-Match/Retry queues;
- manual local frame-index building.

Those workflows are unnecessary for the new Stash-first exhaustive e621 matcher.
