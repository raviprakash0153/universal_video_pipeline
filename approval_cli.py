"""Review and decide on individual platform outputs in the local lifecycle DB."""
from __future__ import annotations

import argparse
import getpass
import json
import os

from pipeline_store import connect, decide_approval, list_outputs, validate_db_approval


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review per-platform outputs and record approval decisions")
    parser.add_argument("--database", default=os.environ.get("PIPELINE_DATABASE"), help="SQLite file (or PIPELINE_DATABASE)")
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list", help="List outputs by approval state")
    listing.add_argument("--state", choices=["not_requested", "approved", "rejected", "invalidated"])
    inspect = sub.add_parser("inspect", help="Inspect one output and verify its current approval")
    inspect.add_argument("output_id")
    for name in ("approve", "reject"):
        cmd = sub.add_parser(name, help=f"{name.title()} one platform output")
        cmd.add_argument("output_id")
        cmd.add_argument("--by", default=os.environ.get("PIPELINE_USER"), help="Approver identity (defaults to local account)")
    args = parser.parse_args(argv)
    db = connect(args.database) if args.database else connect()
    try:
        if args.action == "list":
            for row in list_outputs(db, args.state):
                print(f"{row['output_id']}\t{row['platform']}\t{row['approval_state']}\trights={row['rights_state']}\tsha256={row['sha256'] or 'missing'}\t{row['path'] or 'no media'}")
        elif args.action == "inspect":
            rows = [r for r in list_outputs(db) if r["output_id"] == args.output_id]
            if not rows:
                parser.error(f"Unknown output: {args.output_id}")
            row = rows[0]
            details = {key: row[key] for key in row.keys()}
            details["metadata"] = json.loads(details.pop("metadata_json"))
            try:
                approval = validate_db_approval(db, args.output_id)
                details["approval"] = {"valid": True, "approver": approval["approver"], "approved_at": approval["approved_at"]}
            except PermissionError as exc:
                details["approval"] = {"valid": False, "reason": str(exc)}
            print(json.dumps(details, indent=2, ensure_ascii=False))
        else:
            approver = args.by or getpass.getuser()
            decide_approval(db, args.output_id, args.action == "approve", approver)
            print(f"{args.output_id}: {args.action} recorded by {approver}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
