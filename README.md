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
- Only process videos **longer than X minutes** (ex: 40 minutes)
- **Duplicate prevention**
- **Per-channel rules** (artist override, duration filters, etc.)

---

## Goals

- Maintain a clean **Plex-compatible library for DJ sets**
- Automatically tag sets as **live albums**
- Provide a simple workflow for archiving long-form mixes from YouTube
