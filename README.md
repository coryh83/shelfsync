# shelfsync

A small Docker service that shows where you are in each EPUB in Audiobookshelf and in Calibre,
and keeps the two in step in both directions.

- **Web UI** at `http://localhost:8080`: one row per matched ebook with the ABS and Calibre positions.
- **JSON** at `/api/status`; `POST /api/sync` runs a sync now; `/healthz` for health checks.
- Pure Python standard library, no pip dependencies.

## How it works

| Side | Read | Write |
|---|---|---|
| Audiobookshelf | `GET /api/me` → `mediaProgress[]` (`ebookLocation` = EPUB CFI, `ebookProgress` = 0..1) | `PATCH /api/me/progress/{libraryItemId}` |
| Calibre | `last_read_positions` table in `metadata.db` (`cfi`, `pos_frac`, `epoch`) | `UPDATE`/`INSERT` on the same table |

**Matching:** ISBN first (ISBN-10 and -13 both work), then normalized title plus an author-name overlap.
Ambiguous matches are skipped and logged.

**Conflicts:** The service remembers the last CFI it synced for each pair.
- If only one side moved, that side wins.
- If both moved, the one updated more recently wins.

**Scope:** EPUB only. PDF and audiobook positions are not synced.

## Setup

1. `cp .env.example .env` and fill in `ABS_URL`, `ABS_TOKEN`, and the Calibre path in `docker-compose.yml`.
2. Start with `DRY_RUN=true`, check the log and web UI, then set `DRY_RUN=false`.
3. `docker compose up -d --build`

Find the ABS token under Settings → Users → your user. Find the library ID in the ABS URL when you open the library.

## Things to know

- **Permissions.** The container runs as UID 10001. SQLite needs write access to the Calibre
  *folder* (it creates a journal file there), not just to `metadata.db`. Make the folder writable by
  that UID, e.g. `sudo chown -R 10001 "/path/to/Calibre Library"`, or run the container as your own UID.
- **Calibre must be closed or idle while it is writing.** SQLite allows one writer at a time. If the
  database is locked, the cycle fails and the error appears on the page. Calibre also will not show
  a new position until you reopen the book.
- **Check that Calibre records positions at all.** Run
  `SELECT * FROM last_read_positions;` on `metadata.db`. If the table is empty, Calibre has not
  saved any positions for this library yet, and this service has nothing to read.
- **CFI portability.** CFIs point into the EPUB's internal structure. They line up only when both
  copies are the same file (same edition and same build). If the ABS and Calibre copies differ, the
  position can land in the wrong place. The fraction is stored alongside as a fallback.
- **`CALIBRE_USER` / `CALIBRE_DEVICE`** only control the key this service writes under. Reads use the
  newest position across all users and devices.
- **The web UI has no login.** Keep it on your LAN or behind a reverse proxy with auth. Don't expose it directly.
- **ABS metadata.** Matching uses the ISBN and title/author that ABS returns in its library item listing.
  Books without an ISBN in ABS depend on the title and author matching.

## Tests

```
python3 -m unittest discover -s tests -v
```
