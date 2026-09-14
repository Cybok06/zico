from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from bson import ObjectId

from db import db


agent_codes_col = db["agent_codes"]
VALID_AGENT_CODE_STATUSES = {"active", "inactive"}


def generate_unique_agent_code() -> str:
    for _ in range(1000):
        code = str(random.randint(10000, 99999))
        if not agent_codes_col.find_one({"agent_code": code}, {"_id": 1}):
            return code
    raise RuntimeError("Could not generate unique agent code")


def create_agent_code_for_user(user_id: ObjectId, admin_id: Any = None, now: datetime | None = None) -> ObjectId | None:
    if not isinstance(user_id, ObjectId):
        return None

    existing = agent_codes_col.find_one({"user_id": user_id}, {"_id": 1})
    if existing:
        return existing["_id"]

    now = now or datetime.utcnow()
    doc = {
        "user_id": user_id,
        "agent_code": generate_unique_agent_code(),
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    if admin_id:
        doc["admin_id"] = admin_id

    res = agent_codes_col.insert_one(doc)
    agent_codes_col.update_one({"_id": res.inserted_id}, {"$set": {"id": str(res.inserted_id)}})
    return res.inserted_id


def get_agent_code_for_user(user_id: ObjectId) -> dict[str, Any] | None:
    if not isinstance(user_id, ObjectId):
        return None
    doc = agent_codes_col.find_one({"user_id": user_id})
    if doc and not doc.get("status"):
        doc["status"] = "active"
    return doc


def get_or_create_agent_code_for_user(
    user_id: ObjectId,
    admin_id: Any = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    doc = get_agent_code_for_user(user_id)
    if doc:
        if admin_id and not doc.get("admin_id"):
            agent_codes_col.update_one(
                {"_id": doc["_id"]},
                {"$set": {"admin_id": admin_id, "updated_at": now or datetime.utcnow()}},
            )
            doc["admin_id"] = admin_id
        return doc

    inserted_id = create_agent_code_for_user(user_id, admin_id=admin_id, now=now)
    if not inserted_id:
        return None
    return agent_codes_col.find_one({"_id": inserted_id})


def set_agent_code_status_for_user(
    user_id: ObjectId,
    status: str,
    admin_id: Any = None,
    actor_user_id: Any = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not isinstance(user_id, ObjectId):
        return None

    new_status = str(status or "").strip().lower()
    if new_status not in VALID_AGENT_CODE_STATUSES:
        return None

    now = now or datetime.utcnow()
    doc = get_or_create_agent_code_for_user(user_id, admin_id=admin_id, now=now)
    if not doc:
        return None

    updates = {
        "status": new_status,
        "updated_at": now,
        "status_updated_by": actor_user_id or user_id,
    }
    if admin_id and not doc.get("admin_id"):
        updates["admin_id"] = admin_id

    agent_codes_col.update_one({"_id": doc["_id"]}, {"$set": updates})
    doc.update(updates)
    return doc
