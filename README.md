# Booru Video Importer

**Booru Video Importer** is a standalone Stash plugin for identifying NSFW video scenes against **e621** and **Rule34** and importing source metadata after the actual video is verified.

This repository is intentionally separate from the image-only **Booru Importer**.

## What it does

The plugin uses a conservative two-stage workflow:

1. **Fast Scan** checks the Stash video's MD5 against e621 and Rule34.
2. **Deep Reverse Search** extracts representative frames with FFmpeg, uses SauceNAO and e621 ERIS to discover candidate posts, requires the candidate post to contain a video, and compares the local and remote videos across multiple perceptual frame hashes.

A single matching frame is **not enough** to automatically tag a scene. Borderline multi-frame matches are placed in **Booru Video Review**.

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

1. **Scan Unprocessed Videos (Fast)**
2. **Preview Deep Reverse Search (10 Videos, No Changes)**
3. **Deep Reverse Search All Unresolved Videos**
4. Review scenes tagged **Booru Video Review**

The plugin uses its own status tags and does not share queues with the image Booru Importer.

## Credentials

For best results configure:

- e621 username + API key
- Rule34 user ID + API key
- SauceNAO API key

SauceNAO is used only for candidate discovery. Imported metadata always comes from the matched source post.

## Safety

Back up your Stash database before large bulk metadata changes. Temporary provider failures are kept retryable rather than being converted into permanent No Match results.

## License

GNU Affero General Public License v3.0.
