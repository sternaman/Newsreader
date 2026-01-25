# NYTRSS Docker (no Nix)

This folder builds and runs `nytrss` in Docker without Nix. It writes RSS feeds to `./data/build` and serves them on port 8000.

## Quick start

1) Export NYT cookies to Netscape `cookies.txt` format (see below).
2) Save the file as `scripts/nytrss_docker/data/nyt_cookies.txt`.
3) Run:

```bash
cd scripts/nytrss_docker
docker compose up --build
```

Feeds will be available at:

- `http://<PC-IP>:8000/nyt/home-page.rss`
- `http://<PC-IP>:8000/nyt/business.rss`
- `http://<PC-IP>:8000/nyt/politics.rss`

## Cookies

Use a browser extension like **Get cookies.txt** to export cookies for `nytimes.com`.
Save as `data/nyt_cookies.txt`. The container reads `NYTRSS_COOKIE_FILE=/data/nyt_cookies.txt`.

If you prefer, you can set a raw Cookie header instead:

```bash
export NYTRSS_COOKIE="name=value; name2=value2"
```

Then add it under `environment:` in `docker-compose.yml`.
