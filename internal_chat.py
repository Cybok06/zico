from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from flask import Blueprint, jsonify, render_template, request, session

from db import db


internal_chat_bp = Blueprint("internal_chat", __name__, url_prefix="/api/internal-chat")

users_col = db["users"]
messages_col = db["internal_chat_messages"]
presence_col = db["internal_chat_presence"]

AGENT_ROLES = {"agent", "customer"}
SUB_ADMIN_ROLES = {"admin", "professional_admin", "super_admin", "superadmin"}
CHAT_ROLES = {"main_admin", "agent", "customer"} | SUB_ADMIN_ROLES
ADMIN_ROLES = {"main_admin"} | SUB_ADMIN_ROLES
ONLINE_WINDOW_SECONDS = 90
CONTACT_LIMIT = 300


def _oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _role(value: Any = None) -> str:
    return (value if value is not None else session.get("role") or "").strip().lower()


def _current_user() -> Optional[dict]:
    user_id = _oid(session.get("user_id"))
    if not user_id or _role() not in CHAT_ROLES:
        return None
    return users_col.find_one(
        {"_id": user_id},
        {"username": 1, "first_name": 1, "last_name": 1, "business_name": 1, "role": 1, "admin_id": 1, "status": 1},
    )


def _display_name(user: dict | None) -> str:
    if not user:
        return "Unknown"
    full_name = " ".join([user.get("first_name") or "", user.get("last_name") or ""]).strip()
    return user.get("business_name") or full_name or user.get("username") or "User"


def _label_for_role(role: str) -> str:
    if role == "main_admin":
        return "Main Admin"
    if role in SUB_ADMIN_ROLES:
        return "Admin"
    if role == "agent":
        return "Agent"
    return "Customer"


def _user_payload(user: dict, presence: dict | None = None) -> dict:
    role = _role(user.get("role"))
    presence = presence or _presence_payload(user.get("_id"))
    return {
        "id": str(user.get("_id")),
        "name": _display_name(user),
        "username": user.get("username") or "",
        "role": role,
        "role_label": _label_for_role(role),
        "online": presence["online"],
        "last_seen": presence["last_seen"],
        "last_seen_label": presence["last_seen_label"],
    }


