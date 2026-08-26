# admin_admins.py
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from db import db
from bson import ObjectId
from urllib.parse import urlencode
from datetime import datetime, timedelta
import math
import re
from copy import deepcopy
from service_admin_pricing import apply_admin_pricing_to_offers, normalize_admin_level, reprice_admin_services_for_admin
from social_boosting_pricing import SOCIAL_BOOSTING_NAME, SOCIAL_BOOSTING_SERVICE_ID

admin_admins_bp = Blueprint("admin_admins", __name__)
users_col = db["users"]
orders_col = db["orders"]
services_col = db["services"]

ADMIN_LEVELS = ("admin", "super_admin", "super_professional")
ADMIN_LEVEL_LABELS = {
    "admin": "Admin",
    "super_admin": "Super Admin",
    "super_professional": "Professional Admin",
}


def _normalize_admin_level(raw: str | None) -> str:
    lvl = (raw or "").strip().lower()
    return lvl if lvl in ADMIN_LEVELS else "admin"


def _admin_level_label(raw: str | None) -> str:
    return ADMIN_LEVEL_LABELS.get(_normalize_admin_level(raw), "Admin")


def _avg_daily_sales_for_admin(admin_oid: ObjectId, days_back: int = 30) -> float:
    if not admin_oid:
        return 0.0
    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    paid_statuses = ["processing", "delivered", "success", "completed", "paid"]
    amt_expr = {"$ifNull": ["$charged_amount", "$total_amount"]}
    pipeline = [
        {"$match": {
            "admin_id": admin_oid,
            "status": {"$in": paid_statuses},
            "created_at": {"$gte": start, "$lt": end},
        }},
        {"$group": {
            "_id": None,
            "total": {"$sum": {"$convert": {"input": amt_expr, "to": "double", "onError": 0, "onNull": 0}}},
        }},
    ]
    try:
        doc = next(orders_col.aggregate(pipeline), None)
        total = float((doc or {}).get("total", 0) or 0)
    except Exception:
        total = 0.0
    return round(total / float(days_back), 2)


def _upgrade_requirements(admin_doc: dict) -> dict:
    now = datetime.utcnow()
    created_at = admin_doc.get("created_at")
    if isinstance(created_at, datetime):
        age_days = max(0, (now - created_at).days)
    else:
        age_days = 0
    age_months = round(age_days / 30.0, 2) if age_days else 0.0

    admin_oid = admin_doc.get("_id")
    agents = users_col.count_documents({
        "role": "agent",
        "admin_id": admin_oid,
        "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
    })

    avg_daily_sales = _avg_daily_sales_for_admin(admin_oid, days_back=30)

    super_admin_req = {
        "min_months": 3,
        "min_agents": 30,
        "min_avg_sales": 500,
    }
    super_prof_req = {
        "min_months": 6,
        "min_agents": 70,
        "min_avg_sales": 1000,
    }

    super_admin_meets = {
        "months": age_months >= super_admin_req["min_months"],
        "agents": agents >= super_admin_req["min_agents"],
        "sales": avg_daily_sales >= super_admin_req["min_avg_sales"],
    }
    super_prof_meets = {
        "months": age_months >= super_prof_req["min_months"],
        "agents": agents >= super_prof_req["min_agents"],
        "sales": avg_daily_sales >= super_prof_req["min_avg_sales"],
    }

    return {
        "metrics": {
            "age_days": age_days,
            "age_months": age_months,
            "agents": int(agents),
            "avg_daily_sales": float(avg_daily_sales),
        },
        "requirements": {
            "super_admin": {
                **super_admin_req,
                "meets": super_admin_meets,
                "eligible": all(super_admin_meets.values()),
            },
            "super_professional": {
                **super_prof_req,
                "meets": super_prof_meets,
                "eligible": all(super_prof_meets.values()),
            },
        },
    }


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _require_main_admin_json():
    if not _is_main_admin():
        return False, (jsonify({"status": "error", "message": "Unauthorized"}), 403)
    return True, None


def _to_object_id(hex_id: str):
    try:
        return ObjectId(hex_id)
    except Exception:
        return None


def _base_services_query() -> dict:
    return {
        "$and": [
            {"$or": [{"admin_id": {"$exists": False}}, {"admin_id": None}]},
            {"_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID}},
            {"name": {"$ne": SOCIAL_BOOSTING_NAME}},
        ]
    }


def _duplicate_base_services_for_admin(admin_id: ObjectId) -> int:
    if not isinstance(admin_id, ObjectId):
        return 0

    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) or {}
    admin_level = normalize_admin_level(admin_doc.get("admin_level"))
    base_services = list(services_col.find(_base_services_query()))
    if not base_services:
        return 0

    now = datetime.utcnow()
    to_insert = []
    for base in base_services:
        if services_col.find_one({"admin_id": admin_id, "base_service_id": base.get("_id")}, {"_id": 1}):
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

    if to_insert:
        services_col.insert_many(to_insert)
    return len(to_insert)


