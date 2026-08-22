# DJDownload

A containerized service for archiving YouTube DJ sets.

DJDownload downloads video and/or audio from YouTube, embeds thumbnails, and tags audio files with `RELEASETYPE=album;live` so Plex recognizes DJ sets as **live albums**, keeping them separate from studio releases.

---

## Features

### Core Features (MVP)

- Web UI to submit YouTube URLs
- Download **video, audio, or both**
- Configurable **audio and video output directories**
- Automatic **MP3 tagging**
  - **Title** = YouTube video title
  - **Album** = YouTube video title
  - **Artist** = YouTube channel name or custom override
  - **Album art** = YouTube thumbnail
  - **RELEASETYPE** = `album;live`
- Outputs **one final tagged MP3**

### Missed-Set Finder (Discover tab)

Search YouTube for sets you might be missing from DJs already in your library:

- Pick a DJ (or search **All DJs**) and search YouTube for matching videos
- Filters to videos **longer than 30 minutes**
- Cross-references against your library — by embedded YouTube video ID when available, otherwise by fuzzy title matching — so you only see **new/missing candidates**
- Results are a **review queue**: Download or Dismiss each one manually, nothing downloads automatically
- Dismissed candidates are remembered and won't resurface on future searches

Requires a **YouTube Data API v3 key** (Google Cloud Console → enable "YouTube Data API v3" → create an API key), set in the Discovery card on the Settings tab. Each search costs API quota — `search.list` is 100 units against a default free quota of 10,000 units/day (~100 searches/day), and "All DJs" uses one search per DJ in your library.

---

## Configuration

- Enable/disable **audio downloads**
- Enable/disable **video downloads**
- **Audio output directory**
- **Video output directory**
- **Artist mode**
  - Use YouTube channel name
  - Use custom artist name

---

## Planned Features

- Register **YouTube channels**
- Poll channels for **new uploads**
- **Per-channel rules** (artist override, duration filters, etc.)

---

## Goals

- Maintain a clean **Plex-compatible library for DJ sets**
- Automatically tag sets as **live albums**
- Provide a simple workflow for archiving long-form mixes from YouTube

---

## Deploying on Unraid

Every push to `main` builds and publishes the image to `ghcr.io/wmhunter96/djdownload:latest` via [GitHub Actions](.github/workflows/docker-publish.yml). An [Unraid template](unraid/djdownload.xml) is included so the container installs and updates through the normal Docker UI instead of hand-editing `docker-compose.yml`.

**One-time setup:**

1. On GitHub, go to the repo's **Packages** tab → `djdownload` package → **Package settings** → change visibility to **Public**. (Only needed once — GHCR packages default to private, and a private image needs a login secret on the Unraid side to pull.)
2. On Unraid: **Docker** tab → **Template Repositories** → add `https://github.com/wmhunter96/DJDownload` → **Save**.
3. Go to **Apps** (or Docker → **Add Container** → template dropdown) and select **DJDownload**. Adjust the audio/video/config paths to match your shares, then **Apply**.

**After that:** any new push to `main` refreshes the `latest` tag on GHCR, and Unraid's normal container update check (Docker tab, or the Community Applications "Check for Updates") will offer the update — no manual pulling or compose edits needed.
