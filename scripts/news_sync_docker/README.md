# News Sync Docker (Orbstack)

This container generates XTCH bundles on a schedule and serves a simple OPDS feed
for Crosspoint X4 News Sync.

## Setup

1) Create secrets directory and place cookies (Netscape format):

- `scripts/news_sync_docker/secrets/wsj_cookies.txt`
- `scripts/news_sync_docker/secrets/nyt_cookies.txt`

2) Optional: edit `scripts/news_sync_docker/config/news_sources.json` to add/remove sources.
   - Set `output_format` to `epub` or `xtch` (default is `xtch`).

3) Build + run:

```
cd scripts/news_sync_docker
docker compose up -d --build
```

The server will:
- Generate EPUB or XTCH files into `scripts/news_sync_docker/data/news_out`
- Serve OPDS feed at `http://<host-ip>:8081/news.xml`
- Refresh on an interval (default every 60 minutes, local time)
- Keep only the latest bundle per source (older files are deleted)
- Prevent overlapping runs with a lock (skips if a build is already running)

To change the refresh schedule, set `SCHEDULE_TIMES` (comma-separated HH:MM) or
adjust `REFRESH_SECONDS` in `docker-compose.yml`.

## X4 Settings

- OPDS Server URL: `http://<host-ip>:8081`
- News Feed Path: `news.xml`

## Notes

- The container bundles Calibre (ebook-convert) + Python deps.
- Keep cookies in `secrets/`; they are not committed.
