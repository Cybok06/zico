from __future__ import annotations

from datetime import datetime

from pymongo import ASCENDING

from db import db
from agent_code_utils import create_agent_code_for_user


users_col = db["users"]
agent_codes_col = db["agent_codes"]


def generate_agent_codes() -> dict[str, int]:
    agent_codes_col.create_index([("agent_code", ASCENDING)], unique=True)
    agent_codes_col.create_index([("user_id", ASCENDING)], unique=True)
    agent_codes_col.create_index([("admin_id", ASCENDING)])
    agent_codes_col.create_index([("status", ASCENDING)])

    now = datetime.utcnow()
    users = list(
        users_col.find(
            {
                "role": {"$in": ["agent", "customer"]},
                "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
            },
            {"_id": 1, "admin_id": 1},
        )
    )

    created = 0
    updated = 0
    skipped = 0

    for user in users:
        existing = agent_codes_col.find_one({"user_id": user["_id"]})
        if existing:
            update = {}
            if not existing.get("id"):
                update["id"] = str(existing["_id"])
            if not existing.get("status"):
                update["status"] = "active"
            if user.get("admin_id") and existing.get("admin_id") != user.get("admin_id"):
                update["admin_id"] = user.get("admin_id")
            if update:
                update["updated_at"] = now
                agent_codes_col.update_one({"_id": existing["_id"]}, {"$set": update})
                updated += 1
            else:
                skipped += 1
            continue

        create_agent_code_for_user(user["_id"], user.get("admin_id"), now)
        created += 1

    return {
        "users_seen": len(users),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total_codes": agent_codes_col.count_documents({}),
    }


if __name__ == "__main__":
    result = generate_agent_codes()
    print("Agent code generation complete")
    for key, value in result.items():
        print(f"{key}: {value}")
