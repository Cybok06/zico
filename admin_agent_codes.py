from __future__ import annotations

import math
import re
from datetime import datetime
from urllib.parse import urlencode

from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from db import db
from tenant import current_admin_id_from_session, is_admin_role


admin_agent_codes_bp = Blueprint("admin_agent_codes", __name__)

agent_codes_col = db["agent_codes"]
users_col = db["users"]


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _require_admin() -> bool:
    return is_admin_role(session.get("role"))


def _to_oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _visible_agent_user_query(q: str = "") -> dict:
    conditions = [
        {"role": {"$in": ["agent", "customer"]}},
        {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
    ]

    admin_oid = current_admin_id_from_session(session)
    if not _is_main_admin():
        conditions.append({"admin_id": admin_oid})

    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        conditions.append(
            {
                "$or": [
                    {"first_name": rx},
                    {"last_name": rx},
                    {"username": rx},
                    {"phone": rx},
                    {"email": rx},
                    {"business_name": rx},
                ]
            }
        )

    return {"$and": conditions}


def _visible_agent_user_ids(q: str = "") -> list[ObjectId]:
    return [u["_id"] for u in users_col.find(_visible_agent_user_query(q), {"_id": 1})]


@admin_agent_codes_bp.route("/admin/agent-codes")
def admin_agent_codes():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 50

    visible_user_ids = _visible_agent_user_ids()

    code_filters = [{"user_id": {"$in": visible_user_ids}}]
    if status in {"active", "inactive"}:
        code_filters.append({"status": status})
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        matching_user_ids = [
            u["_id"]
            for u in users_col.find(_visible_agent_user_query(q), {"_id": 1}).limit(500)
        ]
        code_filters.append(
            {
                "$or": [
                    {"agent_code": rx},
                    {"id": rx},
                    {"user_id": {"$in": matching_user_ids}},
                ]
            }
        )

    query = {"$and": code_filters}
    total = agent_codes_col.count_documents(query)
    total_pages = max(math.ceil(total / per_page), 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    rows = list(
        agent_codes_col.find(query)
        .sort([("created_at", -1), ("agent_code", 1)])
        .skip(skip)
        .limit(per_page)
    )

    user_ids = [_to_oid(row.get("user_id")) for row in rows]
    user_ids = [uid for uid in user_ids if uid]
    users = {
        u["_id"]: u
        for u in users_col.find(
            {"_id": {"$in": user_ids}},
            {
                "first_name": 1,
                "last_name": 1,
                "username": 1,
                "phone": 1,
                "email": 1,
                "business_name": 1,
                "stage_label": 1,
                "status": 1,
                "admin_id": 1,
            },
        )
    }

    admin_ids = list({u.get("admin_id") for u in users.values() if u.get("admin_id")})
    admins = {
        a["_id"]: a
        for a in users_col.find(
            {"_id": {"$in": admin_ids}},
            {"first_name": 1, "last_name": 1, "username": 1, "business_name": 1},
        )
    } if admin_ids else {}

    for row in rows:
        user = users.get(_to_oid(row.get("user_id"))) or {}
        row["_user"] = user
        row["_admin"] = admins.get(user.get("admin_id")) or {}

    visible_active_query = {
        "$and": [
            {"user_id": {"$in": visible_user_ids}},
            {"status": "active"},
        ]
    }
    visible_inactive_query = {
        "$and": [
            {"user_id": {"$in": visible_user_ids}},
            {"status": "inactive"},
        ]
    }

    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)

    return render_template(
        "admin_agent_codes.html",
        rows=rows,
        q=q,
        status=status,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        total_active=agent_codes_col.count_documents(visible_active_query),
        total_inactive=agent_codes_col.count_documents(visible_inactive_query),
        base_qs=urlencode(qs),
        is_main_admin=_is_main_admin(),
    )


@admin_agent_codes_bp.route("/admin/agent-codes/<code_id>/status", methods=["POST"])
def update_agent_code_status(code_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    code_oid = _to_oid(code_id)
    new_status = (request.form.get("status") or "").strip().lower()
    if not code_oid or new_status not in {"active", "inactive"}:
        return redirect(url_for("admin_agent_codes.admin_agent_codes"))

    code_doc = agent_codes_col.find_one({"_id": code_oid})
    user_id = _to_oid((code_doc or {}).get("user_id"))
    if not user_id:
        return redirect(url_for("admin_agent_codes.admin_agent_codes"))

    allowed_ids = set(_visible_agent_user_ids())
    if user_id not in allowed_ids:
        return redirect(url_for("admin_agent_codes.admin_agent_codes"))

    agent_codes_col.update_one(
        {"_id": code_oid},
        {
            "$set": {
                "status": new_status,
                "updated_at": datetime.utcnow(),
                "status_updated_by": session.get("user_id"),
            }
        },
    )

    return redirect(
        url_for(
            "admin_agent_codes.admin_agent_codes",
            q=(request.form.get("q") or "").strip(),
            status=(request.form.get("current_status") or "").strip(),
            page=(request.form.get("page") or "1").strip(),
        )
    )


@admin_agent_codes_bp.route("/admin/agent-codes/deactivate-all", methods=["POST"])
def deactivate_all_agent_codes():
    if not _require_admin():
        return redirect(url_for("login.login"))

    visible_user_ids = _visible_agent_user_ids()
    if visible_user_ids:
        agent_codes_col.update_many(
            {"user_id": {"$in": visible_user_ids}},
            {
                "$set": {
                    "status": "inactive",
                    "updated_at": datetime.utcnow(),
                    "status_updated_by": session.get("user_id"),
                }
            },
        )

    return redirect(url_for("admin_agent_codes.admin_agent_codes", status="inactive"))


@admin_agent_codes_bp.route("/admin/agent-codes/activate-all", methods=["POST"])
def activate_all_agent_codes():
    if not _require_admin():
        return redirect(url_for("login.login"))

    visible_user_ids = _visible_agent_user_ids()
    if visible_user_ids:
        agent_codes_col.update_many(
            {"user_id": {"$in": visible_user_ids}},
            {
                "$set": {
                    "status": "active",
                    "updated_at": datetime.utcnow(),
                    "status_updated_by": session.get("user_id"),
                }
            },
        )

    return redirect(url_for("admin_agent_codes.admin_agent_codes", status="active"))