def _presence_payload(user_id: Any) -> dict:
    oid = _oid(user_id)
    if not oid:
        return {"online": False, "last_seen": "", "last_seen_label": "Offline"}
    doc = presence_col.find_one({"user_id": oid}, {"last_seen": 1}) or {}
    last_seen = doc.get("last_seen")
    if not isinstance(last_seen, datetime):
        return {"online": False, "last_seen": "", "last_seen_label": "Offline"}

    now = datetime.utcnow()
    seconds = max(0, int((now - last_seen).total_seconds()))
    online = seconds <= ONLINE_WINDOW_SECONDS
    if online:
        label = "Online"
    elif seconds < 60:
        label = "Last seen just now"
    elif seconds < 3600:
        mins = max(1, seconds // 60)
        label = f"Last seen {mins} min ago"
    elif seconds < 86400:
        hours = max(1, seconds // 3600)
        label = f"Last seen {hours} hr ago"
    else:
        label = last_seen.strftime("Last seen %b %d, %I:%M %p")

    return {
        "online": online,
        "last_seen": last_seen.isoformat() + "Z",
        "last_seen_label": label,
    }


def _presence_map(user_ids: list[ObjectId]) -> dict[str, dict]:
    if not user_ids:
        return {}
    out: dict[str, dict] = {}
    now = datetime.utcnow()
    try:
        for doc in presence_col.find({"user_id": {"$in": user_ids}}, {"user_id": 1, "last_seen": 1}):
            uid = doc.get("user_id")
            last_seen = doc.get("last_seen")
            if not isinstance(uid, ObjectId) or not isinstance(last_seen, datetime):
                continue
            seconds = max(0, int((now - last_seen).total_seconds()))
            online = seconds <= ONLINE_WINDOW_SECONDS
            if online:
                label = "Online"
            elif seconds < 60:
                label = "Last seen just now"
            elif seconds < 3600:
                label = f"Last seen {max(1, seconds // 60)} min ago"
            elif seconds < 86400:
                label = f"Last seen {max(1, seconds // 3600)} hr ago"
            else:
                label = last_seen.strftime("Last seen %b %d, %I:%M %p")
            out[str(uid)] = {
                "online": online,
                "last_seen": last_seen.isoformat() + "Z",
                "last_seen_label": label,
            }
    except Exception:
        pass
    return out


def _touch_presence(user_id: Any) -> None:
    oid = _oid(user_id)
    if not oid:
        return
    now = datetime.utcnow()
    try:
        presence_col.update_one(
            {"user_id": oid},
            {
                "$set": {"last_seen": now, "updated_at": now},
                "$setOnInsert": {"user_id": oid, "created_at": now},
            },
            upsert=True,
        )
    except Exception:
        pass


def _active_user_query(extra: dict) -> dict:
    return {
        **extra,
        "$and": [
            {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
            {"$or": [{"status": {"$exists": False}}, {"status": {"$ne": "deleted"}}]},
        ],
    }


def _main_admin_contacts(current: dict) -> list[dict]:
    # Main admin may have thousands of agents/customers in the platform.
    # Keep this fast: always include sub-admins, then include agents/customers
    # only when there is an existing/recent conversation with them.
    recent_ids = _recent_chat_contact_ids(current["_id"], limit=CONTACT_LIMIT)
    q = _active_user_query(
        {
            "$or": [
                {"role": {"$in": list(SUB_ADMIN_ROLES)}},
                {"_id": {"$in": recent_ids}, "role": {"$in": list(AGENT_ROLES)}},
            ],
            "_id": {"$ne": current["_id"]},
        }
    )
    return list(
        users_col.find(q, {"username": 1, "first_name": 1, "last_name": 1, "business_name": 1, "role": 1})
        .sort("username", 1)
        .limit(CONTACT_LIMIT)
    )


def _admin_contacts(current: dict) -> list[dict]:
    contacts: list[dict] = []
    main_admins = users_col.find(
        _active_user_query({"role": "main_admin", "_id": {"$ne": current["_id"]}}),
        {"username": 1, "first_name": 1, "last_name": 1, "business_name": 1, "role": 1},
    ).sort("username", 1)
    contacts.extend(main_admins)

    agents = users_col.find(
        _active_user_query({"role": {"$in": list(AGENT_ROLES)}, "admin_id": current["_id"]}),
        {"username": 1, "first_name": 1, "last_name": 1, "business_name": 1, "role": 1},
    ).sort("username", 1).limit(CONTACT_LIMIT)
    contacts.extend(agents)
    return contacts


def _agent_contacts(current: dict) -> list[dict]:
    admin_id = _oid(current.get("admin_id"))
    if not admin_id:
        return []
    admin = users_col.find_one(
        _active_user_query({"_id": admin_id, "role": {"$in": list(ADMIN_ROLES)}}),
        {"username": 1, "first_name": 1, "last_name": 1, "business_name": 1, "role": 1},
    )
    return [admin] if admin else []


def _allowed_contacts(current: dict) -> list[dict]:
    role = _role(current.get("role"))
    if role == "main_admin":
        return _main_admin_contacts(current)
    if role in SUB_ADMIN_ROLES:
        return _admin_contacts(current)
    if role in AGENT_ROLES:
        return _agent_contacts(current)
    return []


def _can_message(current: dict, other: dict | None) -> bool:
    if not current or not other:
        return False
    current_id = current.get("_id")
    other_id = other.get("_id")
    if not current_id or not other_id or current_id == other_id:
        return False
    current_role = _role(current.get("role"))
    other_role = _role(other.get("role"))

    if current_role == "main_admin":
        return other_role in SUB_ADMIN_ROLES or other_role in AGENT_ROLES
    if current_role in SUB_ADMIN_ROLES:
        if other_role == "main_admin":
            return True
        if other_role in AGENT_ROLES:
            return _oid(other.get("admin_id")) == current_id
    if current_role in AGENT_ROLES:
        return other_role in SUB_ADMIN_ROLES and _oid(current.get("admin_id")) == other_id
    return False


def _conversation_query(a: ObjectId, b: ObjectId) -> dict:
    return {
        "$or": [
            {"sender_id": a, "recipient_id": b},
            {"sender_id": b, "recipient_id": a},
        ]
    }


def _recent_chat_contact_ids(current_id: ObjectId, limit: int = CONTACT_LIMIT) -> list[ObjectId]:
    ids: list[ObjectId] = []
    seen = set()
    try:
        cursor = messages_col.find(
            {"$or": [{"sender_id": current_id}, {"recipient_id": current_id}]},
            {"sender_id": 1, "recipient_id": 1},
        ).sort("created_at", -1).limit(int(limit))
        for msg in cursor:
            other = msg.get("recipient_id") if msg.get("sender_id") == current_id else msg.get("sender_id")
            if isinstance(other, ObjectId) and other != current_id and other not in seen:
                seen.add(other)
                ids.append(other)
    except Exception:
        pass
    return ids


def _conversation_key(a: ObjectId, b: ObjectId) -> str:
    return ":".join(sorted([str(a), str(b)]))


def _latest_messages_map(current_id: ObjectId, contact_ids: list[ObjectId]) -> dict[str, dict]:
    if not contact_ids:
        return {}
    latest: dict[str, dict] = {}
    try:
        rows = messages_col.find(
            {
                "$or": [
                    {"sender_id": current_id, "recipient_id": {"$in": contact_ids}},
                    {"recipient_id": current_id, "sender_id": {"$in": contact_ids}},
                ]
            },
            {"sender_id": 1, "recipient_id": 1, "body": 1, "created_at": 1},
        ).sort("created_at", -1).limit(max(CONTACT_LIMIT, len(contact_ids) * 3))
        for row in rows:
            other = row.get("recipient_id") if row.get("sender_id") == current_id else row.get("sender_id")
            if not isinstance(other, ObjectId):
                continue
            key = _conversation_key(current_id, other)
            if key not in latest:
                latest[key] = row
    except Exception:
        pass
    return latest


def _unread_counts_map(current_id: ObjectId, contact_ids: list[ObjectId]) -> dict[str, int]:
    if not contact_ids:
        return {}
    try:
        rows = messages_col.aggregate(
            [
                {
                    "$match": {
                        "sender_id": {"$in": contact_ids},
                        "recipient_id": current_id,
                        "read_by": {"$ne": current_id},
                    }
                },
                {"$group": {"_id": "$sender_id", "count": {"$sum": 1}}},
            ]
        )
        return {str(row.get("_id")): int(row.get("count") or 0) for row in rows if row.get("_id")}
    except Exception:
        return {}


def _message_payload(message: dict, current_id: ObjectId) -> dict:
    created = message.get("created_at")
    return {
        "id": str(message.get("_id")),
        "body": message.get("body") or "",
        "mine": message.get("sender_id") == current_id,
        "sender_id": str(message.get("sender_id") or ""),
        "recipient_id": str(message.get("recipient_id") or ""),
        "created_at": created.isoformat() + "Z" if isinstance(created, datetime) else "",
        "created_label": created.strftime("%b %d, %I:%M %p") if isinstance(created, datetime) else "",
    }


def _last_message_for(current_id: ObjectId, other_id: ObjectId) -> dict | None:
    return messages_col.find_one(_conversation_query(current_id, other_id), sort=[("created_at", -1)])


def _unread_count_from(current_id: ObjectId, other_id: ObjectId) -> int:
    return messages_col.count_documents(
        {
            "sender_id": other_id,
            "recipient_id": current_id,
            "read_by": {"$ne": current_id},
        }
    )


@internal_chat_bp.route("/contacts")
def contacts():
    current = _current_user()
    if not current:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    _touch_presence(current["_id"])

    try:
        current_id = current["_id"]
        allowed = _allowed_contacts(current)
        contact_ids = [u["_id"] for u in allowed if isinstance(u.get("_id"), ObjectId)]
        latest_map = _latest_messages_map(current_id, contact_ids)
        unread_map = _unread_counts_map(current_id, contact_ids)
        presence_by_user = _presence_map(contact_ids)
        items = []
        for user in allowed:
            if not user or not user.get("_id"):
                continue
            payload = _user_payload(user, presence_by_user.get(str(user["_id"])))
            last = latest_map.get(_conversation_key(current_id, user["_id"]))
            payload["last_message"] = (last or {}).get("body") or ""
            payload["last_at"] = ""
            payload["_sort_at"] = 0.0
            if last and isinstance(last.get("created_at"), datetime):
                payload["last_at"] = last["created_at"].strftime("%b %d, %I:%M %p")
                payload["_sort_at"] = last["created_at"].timestamp()
            payload["unread"] = unread_map.get(str(user["_id"]), 0)
            items.append(payload)

        items.sort(key=lambda item: (item.get("unread", 0) > 0, item.get("_sort_at") or 0, item.get("name") or ""), reverse=True)
        for item in items:
            item.pop("_sort_at", None)
        return jsonify({"success": True, "contacts": items, "unread_total": sum(i["unread"] for i in items)})
    except Exception:
        return jsonify({"success": False, "message": "Could not load conversations"}), 500


@internal_chat_bp.route("/messages/<contact_id>")
def messages(contact_id: str):
    current = _current_user()
    other_id = _oid(contact_id)
    other = users_col.find_one({"_id": other_id}) if other_id else None
    if not current:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    if not _can_message(current, other):
        return jsonify({"success": False, "message": "Contact not available"}), 403

    current_id = current["_id"]
    _touch_presence(current_id)
    messages_col.update_many(
        {"sender_id": other_id, "recipient_id": current_id, "read_by": {"$ne": current_id}},
        {"$addToSet": {"read_by": current_id}, "$set": {"read_at": datetime.utcnow()}},
    )
    rows = list(messages_col.find(_conversation_query(current_id, other_id)).sort("created_at", -1).limit(100))
    rows.reverse()
    return jsonify(
        {
            "success": True,
            "contact": _user_payload(other),
            "messages": [_message_payload(row, current_id) for row in rows],
        }
    )


@internal_chat_bp.route("/send", methods=["POST"])
def send():
    current = _current_user()
    payload = request.get_json(silent=True) or {}
    other_id = _oid(payload.get("recipient_id"))
    body = (payload.get("body") or "").strip()
    other = users_col.find_one({"_id": other_id}) if other_id else None

    if not current:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    if not body:
        return jsonify({"success": False, "message": "Type a message first"}), 400
    if len(body) > 2000:
        return jsonify({"success": False, "message": "Message is too long"}), 400
    if not _can_message(current, other):
        return jsonify({"success": False, "message": "Contact not available"}), 403

    now = datetime.utcnow()
    _touch_presence(current["_id"])
    doc = {
        "sender_id": current["_id"],
        "sender_role": _role(current.get("role")),
        "recipient_id": other["_id"],
        "recipient_role": _role(other.get("role")),
        "body": body,
        "read_by": [current["_id"]],
        "created_at": now,
        "updated_at": now,
    }
    res = messages_col.insert_one(doc)
    doc["_id"] = res.inserted_id
    return jsonify({"success": True, "message": _message_payload(doc, current["_id"])})


@internal_chat_bp.route("/unread")
def unread():
    current = _current_user()
    if not current:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    _touch_presence(current["_id"])
    count = messages_col.count_documents({"recipient_id": current["_id"], "read_by": {"$ne": current["_id"]}})
    return jsonify({"success": True, "unread_total": count})


@internal_chat_bp.route("/presence", methods=["POST"])
def presence():
    current = _current_user()
    if not current:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    _touch_presence(current["_id"])
    payload = _presence_payload(current["_id"])
    return jsonify({"success": True, **payload})


@internal_chat_bp.route("/widget")
def widget():
    current = _current_user()
    if not current:
        return ("", 204)
    return render_template("internal_chat_widget.html")
