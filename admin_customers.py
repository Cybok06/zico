# admin_customers.py
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from db import db
from urllib.parse import urlencode
from bson import ObjectId
import math
import re
from werkzeug.security import generate_password_hash
from datetime import datetime
from tenant import current_admin_id_from_session

admin_customers_bp = Blueprint("admin_customers", __name__)
users_col = db["users"]


def _admin_oid():
    return current_admin_id_from_session(session)

def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"

def _require_admin_json():
    if session.get("role") not in {"admin", "main_admin"}:
        return False, (jsonify({"status": "error", "message": "Unauthorized"}), 403)
    return True, None

def _require_admin_redirect():
    return session.get("role") in {"admin", "main_admin"}

def _to_object_id(hex_id: str):
    try:
        return ObjectId(hex_id)
    except Exception:
        return None

# View Customers Page
@admin_customers_bp.route("/admin/customers")
def view_customers():
    if not _require_admin_redirect():
        return redirect(url_for("login.login"))

    # --- Filters ---
    q = (request.args.get("q") or "").strip()
    referral = (request.args.get("referral") or "").strip()
    has_whatsapp = request.args.get("has_whatsapp")
    has_email = request.args.get("has_email")
    status = (request.args.get("status") or "").strip().lower()  # 'active' | 'pending' | 'blocked' | ''
    stage_label = (request.args.get("stage_label") or "").strip()

    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 15

    # Build query
    conditions = [{"role": {"$in": ["customer", "agent"]}}]
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        conditions.append({"admin_id": admin_oid})

    # Exclude deleted by default
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
                {"whatsapp": regex},
                {"referral": regex},
            ]
        })

    if referral:
        conditions.append({"referral": {"$regex": re.escape(referral), "$options": "i"}})

    if has_whatsapp == "1":
        conditions.append({"whatsapp": {"$exists": True, "$ne": ""}})
    elif has_whatsapp == "0":
        conditions.append({"$or": [
            {"whatsapp": {"$exists": False}},
            {"whatsapp": ""},
            {"whatsapp": None},
        ]})

    if has_email == "1":
        conditions.append({"email": {"$exists": True, "$ne": ""}})
    elif has_email == "0":
        conditions.append({"$or": [
            {"email": {"$exists": False}},
            {"email": ""},
            {"email": None},
        ]})

    if stage_label:
        if stage_label == "Normal Agent":
            conditions.append({"$or": [
                {"stage_label": "Normal Agent"},
                {"stage_label": {"$exists": False}},
                {"stage_label": ""},
                {"stage_label": None},
            ]})
        else:
            conditions.append({"stage_label": stage_label})

    # IMPORTANT: treat missing status as "active" for filtering
    if status == "blocked":
        conditions.append({"status": "blocked"})
    elif status == "pending":
        conditions.append({"status": "pending", "approval_status": "pending"})
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

    customers = list(
        users_col.find(query)
        .sort([("_id", -1)])
        .skip(skip)
        .limit(per_page)
    )

    # Base query string for pagination
    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)
    base_qs = urlencode(qs)

    return render_template(
        "admin_customers.html",
        customers=customers,
        q=q,
        referral=referral,
        has_whatsapp=has_whatsapp,
        has_email=has_email,
        status=status,
        stage_label=stage_label,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        base_qs=base_qs
    )

# Update Customer API (AJAX)
@admin_customers_bp.route("/admin/customers/update/<customer_id>", methods=["POST"])
def update_customer(customer_id):
    if session.get("role") not in {"admin", "main_admin"}:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    data = {k: v.strip() for k, v in request.form.items() if isinstance(v, str) and v.strip()}

    stage_label = (request.form.get("stage_label") or "").strip()
    if stage_label:
        if stage_label not in {"Normal Agent", "Elite Agent", "Premium"}:
            return jsonify({"status": "error", "message": "Invalid stage label"}), 400
        data["stage_label"] = stage_label

    # Handle password hashing if provided
    if "password" in data and data["password"]:
        data["password"] = generate_password_hash(data["password"])
    else:
        data.pop("password", None)

    # Protect fields
    for key in ("_id", "role", "deleted", "deleted_at"):
        data.pop(key, None)

    q = {"_id": oid, "role": {"$in": ["customer", "agent"]}}
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        q["admin_id"] = admin_oid
    res = users_col.update_one(q, {"$set": data})
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Customer updated successfully" if res.modified_count else "No changes detected"
    })

