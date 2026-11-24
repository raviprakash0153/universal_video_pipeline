"""Review and decide on individual platform outputs in the local lifecycle DB."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import subprocess
from pathlib import Path

from pipeline_store import (connect, decide_approval, list_outputs, validate_db_approval,
                            registered_media_for_preview, record_output_preview_after_launch,
                            validate_output_for_publishing)


def _media_details(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)], check=True, text=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Unable to validate video with ffprobe: {path}") from exc
    probe = json.loads(result.stdout)
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("File contains no video stream")
    fmt = probe.get("format", {})
    return {"format": fmt.get("format_name", "unknown"),
            "dimensions": f"{video.get('width', '?')}x{video.get('height', '?')}",
            "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
            "subtitles": sum(stream.get("codec_type") == "subtitle" for stream in streams)}


def _open_video(path: Path) -> None:
    system = platform.system()
    command = ["open", str(path)] if system == "Darwin" else ["cmd", "/c", "start", "", str(path)] if system == "Windows" else ["xdg-open", str(path)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review per-platform outputs and record approval decisions")
    parser.add_argument("--database", default=os.environ.get("PIPELINE_DATABASE"), help="SQLite file (or PIPELINE_DATABASE)")
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list", help="List outputs by approval state")
    listing.add_argument("--state", choices=["not_requested", "approved", "rejected", "invalidated"])
    inspect = sub.add_parser("inspect", help="Inspect one output and verify its current approval")
    inspect.add_argument("output_id")
    preview = sub.add_parser("preview", help="Validate and launch a registered output in the local video player")
    preview.add_argument("output_id")
    for name in ("approve", "reject"):
        cmd = sub.add_parser(name, help=f"{name.title()} one platform output")
        cmd.add_argument("output_id")
        cmd.add_argument("--by", default=os.environ.get("PIPELINE_USER"), help="Approver identity (defaults to local account)")
    publish = sub.add_parser("publish", help="Publish one already-approved registered YouTube output")
    publish.add_argument("output_id")
    publish.add_argument("--client-secrets", default=str(Path(__file__).resolve().parent / "client_secret.json"),
                         help="YouTube Desktop OAuth client JSON (same as video_pipeline.py)")
    args = parser.parse_args(argv)
    db = connect(args.database) if args.database else connect()
    try:
        if args.action == "list":
            for row in list_outputs(db, args.state):
                print(f"{row['output_id']}\t{row['platform']}\t{row['approval_state']}\trights={row['rights_state']}\tsha256={row['sha256'] or 'missing'}\t{row['path'] or 'no media'}")
        elif args.action in {"inspect", "preview"}:
            rows = [r for r in list_outputs(db) if r["output_id"] == args.output_id]
            if not rows:
                parser.error(f"Unknown output: {args.output_id}")
            row = rows[0]
            details = {key: row[key] for key in row.keys()}
            details["metadata"] = json.loads(details.pop("metadata_json"))
            if args.action == "preview":
                try:
                    registered = registered_media_for_preview(db, args.output_id)
                    media_path = Path(registered["path"])
                    media = _media_details(media_path)
                    _open_video(media_path)
                    reviewed = record_output_preview_after_launch(db, args.output_id, registered["sha256"])
                except (PermissionError, ValueError, OSError, TypeError, subprocess.SubprocessError) as exc:
                    parser.error(str(exc))
                validation = db.execute("SELECT status,details_json FROM format_validations WHERE output_id=? ORDER BY created_at DESC LIMIT 1", (args.output_id,)).fetchone()
                details.update({"path": reviewed["path"], "media_sha256": reviewed["sha256"],
                                "media": media, "validation": ({"status": validation["status"],
                                "details": json.loads(validation["details_json"])} if validation else {"status": "not recorded"})})
                details["rights_status"] = details["rights_state"]
                print(json.dumps(details, indent=2, ensure_ascii=False))
                print("Local video player launched successfully. This confirms launch only; no playback or viewing was verified. No media was uploaded.")
                return
            try:
                approval = validate_db_approval(db, args.output_id)
                details["approval"] = {"valid": True, "approver": approval["approver"], "approved_at": approval["approved_at"]}
            except PermissionError as exc:
                details["approval"] = {"valid": False, "reason": str(exc)}
            print(json.dumps(details, indent=2, ensure_ascii=False))
        elif args.action == "publish":
            try:
                # Serialize validation and claiming so concurrent CLI calls
                # cannot both upload the same registered output.
                db.execute("BEGIN IMMEDIATE")
                row = validate_output_for_publishing(db, args.output_id)
                metadata = json.loads(row["metadata_json"])
                client_file = Path(args.client_secrets).expanduser()
                if not client_file.is_file():
                    raise PermissionError(f"YouTube OAuth client JSON not found: {client_file}")
                # Mark before network I/O. Any interruption from this point is
                # conservatively uncertain and cannot be blindly retried.
                import uuid
                from datetime import datetime, timezone
                from video_pipeline import upload_youtube
                attempt_id = str(uuid.uuid4())
                db.execute("INSERT INTO publishing_attempts VALUES(?,?,?,?,?,?,?)",
                           (attempt_id, args.output_id, "started", None, None, "{}",
                            datetime.now(timezone.utc).isoformat(timespec="seconds")))
                db.execute("UPDATE platform_outputs SET publishing_status='publishing' WHERE output_id=?",
                           (args.output_id,))
                db.commit()
                try:
                    uploaded = upload_youtube(Path(row["path"] if Path(row["path"]).is_absolute()
                                                  else Path(__file__).resolve().parent / row["path"]),
                                              str(metadata.get("title") or args.output_id)[:100],
                                              str(metadata.get("description") or ""),
                                              str(metadata.get("privacy") or "private"), client_file,
                                              tags=metadata.get("tags") or [],
                                              category_id=str(metadata.get("category_id") or "22"))
                except BaseException as exc:
                    with db:
                        db.execute("UPDATE publishing_attempts SET status='uncertain',details_json=? WHERE attempt_id=?",
                                   (json.dumps({"error_type": type(exc).__name__}), attempt_id))
                        db.execute("UPDATE platform_outputs SET publishing_status='uncertain' WHERE output_id=?",
                                   (args.output_id,))
                    raise PermissionError("Upload outcome is uncertain; inspect YouTube manually before any retry.") from exc
                record = uploaded if isinstance(uploaded, dict) else {}
                if not record.get("video_id"):
                    with db:
                        db.execute("UPDATE publishing_attempts SET status='uncertain',details_json=? WHERE attempt_id=?",
                                   ('{"reason":"platform returned no video id"}', attempt_id))
                        db.execute("UPDATE platform_outputs SET publishing_status='uncertain' WHERE output_id=?",
                                   (args.output_id,))
                    raise PermissionError("Upload outcome is uncertain; the platform returned no video ID.")
                with db:
                    db.execute("UPDATE publishing_attempts SET status='succeeded',platform_post_id=?,platform_post_url=? WHERE attempt_id=?",
                               (record["video_id"], record.get("url"), attempt_id))
                    db.execute("UPDATE platform_outputs SET publishing_status='succeeded',platform_post_id=?,platform_post_url=? WHERE output_id=?",
                               (record["video_id"], record.get("url"), args.output_id))
                print(f"{args.output_id}: published to YouTube ({record.get('url') or record['video_id']})")
            except (PermissionError, KeyError, OSError, ValueError) as exc:
                parser.error(str(exc))
        else:
            approver = args.by or getpass.getuser()
            decide_approval(db, args.output_id, args.action == "approve", approver)
            print(f"{args.output_id}: {args.action} recorded by {approver}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
