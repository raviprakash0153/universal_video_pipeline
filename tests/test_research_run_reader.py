import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research_run_reader import ResearchRunReadError, _open_readonly, read_research_run


SCHEMA = """
CREATE TABLE research_runs(run_id TEXT, created_at TEXT, source_status_json TEXT);
CREATE TABLE research_topics(topic_row_id TEXT, topic_id TEXT, run_id TEXT, headline TEXT, summary TEXT, category TEXT, relevance TEXT, verification_status TEXT, score REAL, score_json TEXT, snapshot_json TEXT);
CREATE TABLE discovered_articles(run_id TEXT, topic_id TEXT, url TEXT, publisher TEXT, publication_date TEXT, retrieved_at TEXT, article_json TEXT);
CREATE TABLE research_claims(run_id TEXT, topic_id TEXT, claim_index INTEGER, claim TEXT, status TEXT, claim_json TEXT);
CREATE TABLE claim_evidence(run_id TEXT, topic_id TEXT, claim_index INTEGER, evidence_url TEXT);
CREATE TABLE research_source_status(run_id TEXT, source_key TEXT, status_json TEXT);
"""
STAMP = "2026-01-01T00:00:00+00:00"


class ResearchRunReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / "fixture.sqlite3"
        self.db = sqlite3.connect(self.dbpath)
        self.db.executescript(SCHEMA)
        meta = {key: "synthetic" for key in ("configuration", "query", "coverage", "code_version", "source_provenance")}
        self.db.execute("insert into research_runs values(?,?,?)", ("complete", STAMP, json.dumps(meta)))
        snapshot = {"topic_id":"t1", "headline":"Headline", "summary":"Summary", "category":"science", "relevance":"global", "verification_status":"VERIFIED", "score":1.0, "articles":[], "claims":[]}
        self.db.execute("insert into research_topics values(?,?,?,?,?,?,?,?,?,?,?)", ("complete:t1", "t1", "complete", "Headline", "Summary", "science", "global", "VERIFIED", 1.0, "{}", json.dumps(snapshot)))
        self.db.execute("insert into discovered_articles values(?,?,?,?,?,?,?)", ("complete", "t1", "https://example.com/a", "Example", None, STAMP, json.dumps({"url":"https://example.com/a"})))
        self.db.execute("insert into research_claims values(?,?,?,?,?,?)", ("complete", "t1", 0, "Claim", "SUPPORTED", json.dumps({"claim":"Claim", "status":"SUPPORTED"})))
        self.db.execute("insert into claim_evidence values(?,?,?,?)", ("complete", "t1", 0, "https://example.com/e"))
        self.db.execute("insert into research_source_status values(?,?,?)", ("complete", "feed", "{}"))
        self.db.execute("insert into research_runs values(?,?,?)", ("empty", STAMP, json.dumps(meta)))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def logical_dump(self):
        return {name: self.db.execute(f'select * from "{name}" order by rowid').fetchall() for name in
                ("research_runs", "research_topics", "discovered_articles", "research_claims", "claim_evidence", "research_source_status")}

    def test_saved_and_empty_runs_are_read_only_and_completion_unknown(self):
        before = self.logical_dump()
        result = read_research_run(self.dbpath, "complete")
        self.assertTrue(result.valid, result.validation_issues)
        self.assertEqual(result.completion_status, "unknown")
        self.assertEqual(result.saved_content_status, "content_present_completion_indeterminate")
        self.assertEqual((len(result.topics), len(result.articles), len(result.claims), len(result.evidence_links)), (1,1,1,1))
        self.assertNotIn("t1", result.human_annotations)
        empty = read_research_run(self.dbpath, "empty")
        self.assertTrue(empty.valid)
        self.assertEqual(empty.completion_status, "unknown")
        self.assertEqual(empty.saved_content_status, "empty_saved_run")
        self.assertEqual(empty.topics, [])
        self.assertEqual(self.logical_dump(), before)
        ro = _open_readonly(self.dbpath)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("insert into research_runs values('x', ?, '{}')", (STAMP,))
        ro.close()

    def test_paths_with_uri_special_characters(self):
        path = Path(self.tmp.name) / "spaces ? and #.sqlite3"
        con = sqlite3.connect(path); con.executescript(SCHEMA)
        con.execute("insert into research_runs values(?,?,?)", ("empty", STAMP, "{}")); con.commit(); con.close()
        self.assertEqual(read_research_run(path, "empty").saved_content_status, "empty_saved_run")

    def test_duplicate_associations_are_reported(self):
        self.db.execute("insert into discovered_articles values(?,?,?,?,?,?,?)", ("complete", "t1", "https://example.com/a", "Example", None, STAMP, '{"url":"https://example.com/a"}'))
        self.db.execute("insert into research_claims values(?,?,?,?,?,?)", ("complete", "t1", 0, "Other", "SUPPORTED", '{"claim":"Other","status":"SUPPORTED"}'))
        self.db.execute("insert into claim_evidence values(?,?,?,?)", ("complete", "t1", 0, "https://example.com/e")); self.db.commit()
        result = read_research_run(self.dbpath, "complete")
        duplicate_messages = [issue.message for issue in result.validation_issues if issue.code == "duplicate_association"]
        self.assertEqual(len(duplicate_messages), 3)
        self.assertEqual(len(result.claims), 2)

    def test_invalid_url_shapes_and_encoded_url(self):
        invalid = ["https://publisher..invalid/path", "https://bad host/path", "https://example.com:99999/path", "ftp://example.com/path"]
        for url in invalid:
            self.db.execute("update discovered_articles set url=? where run_id='complete'", (url,))
            self.db.commit()
            result = read_research_run(self.dbpath, "complete")
            self.assertIn("invalid_url", {issue.code for issue in result.validation_issues}, url)
        self.db.execute("update discovered_articles set url='https://example.com/a%20b?q=x%2Fy' where run_id='complete'"); self.db.commit()
        self.assertNotIn("invalid_url", {issue.code for issue in read_research_run(self.dbpath, "complete").validation_issues})

    def test_malformed_blob_json_shapes_and_timestamp_z(self):
        self.db.execute("update research_runs set source_status_json=? where run_id='complete'", (sqlite3.Binary(b"\xff"),))
        self.db.execute("update research_topics set score_json='null' where run_id='complete'")
        self.db.execute("update discovered_articles set article_json='[]', retrieved_at='2026-01-01T00:00:00Zjunk' where run_id='complete'")
        self.db.commit()
        result = read_research_run(self.dbpath, "complete")
        codes = {i.code for i in result.validation_issues}
        self.assertTrue({"malformed_json", "invalid_json_shape", "invalid_timestamp"} <= codes)
        self.db.execute("update discovered_articles set retrieved_at='2026-01-01T00:00:00+05:30' where run_id='complete'"); self.db.commit()
        codes = {i.code for i in read_research_run(self.dbpath, "complete").validation_issues}
        self.assertNotIn("invalid_timestamp", codes)

    def test_reads_one_snapshot_while_writer_commits(self):
        path = Path(self.tmp.name) / "snapshot.sqlite3"
        writer = sqlite3.connect(path); writer.executescript(SCHEMA)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("insert into research_runs values(?,?,?)", ("r", STAMP, "{}")); writer.commit()
        reader = _open_readonly(path); reader.execute("BEGIN")
        reader.execute("select name from sqlite_master").fetchall()
        writer.execute("insert into research_topics values(?,?,?,?,?,?,?,?,?,?,?)", ("id", "t", "r", "h", "s", "c", "r", "v", 1, "{}", "{}")); writer.commit()
        # Once its first read established the snapshot, this connection cannot see the later commit.
        self.assertEqual(reader.execute("select * from research_topics where run_id='r'").fetchall(), [])
        reader.rollback(); reader.close(); writer.close()

    def test_missing_db_and_run_do_not_create_files(self):
        missing_dir = Path(self.tmp.name) / "missing"
        with self.assertRaises(ResearchRunReadError):
            read_research_run(missing_dir / "db.sqlite", "x")
        self.assertFalse(missing_dir.exists())
        with self.assertRaises(ResearchRunReadError):
            read_research_run(self.dbpath, "absent")

    def test_missing_tables_and_columns_are_reported(self):
        p = Path(self.tmp.name) / "bad.sqlite"
        con = sqlite3.connect(p); con.execute("create table research_runs(run_id text)"); con.commit(); con.close()
        with self.assertRaisesRegex(ResearchRunReadError, "incomplete"):
            read_research_run(p, "x")

    def test_malformed_json_timestamps_urls_and_snapshot_conflicts(self):
        self.db.execute("update research_topics set snapshot_json='{}'")
        self.db.execute("update research_runs set created_at='bad' where run_id='complete'")
        self.db.execute("update discovered_articles set url='file:///bad', retrieved_at='bad'")
        self.db.execute("update research_claims set claim_json='nope'")
        self.db.commit()
        result = read_research_run(self.dbpath, "complete")
        codes = {i.code for i in result.validation_issues}
        self.assertTrue({"snapshot_conflict", "invalid_timestamp", "invalid_url", "malformed_json"} <= codes)

    def test_orphan_and_duplicate_ids_are_detected(self):
        self.db.execute("insert into research_topics values(?,?,?,?,?,?,?,?,?,?,?)", ("complete:t1", "t1", "complete", "Headline", "Summary", "science", "global", "VERIFIED", 1.0, "{}", "{}"))
        self.db.execute("insert into discovered_articles values(?,?,?,?,?,?,?)", ("complete", "ghost", "https://example.com", "X", None, None, '{}'))
        self.db.execute("insert into claim_evidence values(?,?,?,?)", ("complete", "ghost", 8, "https://example.com"))
        self.db.commit()
        result = read_research_run(self.dbpath, "complete")
        codes = {i.code for i in result.validation_issues}
        self.assertTrue({"duplicate_identifier", "orphan_topic_association", "orphan_evidence_link"} <= codes)

    def test_missing_metadata_is_explicit(self):
        self.db.execute("update research_runs set source_status_json='{}' where run_id='complete'"); self.db.commit()
        result = read_research_run(self.dbpath, "complete")
        missing = [i.message for i in result.validation_issues if i.code == "missing_metadata"]
        self.assertEqual(len(missing), 5)


if __name__ == "__main__":
    unittest.main()