# === Block / Unblock with audit & boolean flag ===
@admin_customers_bp.route("/admin/customers/toggle_block/<customer_id>", methods=["POST"])
def toggle_block(customer_id):
    ok, resp = _require_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    payload = request.get_json(silent=True) or {}
    block = bool(payload.get("block", False))

    q = {"_id": oid, "role": {"$in": ["customer", "agent"]}}
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        q["admin_id"] = admin_oid
    user = users_col.find_one(q)
    if not user:
        return jsonify({"status": "error", "message": "Customer not found"}), 404

    now = datetime.utcnow()
    new_status = "blocked" if block else "active"

    update_doc = {
        "$set": {
            "status": new_status,
            "is_blocked": bool(block),
            "status_updated_at": now
        },
        "$push": {
            "status_history": {
                "at": now,
                "by": session.get("admin_id") or session.get("user_id"),
                "action": "toggle_block",
                "to": new_status
            }
        }
    }

    uq = {"_id": oid}
    if admin_oid and not _is_main_admin():
        uq["admin_id"] = admin_oid
    res = users_col.update_one(uq, update_doc)

    return jsonify({
        "status": "success",
        "message": "Customer blocked" if block else "Customer unblocked",
        "new_status": new_status,
        "modified": int(bool(res.modified_count))
    })


@admin_customers_bp.route("/admin/customers/approve_agent/<customer_id>", methods=["POST"])
def approve_agent(customer_id):
    ok, resp = _require_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid agent id"}), 400

    q = {
        "_id": oid,
        "role": "agent",
        "status": "pending",
        "approval_status": "pending",
    }
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        q["admin_id"] = admin_oid

    now = datetime.utcnow()
    res = users_col.update_one(
        q,
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
                    "action": "approve_agent",
                    "to": "active",
                }
            },
        },
    )
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Agent approved" if res.modified_count else "Agent is not pending approval",
    })

# === Delete (Soft by default, optional hard delete) ===
@admin_customers_bp.route("/admin/customers/delete/<customer_id>", methods=["POST"])
def delete_customer(customer_id):
    ok, resp = _require_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    payload = request.get_json(silent=True) or {}
    hard = bool(payload.get("hard", False))

    q = {"_id": oid, "role": {"$in": ["customer", "agent"]}}
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        q["admin_id"] = admin_oid
    user = users_col.find_one(q)
    if not user:
        return jsonify({"status": "error", "message": "Customer not found"}), 404

    if hard:
        # Permanent delete (use with care; consider cascading in your app if needed)
        dq = {"_id": oid}
        if admin_oid and not _is_main_admin():
            dq["admin_id"] = admin_oid
        res = users_col.delete_one(dq)
        return jsonify({
            "status": "success" if res.deleted_count else "noop",
            "message": "Customer permanently deleted" if res.deleted_count else "No action taken",
            "hard": True
        })

    # Soft delete (recommended)
    now = datetime.utcnow()
    res = users_col.update_one(
        {"_id": oid, **({"admin_id": admin_oid} if (admin_oid and not _is_main_admin()) else {})},
        {
            "$set": {
                "deleted": True,
                "deleted_at": now,
                "status": "deleted"
            },
            "$push": {
                "status_history": {
                    "at": now,
                    "by": session.get("admin_id") or session.get("user_id"),
                    "action": "delete",
                    "to": "deleted"
                }
            }
        }
    )
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Customer deleted (soft)" if res.modified_count else "No action taken",
        "hard": False
    })