@admin_admins_bp.route("/admin/admins")
def view_admins():
    if not _is_main_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    role = (request.args.get("role") or "").strip().lower()  # admin | main_admin | ""
    status = (request.args.get("status") or "").strip().lower()  # active | pending | blocked | ""

    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 15

    conditions = [{"role": {"$in": ["admin", "main_admin"]}}]
    conditions.append({"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]})

    if q:
        regex = {"$regex": re.escape(q), "$options": "i"}
        conditions.append({
            "$or": [
                {"first_name": regex},
                {"last_name": regex},
                {"username": regex},
                {"email": regex},
                {"phone": regex},
                {"business_name": regex},
            ]
        })

    if role in {"admin", "main_admin"}:
        conditions.append({"role": role})

    if status == "blocked":
        conditions.append({"status": "blocked"})
    elif status == "pending":
        conditions.append({"status": "pending"})
    elif status == "active":
        conditions.append({"$or": [
            {"status": "active"},
            {"status": {"$exists": False}}
        ]})

    query = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    total = users_col.count_documents(query)
    total_pages = max(math.ceil(total / per_page), 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    admins = list(
        users_col.find(query)
        .sort([("_id", -1)])
        .skip(skip)
        .limit(per_page)
    )
    for a in admins:
        a["admin_level"] = _normalize_admin_level(a.get("admin_level"))
        a["admin_level_label"] = _admin_level_label(a.get("admin_level"))

    # Summary KPIs (all admins, not just current page)
    base_admin_conditions = [
        {"role": {"$in": ["admin", "main_admin"]}},
        {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
    ]
    total_admins = users_col.count_documents({"$and": base_admin_conditions})
    total_blocked = users_col.count_documents({"$and": base_admin_conditions + [{"status": "blocked"}]})
    total_pending = users_col.count_documents({"$and": base_admin_conditions + [{"status": "pending"}]})
    total_active = users_col.count_documents({
        "$and": base_admin_conditions + [{"$or": [{"status": "active"}, {"status": {"$exists": False}}]}]
    })

    # Counts of agents/customers per admin (lightweight aggregate)
    counts_map = {}
    try:
        pipeline = [
            {"$match": {"role": {"$in": ["agent", "customer"]}, "admin_id": {"$exists": True}}},
            {"$group": {
                "_id": "$admin_id",
                "agents": {"$sum": {"$cond": [{"$eq": ["$role", "agent"]}, 1, 0]}},
                "customers": {"$sum": {"$cond": [{"$eq": ["$role", "customer"]}, 1, 0]}},
            }},
        ]
        for row in users_col.aggregate(pipeline):
            counts_map[str(row["_id"])] = {
                "agents": row.get("agents", 0),
                "customers": row.get("customers", 0),
            }
    except Exception:
        counts_map = {}

    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)
    base_qs = urlencode(qs)

    return render_template(
        "admin_admins.html",
        admins=admins,
        q=q,
        role=role,
        status=status,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        base_qs=base_qs,
        total_admins=total_admins,
        total_active=total_active,
        total_blocked=total_blocked,
        total_pending=total_pending,
        counts_map=counts_map,
        current_user_id=str(session.get("user_id") or ""),
    )


@admin_admins_bp.route("/admin/admins/eligibility/<admin_id>", methods=["GET"])
def admin_level_eligibility(admin_id):
    ok, resp = _require_main_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(admin_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid admin id"}), 400

    admin_user = users_col.find_one({"_id": oid, "role": {"$in": ["admin", "main_admin"]}})
    if not admin_user:
        return jsonify({"status": "error", "message": "Admin not found"}), 404

    if (admin_user.get("role") or "").lower() == "main_admin":
        return jsonify({"status": "error", "message": "Main admin level is fixed"}), 400

    payload = _upgrade_requirements(admin_user)
    return jsonify({
        "status": "success",
        "admin_id": str(oid),
        "name": (admin_user.get("first_name") or "") + " " + (admin_user.get("last_name") or ""),
        "current_level": _normalize_admin_level(admin_user.get("admin_level")),
        "current_level_label": _admin_level_label(admin_user.get("admin_level")),
        **payload,
    })


@admin_admins_bp.route("/admin/admins/level/<admin_id>", methods=["POST"])
def admin_set_level(admin_id):
    ok, resp = _require_main_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(admin_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid admin id"}), 400

    payload = request.get_json(silent=True) or {}
    level_raw = (payload.get("level") or request.form.get("level") or "").strip().lower()
    level = _normalize_admin_level(level_raw)
    if level not in ADMIN_LEVELS:
        return jsonify({"status": "error", "message": "Invalid admin level"}), 400

    admin_user = users_col.find_one({"_id": oid, "role": {"$in": ["admin", "main_admin"]}}, {"role": 1})
    if not admin_user:
        return jsonify({"status": "error", "message": "Admin not found"}), 404
    if (admin_user.get("role") or "").lower() == "main_admin":
        return jsonify({"status": "error", "message": "Main admin level is fixed"}), 400

    now = datetime.utcnow()
    res = users_col.update_one(
        {"_id": oid},
        {"$set": {
            "admin_level": level,
            "admin_level_updated_at": now,
            "admin_level_updated_by": session.get("user_id"),
            "updated_at": now,
        }}
    )
    try:
        reprice_admin_services_for_admin(oid)
    except Exception:
        pass

    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Admin level updated",
        "level": level,
        "label": _admin_level_label(level),
    })


@admin_admins_bp.route("/admin/admins/approve/<admin_id>", methods=["POST"])
def approve_admin(admin_id):
    ok, resp = _require_main_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(admin_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid admin id"}), 400

    admin_user = users_col.find_one({"_id": oid, "role": "admin"})
    if not admin_user:
        return jsonify({"status": "error", "message": "Admin not found"}), 404
    if admin_user.get("deleted") is True or (admin_user.get("status") or "").lower() == "deleted":
        return jsonify({"status": "error", "message": "Deleted admins cannot be approved"}), 400
    if (admin_user.get("status") or "active").lower() == "blocked":
        return jsonify({"status": "error", "message": "Blocked admins must be unblocked before approval"}), 400

    now = datetime.utcnow()
    res = users_col.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": "active",
                "approval_status": "approved",
                "approved_at": now,
                "approved_by": session.get("user_id"),
                "updated_at": now,
            },
            "$push": {
                "status_history": {
                    "at": now,
                    "by": session.get("user_id"),
                    "action": "approve",
                    "to": "active",
                }
            },
        },
    )

    services_created = 0
    try:
        services_created = _duplicate_base_services_for_admin(oid)
        reprice_admin_services_for_admin(oid)
    except Exception:
        services_created = 0

    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Admin approved" if res.modified_count else "Admin already approved",
        "services_created": services_created,
    })


