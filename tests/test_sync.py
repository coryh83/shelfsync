import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import sync  # noqa: E402
from sync import Calibre, Config, Pos, State, decide, match_item, build_index, normalize_isbn, sync_pair  # noqa: E402


def make_calibre_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, path TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
        CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
        CREATE TABLE last_read_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL,
            format TEXT NOT NULL COLLATE NOCASE, user TEXT NOT NULL, device TEXT NOT NULL,
            cfi TEXT NOT NULL, epoch REAL NOT NULL, pos_frac REAL NOT NULL DEFAULT 0,
            UNIQUE(book, format, user, device));
        INSERT INTO books VALUES (1, 'Dune', 'Dune', ''), (2, 'Neuromancer', 'Neuromancer', '');
        INSERT INTO authors VALUES (1, 'Frank Herbert'), (2, 'William Gibson');
        INSERT INTO books_authors_link VALUES (1, 1, 1), (2, 2, 2);
        INSERT INTO identifiers VALUES (1, 2, 'isbn', '978-0-441-56956-4');
    """)
    con.commit()
    con.close()


class IsbnTests(unittest.TestCase):
    def test_isbn10_converts_to_13(self):
        self.assertEqual(normalize_isbn("0-306-40615-2"), "9780306406157")

    def test_isbn13_hyphens_removed(self):
        self.assertEqual(normalize_isbn("978-0-306-40615-7"), "9780306406157")

    def test_garbage(self):
        self.assertIsNone(normalize_isbn("not an isbn"))


class DecideTests(unittest.TestCase):
    def a(self, cfi, ts=0):
        return Pos(cfi, 0.5, ts)

    def test_no_change(self):
        self.assertIsNone(decide(self.a("x", 10), self.a("y", 5), "x", "y", "both"))

    def test_only_abs_moved(self):
        d = decide(self.a("new", 10), self.a("old", 5), "old", "old", "both")
        self.assertEqual(d[0], "abs_to_calibre")

    def test_only_calibre_moved(self):
        d = decide(self.a("old", 10), self.a("new", 5), "old", "old", "both")
        self.assertEqual(d[0], "calibre_to_abs")

    def test_both_moved_newest_wins(self):
        self.assertEqual(decide(self.a("A", 20), self.a("C", 5), "o", "o", "both")[0], "abs_to_calibre")
        self.assertEqual(decide(self.a("A", 5), self.a("C", 20), "o", "o", "both")[0], "calibre_to_abs")

    def test_first_run_both_present(self):
        d = decide(self.a("A", 20), self.a("C", 5), None, None, "both")
        self.assertEqual(d[0], "abs_to_calibre")

    def test_stale_abs_after_push_is_not_a_change(self):
        # We pushed Calibre's newer position to ABS, but ABS still reports the old one.
        # Calibre (last seen = the position we pushed) is unchanged, so nothing should move back.
        self.assertIsNone(decide(self.a("old", 1), self.a("new", 9), "old", "new", "both"))

    def test_one_way_modes_ignore_other_side(self):
        self.assertIsNone(decide(self.a("old", 1), self.a("new", 9), "old", "old", "abs_to_calibre"))
        self.assertIsNone(decide(self.a("new", 9), self.a("old", 1), "old", "old", "calibre_to_abs"))
        self.assertEqual(decide(self.a("new", 1), self.a("old", 9), "old", "old", "abs_to_calibre")[0],
                         "abs_to_calibre")


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metadata.db"
        make_calibre_db(self.db)
        with Calibre(self.db) as cal:
            self.books = cal.books()
        self.by_isbn, self.by_title = build_index(self.books)

    def tearDown(self):
        self.tmp.cleanup()

    def test_match_by_isbn(self):
        b = match_item("Anything", "", "9780441569564", self.by_isbn, self.by_title)
        self.assertEqual(b.title, "Neuromancer")

    def test_match_by_title_and_author_any_order(self):
        b = match_item("Dune", "Herbert, Frank", None, self.by_isbn, self.by_title)
        self.assertEqual(b.id, 1)

    def test_no_match_wrong_author(self):
        self.assertIsNone(match_item("Dune", "Someone Else", None, self.by_isbn, self.by_title))


class CalibreRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metadata.db"
        make_calibre_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_insert_then_update(self):
        with Calibre(self.db) as cal:
            self.assertEqual(cal.latest_positions(), {})
            cal.write_position(1, "epubcfi(/6/2)", 0.25, "local", "local")
            cal.write_position(1, "epubcfi(/6/8)", 0.5, "local", "local")
        with Calibre(self.db) as cal:
            pos = cal.latest_positions()[1]
        self.assertEqual(pos.cfi, "epubcfi(/6/8)")
        self.assertAlmostEqual(pos.fraction, 0.5)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM last_read_positions").fetchone()[0], 1)
        con.close()

    def test_newest_row_across_devices_is_read(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO last_read_positions (book, format, user, device, cfi, epoch, pos_frac) "
                    "VALUES (1, 'EPUB', 'kobo', 'kobo', 'old', 100, 0.1)")
        con.execute("INSERT INTO last_read_positions (book, format, user, device, cfi, epoch, pos_frac) "
                    "VALUES (1, 'EPUB', 'web', 'web', 'new', 200, 0.2)")
        con.commit()
        con.close()
        with Calibre(self.db) as cal:
            self.assertEqual(cal.latest_positions()[1].cfi, "new")


class FakeAbs:
    def __init__(self):
        self.calls = []

    def set_ebook_progress(self, item_id, cfi, fraction):
        self.calls.append((item_id, cfi, fraction))


class SyncPairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metadata.db"
        make_calibre_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(abs_url="x", abs_token="x", calibre_db=self.db, abs_library_ids=[],
                    calibre_user="local", calibre_device="local", mode="both", interval=1,
                    state_path=Path(self.tmp.name) / "state.json", http_port=0, dry_run=False)
        base.update(kw)
        return Config(**base)

    def test_abs_progress_is_written_to_calibre_and_remembered(self):
        cfg, abs_fake = self.cfg(), FakeAbs()
        state = State(cfg.state_path)
        with Calibre(self.db) as cal:
            action = sync_pair(cfg, abs_fake, cal, state, "li_1", 1,
                               Pos("epubcfi(/6/4)", 0.3, 100), Pos(None, None, 0))
            self.assertEqual(action, "synced ABS -> Calibre")
            self.assertEqual(cal.latest_positions()[1].cfi, "epubcfi(/6/4)")
        self.assertEqual(abs_fake.calls, [])
        state.save()
        # second run with nothing new must not write anything
        state2 = State(cfg.state_path)
        with Calibre(self.db) as cal:
            action = sync_pair(cfg, abs_fake, cal, state2, "li_1", 1,
                               Pos("epubcfi(/6/4)", 0.3, 100), Pos("epubcfi(/6/4)", 0.3, 150))
        self.assertEqual(action, "in sync")

    def test_calibre_progress_is_pushed_to_abs(self):
        cfg, abs_fake = self.cfg(), FakeAbs()
        state = State(cfg.state_path)
        state.pairs["li_1:1"] = {"abs_cfi": "epubcfi(/6/2)", "cal_cfi": "epubcfi(/6/2)"}
        with Calibre(self.db) as cal:
            action = sync_pair(cfg, abs_fake, cal, state, "li_1", 1,
                               Pos("epubcfi(/6/2)", 0.1, 10), Pos("epubcfi(/6/9)", 0.7, 50))
        self.assertEqual(action, "synced Calibre -> ABS")
        self.assertEqual(abs_fake.calls, [("li_1", "epubcfi(/6/9)", 0.7)])

    def test_no_ping_pong_when_abs_reports_stale_position(self):
        cfg, abs_fake = self.cfg(), FakeAbs()
        state = State(cfg.state_path)
        state.pairs["li_1:1"] = {"abs_cfi": "epubcfi(/6/2)", "cal_cfi": "epubcfi(/6/2)"}
        with Calibre(self.db) as cal:
            cal.write_position(1, "epubcfi(/6/9)", 0.7, "local", "local")
            # cycle 1: Calibre moved, ABS (stale) still at /6/2 -> push to ABS
            cal_pos = cal.latest_positions()[1]
            self.assertEqual(sync_pair(cfg, abs_fake, cal, state, "li_1", 1,
                                       Pos("epubcfi(/6/2)", 0.1, 10), cal_pos), "synced Calibre -> ABS")
            # cycles 2 and 3: ABS still reports /6/2 and Calibre stays at /6/9 -> nothing may be written
            for _ in range(2):
                cal_pos = cal.latest_positions()[1]
                self.assertEqual(sync_pair(cfg, abs_fake, cal, state, "li_1", 1,
                                           Pos("epubcfi(/6/2)", 0.1, 10), cal_pos), "in sync")
            self.assertEqual(cal.latest_positions()[1].cfi, "epubcfi(/6/9)")
        self.assertEqual(len(abs_fake.calls), 1)

    def test_dry_run_writes_nothing(self):
        cfg, abs_fake = self.cfg(dry_run=True), FakeAbs()
        state = State(cfg.state_path)
        with Calibre(self.db) as cal:
            action = sync_pair(cfg, abs_fake, cal, state, "li_1", 1,
                               Pos("epubcfi(/6/4)", 0.3, 100), Pos(None, None, 0))
            self.assertEqual(action, "would sync ABS -> Calibre")
            self.assertIsNone(cal.latest_positions().get(1))
        self.assertEqual(state.pairs, {})


if __name__ == "__main__":
    unittest.main()
