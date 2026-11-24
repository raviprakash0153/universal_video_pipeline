import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline_store import (connect, create_job, create_platform_output, register_asset,
                            record_provenance, set_rights, decide_approval,
                            validate_db_approval, list_outputs, record_output_preview)


class LifecycleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = connect(self.root / "db.sqlite")
        self.media = self.root / "video.mp4"
        self.media.write_bytes(b"original bytes")
        create_job(self.db, "job", "source.mp4", rights_state="unknown")
        record_provenance(self.db, "job", "https://example.test/source", "url", {"title": "source"})
        set_rights(self.db, "job", "licensed", {"evidence": "local license file"})
        self.asset = register_asset(self.db, "job", self.media)
        create_platform_output(self.db, "yt", "job", "youtube", asset_id=self.asset,
                               metadata={"title": "Title", "platform": "youtube"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_provenance_asset_rights_and_per_platform_approval(self):
        self.assertEqual(self.db.execute("select count(*) from source_provenance").fetchone()[0], 1)
        self.assertEqual(self.db.execute("select count(*) from evidence where kind='rights'").fetchone()[0], 1)
        self.assertEqual(len(self.db.execute("select sha256 from media_assets").fetchone()[0]), 64)
        create_platform_output(self.db, "ig", "job", "instagram", asset_id=self.asset,
                               metadata={"title": "Title", "platform": "instagram"})
        record_output_preview(self.db, "yt")
        decide_approval(self.db, "yt", True, "owner")
        self.assertEqual(validate_db_approval(self.db, "yt")["approver"], "owner")
        with self.assertRaises(PermissionError):
            validate_db_approval(self.db, "ig")
        record_output_preview(self.db, "ig")
        decide_approval(self.db, "ig", False, "owner")
        self.assertEqual(self.db.execute("select approved from approval_history where output_id='ig'").fetchone()[0], 0)

    def test_rights_missing_blocks_approval_and_changed_media_invalidates(self):
        set_rights(self.db, "job", "unknown")
        with self.assertRaises(PermissionError):
            decide_approval(self.db, "yt", True, "owner")
        set_rights(self.db, "job", "licensed")
        record_output_preview(self.db, "yt")
        decide_approval(self.db, "yt", True, "owner")
        self.media.write_bytes(b"changed")
        with self.assertRaisesRegex(PermissionError, "invalidated"):
            validate_db_approval(self.db, "yt")
        self.assertEqual(list_outputs(self.db)[0]["approval_state"], "invalidated")

    def test_metadata_change_invalidates_and_rejection_is_audited(self):
        record_output_preview(self.db, "yt")
        decide_approval(self.db, "yt", False, "reviewer")
        self.assertEqual(self.db.execute("select approved from approval_history").fetchone()[0], 0)
        record_output_preview(self.db, "yt")
        decide_approval(self.db, "yt", True, "owner")
        self.db.execute("update platform_outputs set metadata_json=? where output_id='yt'", (json.dumps({"title":"Changed"}),))
        self.db.commit()
        with self.assertRaisesRegex(PermissionError, "metadata changed"):
            validate_db_approval(self.db, "yt")

    def test_migration_and_db_location_are_configurable(self):
        self.assertEqual(self.db.execute("pragma user_version").fetchone()[0], 8)
        self.assertEqual(self.db.execute("select count(*) from schema_migrations").fetchone()[0], 8)
        with tempfile.TemporaryDirectory() as tmp:
            configured = Path(tmp) / "custom" / "state.sqlite"
            db = connect(configured)
            self.assertTrue(configured.exists())
            db.close()

    def test_preview_requires_registered_unchanged_readable_media(self):
        preview = record_output_preview(self.db, "yt")
        self.assertEqual(preview["path"], str(self.media.resolve()))
        self.assertEqual(len(preview["sha256"]), 64)
        self.media.write_bytes(b"changed after preview")
        with self.assertRaisesRegex(PermissionError, "changed"):
            decide_approval(self.db, "yt", True, "owner")

    def test_decision_requires_preview_and_metadata_matches_preview(self):
        with self.assertRaisesRegex(PermissionError, "Preview this exact"):
            decide_approval(self.db, "yt", True, "owner")
        record_output_preview(self.db, "yt")
        self.db.execute("UPDATE platform_outputs SET metadata_json=? WHERE output_id='yt'",
                        (json.dumps({"title": "Changed", "platform": "youtube"}),))
        self.db.commit()
        with self.assertRaisesRegex(PermissionError, "Preview this exact"):
            decide_approval(self.db, "yt", True, "owner")

    def test_missing_media_cannot_be_previewed_or_approved(self):
        self.media.unlink()
        with self.assertRaisesRegex(PermissionError, "missing or unreadable"):
            record_output_preview(self.db, "yt")
        with self.assertRaises(PermissionError):
            decide_approval(self.db, "yt", True, "owner")


if __name__ == "__main__":
    unittest.main()
