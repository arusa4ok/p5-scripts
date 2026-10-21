#!/usr/bin/env python3
"""Unit tests for thread.py (cc-stack spec 018). Run: python3 -m unittest scripts/p5/test_thread.py"""
import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("thread_cli", os.path.join(HERE, "thread.py"))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

ROW = "- [demo] id: `ot-a` · **Alpha thing** · status: `waiting` · waiting-on: anton · next: ask · since: `2026-10-01` · updated: `2026-10-02`"


def run(argv):
    with redirect_stdout(io.StringIO()):
        T.main(argv)


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        T.MEM, T.LEDGER = self.d, f"{self.d}/open-threads.md"
        T.LEGACY, T.LOG, T.TASKS = f"{self.d}/legacy.md", f"{self.d}/log.jsonl", f"{self.d}/tasks"
        self.write([ROW])

    def write(self, rows):
        T.write_lines(["# ledger", "", T.OPEN_H] + rows)

    def test_parse_title_without_bold_or_id(self):
        r = T.parse("- [demo] plain title here · status: `open` · next: go")
        self.assertEqual(r["title"], "plain title here")
        self.assertIsNone(r["id"])

    def test_set_field_roundtrip_and_delimiter_sanitised(self):
        line = T.set_field(ROW, "next", "do a · then b", False)
        r = T.parse(line)
        self.assertEqual(r["next"], "do a / then b")
        self.assertEqual(T.parse(T.set_field(line, "next", "x", False))["next"], "x")
        self.assertEqual(r["since_d"].isoformat(), "2026-10-01")

    def test_ready_block_caps_index_without_dropping_it(self):
        rows = [f"- [demo] id: `ot-{i}` · **Row number {i} with a fairly long title to fill the index** · status: `open` · next-resurface: `2030-01-01` · updated: `2026-10-01`" for i in range(40)]
        self.write(rows)
        text, n = T.ready_block("demo")
        self.assertLessEqual(len(text), T.BLOCK_CAP)
        self.assertIn("INDEX (40)", text)
        self.assertIn(f"+{40 - T.INDEX_MAX} more", text)
        self.assertEqual(n["index"], 40)

    def test_close_archives_then_removes(self):
        run(["close", "ot-a", "--outcome", "done"])
        self.assertNotIn("ot-a", open(T.LEDGER).read())
        arch = open(T.archive_path()).read()
        self.assertIn("ot-a", arch)
        self.assertIn("## Closed", arch)
        self.assertTrue(os.path.exists(T.LEDGER + ".bak"))

    def test_defer_into_the_past_is_refused(self):
        with self.assertRaises(SystemExit):
            run(["defer", "ot-a", "--until", "2020-01-01"])

    def test_waiting_row_needs_waiting_on_and_next(self):
        with self.assertRaises(SystemExit):
            run(["add", "--project", "demo", "--title", "B", "--status", "waiting"])

    def test_update_keeps_since_until_the_status_changes(self):
        run(["update", "ot-a", "--note", "ping"])
        r = T.rows()[0][0]
        self.assertEqual(r["since"], "2026-10-01")
        self.assertEqual(r["updated"], T.TODAY.isoformat())
        run(["update", "ot-a", "--status", "in-work"])
        self.assertEqual(T.rows()[0][0]["since"], T.TODAY.isoformat())

    def test_duplicate_id_is_refused(self):
        self.write([ROW, ROW])
        with self.assertRaises(SystemExit):
            T.find("ot-a")

    def test_migrate_refuses_when_legacy_exists(self):
        open(T.LEGACY, "w").write("x")
        with self.assertRaises(SystemExit):
            run(["migrate-legacy", "--apply"])
        self.assertEqual(open(T.LEGACY).read(), "x")


if __name__ == "__main__":
    unittest.main()
