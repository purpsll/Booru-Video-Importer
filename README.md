# Booru Video Importer

**Booru Video Importer** is a standalone Stash plugin for identifying NSFW video scenes against **e621** and **Rule34** and importing source metadata after the actual video is verified.

This repository is intentionally separate from the image-only **Booru Importer**.

## What it does

The plugin supports three conservative matching paths:

1. **Fast Scan** checks the Stash video's MD5 against e621 and Rule34.
2. **Deep Reverse Search** starts from a local Stash scene, extracts representative frames, uses SauceNAO plus a limited e621 ERIS fallback to locate candidate posts, then verifies the actual candidate video across multiple frames.
3. **e621 Source-First Match** works in the opposite direction: it scans e621 WebM/MP4 posts, hashes early frames, compares them against a cached early-frame index of local Stash videos, and runs full multi-frame verification before importing anything.

A single matching frame is **never enough** to automatically tag a scene. Borderline multi-frame matches are placed in **Booru Video Review**.

## Metadata

For a verified match the plugin can merge:

- canonical e621 or Rule34 post URL
- source URLs
- source post date when the Stash scene has no date
- booru tags
- character tags as Stash Performers
- artist information as Stash Studio metadata

Existing Studio and date values are preserved.

## Install through Stash

After GitHub Pages has deployed this repository, add this plugin source in **Stash → Settings → Plugins**:

```text
https://purpsll.github.io/Booru-Video-Importer/main/index.yml
```

Refresh plugin sources and install **Booru Video Importer**.

## Recommended workflow

For local-first matching:

1. **Scan Unprocessed Videos (Fast)**
2. **Preview Deep Reverse Search (10 Videos, No Changes)**
3. **Deep Reverse Search All Unresolved Videos**
4. Review scenes tagged **Booru Video Review**

For e621 source-first matching:

1. **Build / Refresh Local Video Frame Index**
2. **Preview e621 Source-First Match (25 Posts, No Changes)**
3. **Match e621 Videos to Local Stash**

The local frame index is cached and unchanged files reuse their hashes on later runs.

Source-first matching is intentionally strict: both early frames must match at the same positions, duration is filtered to within 1 second by default, and final verification compares stronger 256-bit perceptual hashes at seven aligned timestamps. A high-confidence result requires all seven sampled frames to pass plus nearly identical runtime. **Allow Source-First Auto Import** is off by default, so verified source-first results go to Review until you explicitly enable automatic metadata writes.

The plugin uses its own status tags and does not share queues with the image Booru Importer.

## Credentials

For best results configure:

- e621 username + API key
- Rule34 user ID + API key
- SauceNAO API key

SauceNAO is used only for candidate discovery. Imported metadata always comes from the matched source post.

## Scope Matching to Any Stash Tag

Use **Stash Tag Scope (any tag or alias)** to limit the plugin to a subset of your local Stash videos.

For example, entering `furry` makes the plugin process only scenes that already carry the Stash tag `furry`. The value is resolved live against the Stash tag database, including aliases, so it is not limited to a hardcoded list and future/custom tags work automatically. Leave the setting blank to process all eligible videos.

This scope applies to Fast Scan, Deep Scan, Review/Retry queues, local frame indexing, and e621 source-first matching.

## Protect Organized Scenes

Enable **Protect Organized Scenes** in the plugin settings if Stash's Organized flag means a scene is finished and should be left alone.

When enabled, an Organized scene is completely read-only to this plugin: no tags, performers, studio, URLs, dates, or workflow markers are added or changed.

## Safety

Back up your Stash database before large bulk metadata changes. Temporary provider failures are kept retryable rather than being converted into permanent No Match results.

## License

GNU Affero General Public License v3.0.
