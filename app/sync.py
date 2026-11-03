#!/usr/bin/env python3
"""Two-way sync of EPUB reading positions between Audiobookshelf and Calibre.

Audiobookshelf
  Read:  GET  /api/me                      -> mediaProgress[] (ebookLocation = EPUB CFI,
                                              ebookProgress = 0..1, lastUpdate = epoch ms)
  Write: PATCH /api/me/progress/{libraryItemId} with {ebookLocation, ebookProgress}

Calibre
  Positions live in the `last_read_positions` table of metadata.db
  (book, format, user, device, cfi, epoch, pos_frac). Read and written directly
  with sqlite3 because Calibre has no API for this.

Conflict rule (per book pair, remembered between runs):
  - only one side moved since the last sync  -> that side wins
  - both sides moved                         -> the more recently updated side wins
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sync")

MODES = ("both", "abs_to_calibre", "calibre_to_abs")


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    abs_url: str
    abs_token: str
    calibre_db: Path
    abs_library_ids: list[str]
    calibre_user: str
    calibre_device: str
    mode: str
    interval: int
    state_path: Path
    http_port: int
    dry_run: bool


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


def load_config() -> Config:
    mode = os.environ.get("SYNC_MODE", "both").strip()
    if mode not in MODES:
        raise SystemExit(f"SYNC_MODE must be one of: {', '.join(MODES)}")
    library_ids = [x.strip() for x in os.environ.get("ABS_LIBRARY_IDS", "").split(",") if x.strip()]
    return Config(
        abs_url=_required("ABS_URL").rstrip("/"),
        abs_token=_required("ABS_TOKEN"),
        calibre_db=Path(os.environ.get("CALIBRE_LIBRARY_DIR", "/calibre")) / "metadata.db",
        abs_library_ids=library_ids,
        calibre_user=os.environ.get("CALIBRE_USER", "local"),
        calibre_device=os.environ.get("CALIBRE_DEVICE", "local"),
        mode=mode,
        interval=int(os.environ.get("SYNC_INTERVAL_SECONDS", "300")),
        state_path=Path(os.environ.get("STATE_PATH", "/data/state.json")),
        http_port=int(os.environ.get("HTTP_PORT", "8080")),
        dry_run=os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )


# --------------------------------------------------------------------------- model

@dataclass
class Pos:
    cfi: Optional[str]
    fraction: Optional[float]
    ts: float  # epoch seconds; 0 when unknown

    def as_dict(self) -> dict:
        return {
            "cfi": self.cfi,
            "fraction": self.fraction,
            "updated": datetime.fromtimestamp(self.ts, timezone.utc).isoformat() if self.ts else None,
        }


@dataclass
class CalibreBook:
    id: int
    title: str
    authors: list[str] = field(default_factory=list)
    isbn: Optional[str] = None


def normalize_isbn(raw: Optional[str]) -> Optional[str]:
    """Return an ISBN-13 digit string, converting ISBN-10 when needed."""
    if not raw:
        return None
    s = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(s) == 13 and s.isdigit():
        return s
    if len(s) == 10 and s[:9].isdigit():
        core = "978" + s[:9]
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return None


def title_key(title: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def name_tokens(names: list[str]) -> set[str]:
    """'Herbert, Frank' and 'Frank Herbert' give the same tokens."""
    out: set[str] = set()
    for n in names:
        out.update(re.findall(r"[a-z0-9]+", (n or "").lower()))
    return out


# --------------------------------------------------------------------------- Audiobookshelf

class AbsClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def _call(self, method: str, path: str, body: Optional[dict] = None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def libraries(self) -> list[dict]:
        return self._call("GET", "/api/libraries")["libraries"]

    def library_items(self, library_id: str) -> list[dict]:
        # limit=0 asks for every item in the library
        return self._call("GET", f"/api/libraries/{library_id}/items?limit=0")["results"]

    def ebook_progress(self) -> dict[str, dict]:
        me = self._call("GET", "/api/me")
        return {
            p["libraryItemId"]: p
            for p in me.get("mediaProgress", [])
            if not p.get("episodeId")  # books have no episodeId; podcasts do
        }

    def set_ebook_progress(self, item_id: str, cfi: str, fraction: float) -> None:
        self._call("PATCH", f"/api/me/progress/{item_id}",
                   {"ebookLocation": cfi, "ebookProgress": fraction})


# --------------------------------------------------------------------------- Calibre

class Calibre:
    def __init__(self, db_path: Path):
        if not db_path.exists():
            raise FileNotFoundError(f"Calibre database not found: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=rw"
        self.conn = sqlite3.connect(uri, uri=True, timeout=30)
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='last_read_positions'"
        ).fetchone()
        self.has_positions = bool(exists)

    def __enter__(self) -> "Calibre":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.close()

    def books(self) -> list[CalibreBook]:
        authors: dict[int, list[str]] = {}
        for book, name in self.conn.execute(
            "SELECT bal.book, a.name FROM books_authors_link bal JOIN authors a ON a.id = bal.author"
        ):
            authors.setdefault(book, []).append(name)
        isbns: dict[int, str] = {}
        for book, val in self.conn.execute(
            "SELECT book, val FROM identifiers WHERE lower(type) = 'isbn'"
        ):
            norm = normalize_isbn(val)
            if norm:
                isbns.setdefault(book, norm)
        return [
            CalibreBook(id=bid, title=title or "", authors=authors.get(bid, []), isbn=isbns.get(bid))
            for bid, title in self.conn.execute("SELECT id, title FROM books")
        ]

    def latest_positions(self) -> dict[int, Pos]:
        """Newest EPUB position per book, across all users and devices."""
        if not self.has_positions:
            return {}
        latest: dict[int, Pos] = {}
        rows = self.conn.execute(
            "SELECT book, cfi, epoch, pos_frac FROM last_read_positions "
            "WHERE upper(format) = 'EPUB' ORDER BY epoch ASC"
        )
        for book, cfi, epoch, frac in rows:  # ascending, so the last row per book is newest
            latest[book] = Pos(cfi=cfi, fraction=frac, ts=float(epoch or 0))
        return latest

    def write_position(self, book_id: int, cfi: str, fraction: float, user: str, device: str) -> None:
        if not self.has_positions:
            raise RuntimeError("last_read_positions table is missing in this Calibre library")
        now = time.time()
        cur = self.conn.execute(
            "UPDATE last_read_positions SET cfi = ?, epoch = ?, pos_frac = ? "
            "WHERE book = ? AND upper(format) = 'EPUB' AND user = ? AND device = ?",
            (cfi, now, fraction, book_id, user, device),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO last_read_positions (book, format, user, device, cfi, epoch, pos_frac) "
                "VALUES (?, 'EPUB', ?, ?, ?, ?, ?)",
                (book_id, user, device, cfi, now, fraction),
            )
        self.conn.commit()


# --------------------------------------------------------------------------- matching

def build_index(books: list[CalibreBook]):
    by_isbn: dict[str, CalibreBook] = {}
    by_title: dict[str, list[CalibreBook]] = {}
    for b in books:
        if b.isbn:
            by_isbn.setdefault(b.isbn, b)
        by_title.setdefault(title_key(b.title), []).append(b)
    return by_isbn, by_title


def match_item(title: str, author: str, isbn: Optional[str], by_isbn, by_title) -> Optional[CalibreBook]:
    norm = normalize_isbn(isbn)
    if norm and norm in by_isbn:
        return by_isbn[norm]
    candidates = by_title.get(title_key(title), [])
    wanted = name_tokens([author])
    if wanted:
        candidates = [b for b in candidates if wanted & name_tokens(b.authors)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        log.warning("Ambiguous match for %r by %r; skipping", title, author)
    return None


# --------------------------------------------------------------------------- decision

def decide(abs_pos: Pos, cal_pos: Pos, last_abs: Optional[str], last_cal: Optional[str], mode: str):
    """Return (direction, source Pos) to copy, or None.

    last_abs / last_cal are what each side showed on the previous cycle.
    direction is 'abs_to_calibre' or 'calibre_to_abs'.
    """
    a_changed = abs_pos.cfi is not None and abs_pos.cfi != last_abs
    c_changed = cal_pos.cfi is not None and cal_pos.cfi != last_cal

    if mode == "abs_to_calibre":
        return ("abs_to_calibre", abs_pos) if a_changed else None
    if mode == "calibre_to_abs":
        return ("calibre_to_abs", cal_pos) if c_changed else None

    if not a_changed and not c_changed:
        return None
    if a_changed and not c_changed:
        return ("abs_to_calibre", abs_pos)
    if c_changed and not a_changed:
        return ("calibre_to_abs", cal_pos)
    # both moved: newest wins
    if abs_pos.ts >= cal_pos.ts:
        return ("abs_to_calibre", abs_pos)
    return ("calibre_to_abs", cal_pos)


# --------------------------------------------------------------------------- state

class State:
    def __init__(self, path: Path):
        self.path = path
        self.pairs: dict[str, dict] = {}
        if path.exists():
            try:
                self.pairs = json.loads(path.read_text()).get("pairs", {})
            except (ValueError, OSError) as e:
                log.warning("Could not read state file %s (%s); starting fresh", path, e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pairs": self.pairs}, indent=2))
        os.replace(tmp, self.path)


def sync_pair(cfg: Config, abs_client: AbsClient, cal: Calibre, state: State,
              abs_id: str, cal_id: int, abs_pos: Pos, cal_pos: Pos) -> str:
    key = f"{abs_id}:{cal_id}"
    prev = state.pairs.get(key, {})
    decision = decide(abs_pos, cal_pos, prev.get("abs_cfi"), prev.get("cal_cfi"), cfg.mode)

    action = "in sync"
    if decision is not None:
        direction, src = decision
        target_cfi = src.cfi
        fraction = src.fraction if src.fraction is not None else 0.0

        if direction == "abs_to_calibre":
            needs_write = cal_pos.cfi != target_cfi
            label = "ABS -> Calibre"
        else:
            needs_write = abs_pos.cfi != target_cfi
            label = "Calibre -> ABS"

        if needs_write:
            if cfg.dry_run:
                return f"would sync {label}"
            try:
                if direction == "abs_to_calibre":
                    cal.write_position(cal_id, target_cfi, fraction, cfg.calibre_user, cfg.calibre_device)
                else:
                    abs_client.set_ebook_progress(abs_id, target_cfi, fraction)
            except Exception as e:  # keep going with other books; retry next cycle
                log.error("Sync %s failed for %s: %s", label, key, e)
                return f"error ({label}): {e}"
            action = f"synced {label}"
            log.info("%s: %s", action, key)

    if cfg.dry_run:
        return action

    # Record what each side showed this cycle (before any write). A write then looks
    # like a change next time, but resolves to a no-op because both sides already agree.
    # This avoids ping-pong when a server reports stale progress.
    state.pairs[key] = {
        "abs_cfi": abs_pos.cfi,
        "cal_cfi": cal_pos.cfi,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "last_action": action,
    }
    return action


# --------------------------------------------------------------------------- cycle

def run_cycle(cfg: Config, abs_client: AbsClient, state: State) -> list[dict]:
    library_ids = cfg.abs_library_ids or [lib["id"] for lib in abs_client.libraries()]

    ebook_items = []
    for lib in library_ids:
        for item in abs_client.library_items(lib):
            media = item.get("media") or {}
            if item.get("mediaType") == "book" and media.get("ebookFormat"):
                ebook_items.append(item)

    progress = abs_client.ebook_progress()
    rows: list[dict] = []

    with Calibre(cfg.calibre_db) as cal:
        by_isbn, by_title = build_index(cal.books())
        cal_positions = cal.latest_positions()

        for item in ebook_items:
            md = (item.get("media") or {}).get("metadata") or {}
            title = md.get("title") or ""
            author = md.get("authorName") or ""
            p = progress.get(item["id"])
            abs_pos = Pos(
                cfi=p.get("ebookLocation") if p else None,
                fraction=p.get("ebookProgress") if p else None,
                ts=((p.get("lastUpdate") or 0) / 1000) if p else 0.0,
            )
            book = match_item(title, author, md.get("isbn"), by_isbn, by_title)
            row = {
                "abs_id": item["id"],
                "title": title,
                "author": author,
                "abs": abs_pos.as_dict(),
                "calibre_id": None,
                "calibre": None,
                "action": "not in Calibre",
            }
            if book is not None:
                cal_pos = cal_positions.get(book.id, Pos(None, None, 0.0))
                row["calibre_id"] = book.id
                row["calibre"] = cal_pos.as_dict()
                row["action"] = sync_pair(cfg, abs_client, cal, state, item["id"], book.id, abs_pos, cal_pos)
            rows.append(row)

    if not cfg.dry_run:
        state.save()
    return rows


# --------------------------------------------------------------------------- status + web

_status_lock = threading.Lock()
STATUS: dict = {"rows": [], "last_run": None, "last_error": None, "mode": None, "dry_run": None}


def publish(cfg: Config, rows: Optional[list[dict]], error: Optional[str]) -> None:
    with _status_lock:
        if rows is not None:
            STATUS["rows"] = rows
        STATUS["last_error"] = error
        STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
        STATUS["mode"] = cfg.mode
        STATUS["dry_run"] = cfg.dry_run


def snapshot() -> dict:
    with _status_lock:
        return json.loads(json.dumps(STATUS))


def _fmt_pos(pos: Optional[dict]) -> str:
    if not pos:
        return "-"
    frac = pos.get("fraction")
    pct = f"{frac * 100:.1f}%" if isinstance(frac, (int, float)) else "-"
    upd = pos.get("updated")
    upd = datetime.fromisoformat(upd).astimezone().strftime("%Y-%m-%d %H:%M") if upd else "-"
    cfi = html.escape(pos.get("cfi") or "-")
    return f"{pct}<br><small>{upd}</small><br><code>{cfi[:60]}</code>"


def render_html(snap: dict) -> str:
    body_rows = []
    for r in snap["rows"]:
        body_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(r['title'])}</strong><br><small>{html.escape(r['author'])}</small></td>"
            f"<td>{_fmt_pos(r['abs'])}</td>"
            f"<td>{_fmt_pos(r['calibre'])}</td>"
            f"<td>{html.escape(r['action'] or '')}</td>"
            "</tr>"
        )
    err = f"<p class='err'>Last error: {html.escape(snap['last_error'])}</p>" if snap["last_error"] else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>ABS &harr; Calibre reading positions</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 2rem; color: #222; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f5f5f5; }}
 code {{ font-size: 11px; word-break: break-all; }}
 .err {{ color: #b00020; }}
</style></head><body>
<h1>Reading positions</h1>
<p>Mode: <b>{html.escape(str(snap['mode']))}</b>{' (dry run)' if snap['dry_run'] else ''} &middot;
Last run: {html.escape(snap['last_run'] or 'never')}</p>
{err}
<table>
<tr><th>Book</th><th>Audiobookshelf</th><th>Calibre</th><th>Last action</th></tr>
{''.join(body_rows) or '<tr><td colspan=4>No matched ebooks yet.</td></tr>'}
</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    trigger = threading.Event()

    def _send(self, code: int, ctype: str, payload: str) -> None:
        data = payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/status"):
            self._send(200, "application/json", json.dumps(snapshot(), indent=2))
        elif self.path == "/" or self.path.startswith("/?"):
            self._send(200, "text/html; charset=utf-8", render_html(snapshot()))
        elif self.path == "/healthz":
            self._send(200, "text/plain", "ok")
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/sync":
            Handler.trigger.set()
            self._send(202, "application/json", json.dumps({"queued": True}))
        else:
            self._send(404, "text/plain", "not found")

    def log_message(self, *args) -> None:  # keep stdout quiet
        return


# --------------------------------------------------------------------------- main

def main() -> None:
    cfg = load_config()
    state = State(cfg.state_path)
    abs_client = AbsClient(cfg.abs_url, cfg.abs_token)
    publish(cfg, None, None)

    server = ThreadingHTTPServer(("0.0.0.0", cfg.http_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web UI on :%d, mode=%s, interval=%ss, dry_run=%s",
             cfg.http_port, cfg.mode, cfg.interval, cfg.dry_run)

    once = "--once" in sys.argv
    while True:
        try:
            rows = run_cycle(cfg, abs_client, state)
            publish(cfg, rows, None)
            log.info("Cycle done: %d ebook(s) seen", len(rows))
        except Exception as e:  # keep the service alive; show the error on the page
            log.exception("Sync cycle failed")
            publish(cfg, None, str(e))
        if once:
            return
        Handler.trigger.wait(cfg.interval)
        Handler.trigger.clear()


if __name__ == "__main__":
    main()