@admin_admins_bp.route("/admin/admins/toggle_block/<admin_id>", methods=["POST"])
def toggle_admin_block(admin_id):
    ok, resp = _require_main_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(admin_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid admin id"}), 400

    if str(session.get("user_id") or "") == str(admin_id):
        return jsonify({"status": "error", "message": "You cannot block your own account"}), 400

    payload = request.get_json(silent=True) or {}
    block = bool(payload.get("block", False))

    admin_user = users_col.find_one({"_id": oid, "role": {"$in": ["admin", "main_admin"]}})
    if not admin_user:
        return jsonify({"status": "error", "message": "Admin not found"}), 404

    if (admin_user.get("role") or "").lower() == "main_admin":
        return jsonify({"status": "error", "message": "Main admin cannot be blocked"}), 400

    now = datetime.utcnow()
    new_status = "blocked" if block else "active"

    res = users_col.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": new_status,
                "is_blocked": bool(block),
                "status_updated_at": now
            },
            "$push": {
                "status_history": {
                    "at": now,
                    "by": session.get("user_id"),
                    "action": "toggle_block",
                    "to": new_status
                }
            }
        }
    )

    return jsonify({
        "status": "success",
        "message": "Admin blocked" if block else "Admin unblocked",
        "new_status": new_status,
        "modified": int(bool(res.modified_count))
    })


@admin_admins_bp.route("/admin/admins/delete/<admin_id>", methods=["POST"])
def delete_admin(admin_id):
    ok, resp = _require_main_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(admin_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid admin id"}), 400

    if str(session.get("user_id") or "") == str(admin_id):
        return jsonify({"status": "error", "message": "You cannot delete your own account"}), 400

    payload = request.get_json(silent=True) or {}
    hard = bool(payload.get("hard", False))

    admin_user = users_col.find_one({"_id": oid, "role": {"$in": ["admin", "main_admin"]}})
    if not admin_user:
        return jsonify({"status": "error", "message": "Admin not found"}), 404

    if (admin_user.get("role") or "").lower() == "main_admin":
        return jsonify({"status": "error", "message": "Main admin cannot be deleted"}), 400

    service_delete_query = {"admin_id": {"$in": [oid, str(oid)]}}

    if hard:
        service_res = services_col.delete_many(service_delete_query)
        res = users_col.delete_one({"_id": oid})
        return jsonify({
            "status": "success" if res.deleted_count else "noop",
            "message": "Admin permanently deleted" if res.deleted_count else "No action taken",
            "hard": True,
            "services_deleted": service_res.deleted_count,
        })

    now = datetime.utcnow()
    service_res = services_col.delete_many(service_delete_query)
    res = users_col.update_one(
        {"_id": oid},
        {
            "$set": {
                "deleted": True,
                "deleted_at": now,
                "status": "deleted"
            },
            "$push": {
                "status_history": {
                    "at": now,
                    "by": session.get("user_id"),
                    "action": "delete",
                    "to": "deleted"
                }
            }
        }
    )
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Admin deleted (soft)" if res.modified_count else "No action taken",
        "hard": False,
        "services_deleted": service_res.deleted_count,
    })
