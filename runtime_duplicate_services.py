"""
Duplicate base services for each admin user.

Base services are documents in `services` with no `admin_id` (or admin_id == None).
Each cloned service gets:
  - admin_id (admin user's _id)
  - base_service_id (original service _id)
  - cloned_at (timestamp)
  - created_at / updated_at set to now

Default behavior: apply changes immediately. Use --dry-run to preview only.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from typing import Iterable

from db import db
from service_admin_pricing import apply_admin_pricing_to_offers, normalize_admin_level
from social_boosting_pricing import SOCIAL_BOOSTING_NAME, SOCIAL_BOOSTING_SERVICE_ID

services_col = db["services"]
users_col = db["users"]


def _admin_query(include_blocked: bool) -> dict:
    q = {"role": {"$in": ["admin", "main_admin"]}}
    if not include_blocked:
        q["$or"] = [{"status": "active"}, {"status": {"$exists": False}}]
    return q


def _base_services() -> list[dict]:
    return list(
        services_col.find(
            {
                "$and": [
                    {
                        "$or": [
                            {"admin_id": {"$exists": False}},
                            {"admin_id": None},
                        ]
                    },
                    {"_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID}},
                    {"name": {"$ne": SOCIAL_BOOSTING_NAME}},
                ]
            }
        )
    )


def _dedupe_query(admin_id, base_doc: dict) -> dict:
    q = {"admin_id": admin_id, "base_service_id": base_doc.get("_id")}
    return q


def _loose_dedupe_query(admin_id, base_doc: dict) -> dict:
    q = {"admin_id": admin_id, "name": base_doc.get("name")}
    if base_doc.get("type"):
        q["type"] = base_doc.get("type")
    if base_doc.get("service_network"):
        q["service_network"] = base_doc.get("service_network")
    if base_doc.get("network"):
        q["network"] = base_doc.get("network")
    return q


def _iter_admins(include_blocked: bool) -> Iterable[dict]:
    return users_col.find(_admin_query(include_blocked), {"_id": 1, "username": 1, "role": 1, "status": 1, "admin_level": 1})


def main() -> int:
    parser = argparse.ArgumentParser(description="Duplicate base services per admin.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to the database.")
    parser.add_argument("--include-blocked", action="store_true", help="Include blocked admins.")
    parser.add_argument(
        "--loose-dedupe",
        action="store_true",
        help="Skip if an admin already has a service with the same name/type/network.",
    )
    args = parser.parse_args()

    admins = list(_iter_admins(args.include_blocked))
    base_services = _base_services()

    print(f"Admins found: {len(admins)}")
    print(f"Base services found: {len(base_services)}")
    if not admins or not base_services:
        print("Nothing to do.")
        return 0

    to_insert = []
    skipped = 0

    now = datetime.utcnow()

    for admin in admins:
        admin_id = admin["_id"]
        admin_level = normalize_admin_level(admin.get("admin_level"))
        for base in base_services:
            if services_col.find_one(_dedupe_query(admin_id, base)):
                skipped += 1
                continue
            if args.loose_dedupe and services_col.find_one(_loose_dedupe_query(admin_id, base)):
                skipped += 1
                continue

            new_doc = deepcopy(base)
            new_doc.pop("_id", None)
            new_doc["admin_id"] = admin_id
            new_doc["base_service_id"] = base.get("_id")
            new_doc["cloned_at"] = now
            new_doc["created_at"] = now
            new_doc["updated_at"] = now
            if isinstance(new_doc.get("offers"), list):
                new_doc["offers"] = apply_admin_pricing_to_offers(
                    base.get("offers") or [],
                    new_doc.get("offers") or [],
                    admin_level,
                )
            to_insert.append(new_doc)

    print(f"Planned inserts: {len(to_insert)}")
    print(f"Skipped (already exists): {skipped}")

    if args.dry_run:
        print("Dry run only. Re-run without --dry-run to write changes.")
        return 0

    if to_insert:
        res = services_col.insert_many(to_insert)
        print(f"Inserted: {len(res.inserted_ids)}")
    else:
        print("No inserts required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
