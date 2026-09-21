# Booru Video Importer

## Install through Stash

Add this source in **Stash → Settings → Plugins** after GitHub Pages deployment is enabled for this repository:

```text
https://purpsll.github.io/Booru-Video-Importer/main/index.yml
```

Refresh plugin sources and install **Booru Video Importer**.


A separate Stash plugin for reverse-searching **video scenes** against **e621** and **Rule34** metadata.

It does not process Stash Images and it does not share the image Booru Importer's queues or status tags.

## How it works

The plugin deliberately separates **discovery** from **verification**:

1. **Fast Scan** checks the local video MD5 against e621 and Rule34. If the exact uploaded video file is found, the source metadata is imported.
2. **Deep Reverse Search** extracts several representative JPEG frames from the local Stash video using Stash's configured FFmpeg.
3. **SauceNAO** can use those frames to locate e621 or Rule34 post IDs. SauceNAO is only a locator; its tags are never treated as authoritative metadata.
4. When e621 credentials are configured, the plugin can use e621's **ERIS-backed reverse-image search** as a limited supplemental locator. It is intentionally restricted so repeated frame uploads do not hammer e621.
5. Candidate posts are fetched from e621 or Rule34 and must contain an actual video file.
6. The candidate video is opened remotely with FFmpeg and compared to the local Stash video across several perceptual frame hashes.
7. Only a high-confidence multi-frame video match imports metadata. Borderline video matches are placed in **Booru Video Review** instead.

The plugin also has an **e621 source-first** mode. It scans e621 WebM/MP4 posts, hashes an early useful frame plus an early proportional frame, compares those hashes against a persistent local Stash frame index, and only then performs full multi-frame verification. This mode does not depend on ERIS to find the local scene.

This prevents a single coincidentally similar frame from tagging the wrong video.

## Metadata imported

For a verified match, the plugin merges rather than deletes existing Stash metadata:

- Source post URL and explicit source URLs
- Post date when the Stash scene does not already have a date
- General/source tags
- e621/Rule34 character tags as Stash Performers
- The first artist as the Stash Studio when the scene does not already have one
- Additional artists remain visible as tags
- A Booru Video Imported workflow marker

The plugin does not overwrite an existing Studio or date.

## Workflow tags

The video plugin uses its own markers:

- Booru Video Imported
- Booru Video Unresolved
- Booru Video Review
- Booru Video No Match
- Booru Video Retry Later

These are separate from the image importer's Multi-Booru markers.

## Tasks

**1. Scan Unprocessed Videos (Fast)**  
Checks exact video MD5 only. Misses are queued for Deep Reverse Search.

**2. Preview Deep Reverse Search (10 Videos, No Changes)**  
Runs the expensive matching pipeline on ten unresolved videos without changing Stash.

**3. Deep Reverse Search All Unresolved Videos**  
Extracts frames, discovers candidates, verifies actual candidate videos, and imports verified source metadata.

**4. Recheck Video Review Candidates**  
Runs full verification again for borderline candidates.

**5. Retry Video No-Match Scenes**  
Useful later when booru indexes have gained new posts.

**6. Build / Refresh Local Video Frame Index**  
Creates or refreshes the cached early-frame hashes used by source-first matching. Unchanged files reuse cached hashes.

**7. Preview e621 Source-First Match (25 Posts, No Changes)**  
Tests recent e621 WebM/MP4 posts against the local frame index without changing Stash.

**8. Continue e621 History Match to Local Stash**  
Continues backward from the saved e621 cursor for the current Stash Tag Scope. An e621 video's API duration must exactly match a local video's normalized millisecond duration before the remote video is opened. Only then are early frames and seven aligned verification frames examined.

**9. Reset e621 Source-First Cursor**  
Resets the current Stash Tag Scope to the newest e621 videos. Use this after adding local videos when you want previously scanned history reconsidered.

## Dynamic Stash Tag Scope

Set **Stash Tag Scope (any tag or alias)** to any existing Stash tag name or alias. For example, entering `furry` limits the tagger to local video scenes that already carry that tag.

The tag is resolved live from your Stash database, so there is no hardcoded tag list. Custom tags and tags created later work automatically. Leave the field blank to process all eligible videos.

The scope applies across Fast Scan, Deep Scan, Review/Retry processing, local frame indexing, and e621 source-first matching.

## Settings

### Protect Organized Scenes

Enable **Protect Organized Scenes** to make Stash scenes marked Organized completely read-only to this plugin. Protected scenes receive no tags, performers, studio, URLs, dates, or workflow markers.

### Exact duration matching

Source-first matching has no duration tolerance. Durations are normalized to integer milliseconds and must match exactly before any e621 video frame is read. Missing e621 duration metadata is skipped rather than probed remotely.

### Historical cursor

Each Stash Tag Scope has its own persistent e621 history cursor. Full runs continue older; previews never advance the cursor.

### e621

e621 username + e621 API key are recommended. They enable authenticated API access. Source-first matching uses the normal e621 posts API and video files; ERIS is only a limited supplemental locator for the local-first reverse-search path.

### Rule34

Rule34 user ID + Rule34 API key are required to resolve Rule34 candidates and authoritative Rule34 tag categories.

### SauceNAO

A SauceNAO API key is strongly recommended for Deep Reverse Search. The polling ceiling can be left at 0/blank for Auto; the plugin learns the account's reported short-term allowance, including paid accounts.

## Important matching behavior

A frame-search result is **not** considered a video match by itself.

The plugin downloads no permanent copy of a candidate video. FFmpeg reads the remote video stream only long enough to extract comparison frames. A candidate must pass multi-frame perceptual verification before metadata is applied.

If providers are temporarily unavailable or credentials are insufficient to resolve a candidate, the scene is kept as **Retry Later** rather than being incorrectly marked **No Match**.
