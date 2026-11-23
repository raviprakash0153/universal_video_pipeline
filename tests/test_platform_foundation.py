import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_foundation import (
    CAPABILITY_REGISTRY, PLATFORMS, PlatformCapability, PlatformOutput,
    build_platform_packages, create_content_package, reject_paid_api,
)
from safety_foundations import (
    enforce_reuse_rights, resolve_local_input, validate_approval, write_approval,
)


class SafetyFoundationTests(unittest.TestCase):
    def test_rights_must_be_explicit_and_eligible(self):
        for state in ("unknown", "prohibited"):
            with self.assertRaises(PermissionError):
                enforce_reuse_rights(state)
        for state in ("owned", "licensed", "permission_granted", "public_domain"):
            enforce_reuse_rights(state)

    def test_local_input_accepts_landscape_portrait_and_rejects_under_720(self):
        def probe(width, height):
            return type("Result", (), {"stdout": json.dumps({"streams": [{"width": width, "height": height}]})})()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mp4"
            path.touch()
            with patch("safety_foundations.subprocess.run", side_effect=[probe(1280, 720), probe(720, 1280), probe(640, 360)]) as run:
                self.assertEqual(resolve_local_input(path)["orientation"], "landscape")
                self.assertEqual(resolve_local_input(path)["orientation"], "portrait")
                with self.assertRaisesRegex(ValueError, "below the 720p minimum"):
                    resolve_local_input(path)
            self.assertEqual(run.call_count, 3)

    def test_approval_binds_media_hash_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "out.mp4"
            approval = Path(directory) / "approval.json"
            media.write_bytes(b"video")
            metadata = {"media_file": str(media), "title": "Approved", "privacy": "private"}
            write_approval(approval, media, metadata, approver="creator", approved_at="2026-11-23T00:00:00Z")
            validate_approval(approval, metadata)
            with self.assertRaises(PermissionError):
                validate_approval(approval, {**metadata, "title": "Changed"})
            media.write_bytes(b"changed")
            with self.assertRaises(PermissionError):
                validate_approval(approval, metadata)


class PlatformFoundationTests(unittest.TestCase):
    def test_registry_has_requested_platforms_only_and_unknown_defaults(self):
        self.assertEqual(set(CAPABILITY_REGISTRY), set(PLATFORMS))
        self.assertNotIn("tiktok", CAPABILITY_REGISTRY)
        self.assertTrue(all(item.official_api == "unknown" for item in CAPABILITY_REGISTRY.values()))

    def test_output_record_contains_all_required_tracking_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "v.mp4"
            media.write_bytes(b"clip")
            record = PlatformOutput.for_media("youtube", media, {"caption": "hello"})
        self.assertEqual(len(record.sha256), 64)
        for field in ("platform", "media_file_path", "sha256", "metadata", "format_validation",
                      "approval_state", "publishing_method", "publishing_status", "platform_post_id",
                      "platform_post_url", "analytics_availability", "analytics_retrieval_status"):
            self.assertTrue(hasattr(record, field))

    def test_one_platform_failure_does_not_stop_other_packages(self):
        def builder(platform):
            if platform == "x":
                raise RuntimeError("bad adapter")
            return {"platform": platform}
        result = build_platform_packages(["youtube", "x", "bluesky"], builder)
        self.assertEqual(result["youtube"]["status"], "ready")
        self.assertEqual(result["x"]["status"], "failed")
        self.assertEqual(result["bluesky"]["status"], "ready")

    def test_package_completeness_and_paid_api_rejection(self):
        package = create_content_package(platform="instagram", vertical_video="v.mp4", caption="caption",
                                         hook="hook", hashtags=["tag"], cover_path="cover.jpg",
                                         manual_instructions="Upload manually")
        self.assertEqual(package["media_variants"]["vertical"], "v.mp4")
        self.assertEqual(package["hashtags"], ["tag"])
        self.assertEqual(package["manual_instructions"], "Upload manually")
        with self.assertRaises(PermissionError):
            reject_paid_api(PlatformCapability("x", access_cost="paid"))
        reject_paid_api(PlatformCapability("youtube", access_cost="free"))


if __name__ == "__main__":
    unittest.main()
