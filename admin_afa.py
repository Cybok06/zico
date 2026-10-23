# admin_afa.py
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from bson import ObjectId
from db import db
from datetime import datetime, timedelta
import re
from afa_settings_utils import (
    ADMIN_LEVELS,
    ADMIN_LEVEL_LABELS,
    DEFAULT_AFA_PRICE,
    SETTINGS_ID,
    ensure_admin_afa_settings,
    load_afa_admin_base_price,
    load_afa_base_price,
    load_afa_level_prices,
    load_afa_price,
    normalize_admin_level,
    save_afa_level_prices,
)
from tenant import current_admin_id_from_session
from profit_ledger import apply_profit_split, normalize_profit_line, profit_totals

admin_afa_bp = Blueprint("admin_afa", __name__)

# Collections
afa_col = db["afa_registrations"]
users_col = db["users"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
orders_col = db["orders"]
afa_settings_col = db["afa_settings"]
# Optional: if you want to reflect open/stock into a service doc (e.g., "AFA TALKTIME")
services_col = db["services"]

# Constants / defaults
AMOUNT_DEFAULT = DEFAULT_AFA_PRICE  # only used as a last-resort fallback

# ------------------ Helpers ------------------

def _now():
    return datetime.utcnow()

def _require_admin():
    return (session.get("role") or "").strip().lower() in {
        "admin",
        "main_admin",
        "super_admin",
        "professional_admin",
        "super_professional",
        "superadmin",
    }

def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"

def _scope_query(base: dict | None = None) -> dict:
    q = dict(base or {})
    if not _is_main_admin():
        admin_oid = _admin_oid()
        if admin_oid:
            q["admin_id"] = admin_oid
    return q

def _admin_oid():
    return current_admin_id_from_session(session)

def _get_actor():
    actor_id = session.get("user_id")
    actor_name = session.get("username") or session.get("email") or "admin"
    if actor_id:
        try:
            u = users_col.find_one({"_id": ObjectId(actor_id)})
            if u:
                actor_name = (
                    u.get("username")
                    or f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                    or actor_name
                )
        except Exception:
            pass
    return actor_id, actor_name

def _to_objectid(maybe):
    try:
        return ObjectId(maybe)
    except Exception:
        return None

def _settings_key(admin_oid: ObjectId | None):
    if not admin_oid:
        return SETTINGS_ID
    if _is_main_admin() and str(admin_oid) == str(session.get("user_id")):
        return SETTINGS_ID
    if admin_oid and users_col.find_one({"_id": admin_oid, "role": "main_admin"}, {"_id": 1}):
        return SETTINGS_ID
    return f"{SETTINGS_ID}:{str(admin_oid)}"

def _get_settings(admin_oid: ObjectId | None = None):
    """
    Ensure there is a single settings doc.
    Structure: { _id, price: float, is_open: bool, in_stock: bool, updated_at: datetime }
    """
    if _is_main_admin():
        return ensure_admin_afa_settings(None, default_price=AMOUNT_DEFAULT)
    scoped_admin_oid = admin_oid or _admin_oid()
    assigned_base = load_afa_admin_base_price(scoped_admin_oid, users_col, default=AMOUNT_DEFAULT)
    return ensure_admin_afa_settings(scoped_admin_oid, default_price=assigned_base)

def _save_settings(price: float, is_open: bool, in_stock: bool, admin_oid: ObjectId | None = None):
    admin_oid = admin_oid or _admin_oid()
    key = _settings_key(admin_oid)
    if not key:
        return _get_settings(admin_oid)
    doc = {
        "price": float(price),
        "is_open": bool(is_open),
        "in_stock": bool(in_stock),
        "updated_at": _now(),
    }
    query = {"_id": key}
    if key != SETTINGS_ID:
        doc["admin_id"] = admin_oid
    afa_settings_col.update_one(query, {"$set": doc}, upsert=True)
    return afa_settings_col.find_one({"_id": key}) or _get_settings(admin_oid)

def _settings_price(admin_oid: ObjectId | None = None):
    if admin_oid is None:
        return load_afa_price(None if _is_main_admin() else _admin_oid(), default=AMOUNT_DEFAULT)
    if _is_main_admin() and str(admin_oid) == str(session.get("user_id")):
        return load_afa_price(None, default=AMOUNT_DEFAULT)
    if _is_main_admin() and users_col.find_one({"_id": admin_oid, "role": "main_admin"}, {"_id": 1}):
        return load_afa_price(None, default=AMOUNT_DEFAULT)
    return load_afa_price(admin_oid, default=AMOUNT_DEFAULT)


def _afa_profit_value(order: dict) -> float:
    try:
        if _is_main_admin():
            profit = float(order.get("main_admin_profit_total") or 0)
            if profit <= 0:
                profit = float(order.get("profit_amount_total") or 0)
        else:
            profit = float(order.get("admin_profit_total") or 0)
            if profit <= 0:
                profit = float(order.get("profit_amount_total") or 0)
        return profit
    except Exception:
        return 0.0


def _today_range() -> tuple[datetime, datetime]:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    return start, start + timedelta(days=1)


def _afa_profit_summary(reg_query: dict, admin_oid: ObjectId | None) -> dict:
    total_profit = 0.0
    today_profit = 0.0
    today_start, today_end = _today_range()

    order_query = {
        "kind": "afa_registration",
        "status": {"$nin": ["cancelled", "canceled", "failed", "refunded"]},
    }
    if not _is_main_admin() and admin_oid:
        order_query["admin_id"] = admin_oid

    reg_ids = []
    try:
        for reg in afa_col.find(reg_query, {"_id": 1, "charged": 1, "refunded": 1}):
            if reg.get("charged") and not reg.get("refunded"):
                reg_ids.append(reg["_id"])
    except Exception as exc:
        try:
            print("[afa_profit_summary_reg_error]", {
                "error": str(exc),
                "role": session.get("role"),
                "admin_oid": str(admin_oid or ""),
                "reg_query": str(reg_query),
            })
        except Exception:
            pass
        reg_ids = []

    if not reg_ids:
        return {"total_profit": 0.0, "today_profit": 0.0}

    order_query["afa_registration_id"] = {"$in": reg_ids}
    try:
        for order in orders_col.find(
            order_query,
            {
                "created_at": 1,
                "main_admin_profit_total": 1,
                "admin_profit_total": 1,
                "profit_amount_total": 1,
            },
        ):
            profit = _afa_profit_value(order)
            total_profit += profit
            created = order.get("created_at")
            if isinstance(created, datetime) and today_start <= created < today_end:
                today_profit += profit
    except Exception as exc:
        try:
            print("[afa_profit_summary_order_error]", {
                "error": str(exc),
                "role": session.get("role"),
                "admin_oid": str(admin_oid or ""),
                "order_query": str(order_query),
            })
        except Exception:
            pass

    return {
        "total_profit": round(total_profit, 2),
        "today_profit": round(today_profit, 2),
    }


def _admin_level_for(admin_oid: ObjectId | None) -> str:
    if not admin_oid:
        return "admin"
    try:
        doc = users_col.find_one({"_id": admin_oid}, {"admin_level": 1}) or {}
        return normalize_admin_level(doc.get("admin_level"))
    except Exception:
        return "admin"


def _admin_is_main(admin_oid: ObjectId | None) -> bool:
    if not admin_oid:
        return False
    try:
        return bool(users_col.find_one({"_id": admin_oid, "role": "main_admin"}, {"_id": 1}))
    except Exception:
        return False


def _clear_dashboard_cache_safely():
    try:
        from admin_dashboard import clear_dashboard_cache

        clear_dashboard_cache()
    except Exception:
        pass


def _afa_order_profit_line(reg: dict, amount: float, admin_oid: ObjectId | None = None) -> tuple[dict, dict]:
    owner_admin_oid = admin_oid or reg.get("admin_id")
    main_base_price = round(float(load_afa_base_price(default=0.0) or 0.0), 2)
    assigned_admin_price = round(float(load_afa_admin_base_price(owner_admin_oid, users_col, default=main_base_price) or 0.0), 2)
    selling = round(float(amount or 0.0), 2)
    if _admin_is_main(owner_admin_oid):
        assigned_admin_price = selling
    line = {
        "phone": reg.get("phone"),
        "base_amount": assigned_admin_price,
        "main_base_amount": main_base_price,
        "admin_base_amount": assigned_admin_price,
        "selling_amount": selling,
        "amount": selling,
        "profit_amount": 0.0,
        "profit_percent_used": 0.0,
        "value": "AFA Registration",
        "value_obj": {"registration_id": str(reg.get("_id") or ""), "source": "admin_afa_charge"},
        "serviceId": "afa_registration",
        "serviceName": "AFA Registration",
        "service_type": "AFA",
        "line_status": "completed",
        "api_status": "not_applicable",
        "api_response": {"note": "AFA registration charged."},
    }
    finalized = apply_profit_split(
        normalize_profit_line(
            line,
            selling_amount=selling,
            main_base_amount=main_base_price,
            admin_base_amount=assigned_admin_price,
        )
    )
    return finalized, profit_totals([finalized])

def _find_balance_doc(customer_id, admin_oid: ObjectId | None = None):
    """Accepts ObjectId or raw value stored in reg['customer_id']."""
    bal = None
    q1 = {"user_id": customer_id}
    if admin_oid:
        q1["admin_id"] = admin_oid
    if isinstance(customer_id, ObjectId):
        bal = balances_col.find_one(q1)
    q2 = {"user_id": customer_id}
    if admin_oid:
        q2["admin_id"] = admin_oid
    if not bal and customer_id:
        bal = balances_col.find_one(q2)
    return bal

def _refund_registration(reg: dict, amount: float, actor_id: str | None, actor_name: str, admin_oid: ObjectId | None = None):
    """
    Idempotent refund:
      - If already refunded, no-op.
      - Credits user's wallet, logs deposit, stamps refunded_* fields.
    Returns (ok: bool, msg: str, already_refunded: bool)
    """
    if reg.get("refunded"):
        return True, "Already refunded", True

    customer_id = reg.get("customer_id")
    bal = _find_balance_doc(customer_id, admin_oid)
    if not bal:
        return False, "Customer balance not found", False

    old_amount = float(bal.get("amount", 0.0))
    new_amount = old_amount + float(amount)

    # credit balance
    balances_col.update_one(
        {"_id": bal["_id"], **({"admin_id": admin_oid} if admin_oid else {})},
        {"$set": {"amount": new_amount, "updated_at": _now()}},
    )

    # log deposit
    log_doc = {
        "balance_id": bal["_id"],
        "user_id": bal["user_id"],
        "admin_id": admin_oid or reg.get("admin_id"),
        "action": "deposit",
        "delta": float(amount),
        "amount_before": old_amount,
        "amount_after": new_amount,
        "currency": bal.get("currency", "GHS"),
        "note": f"AFA registration refund ({str(reg['_id'])})",
        "actor_id": ObjectId(actor_id) if actor_id else None,
        "actor_name": actor_name,
        "created_at": _now(),
    }
    log_res = balance_logs_col.insert_one(log_doc)
    admin_refund_amount = round(float(reg.get("admin_wallet_debit_total") or 0.0), 2)
    admin_refund_log_id = None
    if admin_refund_amount > 0 and admin_oid:
        admin_bal = balances_col.find_one({"user_id": admin_oid})
        if admin_bal:
            admin_old_amount = float(admin_bal.get("amount", 0.0))
            admin_new_amount = round(admin_old_amount + admin_refund_amount, 2)
            balances_col.update_one(
                {"_id": admin_bal["_id"]},
                {"$set": {"amount": admin_new_amount, "updated_at": _now(), "admin_id": admin_oid}},
            )
            admin_refund_log = {
                "balance_id": admin_bal["_id"],
                "user_id": admin_oid,
                "admin_id": admin_oid,
                "action": "deposit",
                "delta": admin_refund_amount,
                "amount_before": admin_old_amount,
                "amount_after": admin_new_amount,
                "currency": admin_bal.get("currency", "GHS"),
                "note": f"AFA registration admin base refund ({str(reg['_id'])})",
                "source": "admin_afa_registration_refund",
                "labels": ["admin_base_refund", "afa_registration_refund"],
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "actor_name": actor_name,
                "created_at": _now(),
            }
            admin_refund_log_id = balance_logs_col.insert_one(admin_refund_log).inserted_id

    # mark reg refunded
    afa_col.update_one(
        {"_id": reg["_id"], **({"admin_id": admin_oid} if admin_oid else {})},
        {
            "$set": {
                "refunded": True,
                "refunded_amount": float(amount),
                "refunded_at": _now(),
                "refunded_by": actor_name,
                "refund_log_id": log_res.inserted_id,
                "admin_refunded_amount": admin_refund_amount,
                "admin_refund_log_id": admin_refund_log_id,
                "admin_id": admin_oid or reg.get("admin_id"),
                "updated_at": _now(),
            }
        },
    )
    return True, "Refunded", False

# ------------------ PAGE ------------------

@admin_afa_bp.route("/admin/afa")
def admin_afa_page():
    if not _require_admin():
        return redirect(url_for("login.login"))
    return render_template("admin_afa.html")

# ------------------ SETTINGS API ------------------

@admin_afa_bp.route("/admin/api/afa/settings", methods=["GET"])
def admin_afa_get_settings():
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401
    admin_oid = _admin_oid()
    s = _get_settings(admin_oid)
    main_base_price = float(load_afa_base_price(default=DEFAULT_AFA_PRICE))
    assigned_base_price = float(load_afa_admin_base_price(admin_oid, users_col, default=DEFAULT_AFA_PRICE))
    level_prices = load_afa_level_prices(default=main_base_price)
    admin_level = _admin_level_for(admin_oid)
    return jsonify(
        success=True,
        data={
            "price": float(s.get("price", AMOUNT_DEFAULT)),
            "base_price": main_base_price if _is_main_admin() else assigned_base_price,
            "main_base_price": main_base_price,
            "assigned_base_price": assigned_base_price,
            "admin_level": admin_level,
            "admin_level_label": ADMIN_LEVEL_LABELS.get(admin_level, "Admin"),
            "level_prices": level_prices,
            "level_labels": ADMIN_LEVEL_LABELS,
            "admin_levels": list(ADMIN_LEVELS),
            "min_price": 0.0 if _is_main_admin() else assigned_base_price,
            "is_main_admin": _is_main_admin(),
            "is_open": bool(s.get("is_open", True)),
            "in_stock": bool(s.get("in_stock", True)),
            "updated_at": s.get("updated_at").strftime("%d %b %Y, %I:%M %p")
            if s.get("updated_at")
            else "",
        },
    )

@admin_afa_bp.route("/admin/api/afa/settings", methods=["POST"])
def admin_afa_set_settings():
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401
    admin_oid = _admin_oid()

    payload = request.get_json(silent=True) or {}
    try:
        price = float(payload.get("price", AMOUNT_DEFAULT))
        if price < 0:
            return jsonify(success=False, error="Price must be >= 0.00"), 400
        if _is_main_admin():
            raw_level_prices = payload.get("level_prices") or {}
            for level, raw_level_price in raw_level_prices.items():
                level_price = float(raw_level_price or 0)
                if level_price < price:
                    label = ADMIN_LEVEL_LABELS.get(normalize_admin_level(level), "Admin")
                    return jsonify(success=False, error=f"{label} AFA price cannot be below base price (GHS {price:.2f})."), 400
        else:
            base_price = load_afa_admin_base_price(admin_oid, users_col, default=DEFAULT_AFA_PRICE)
            if price < base_price:
                return jsonify(success=False, error=f"Price cannot be below your assigned AFA price (GHS {base_price:.2f})."), 400
        is_open = bool(payload.get("is_open", True))
        in_stock = bool(payload.get("in_stock", True))
    except Exception as e:
        return jsonify(success=False, error=str(e)), 400

    doc = _save_settings(price, is_open, in_stock, admin_oid)
    level_prices = {}
    if _is_main_admin():
        level_prices = save_afa_level_prices(payload.get("level_prices") or {}, min_price=price)
    try:
        print("[afa_settings_save_debug]", {
            "role": session.get("role"),
            "admin_level": session.get("admin_level"),
            "user_id": str(session.get("user_id") or ""),
            "admin_oid": str(admin_oid or ""),
            "key": _settings_key(admin_oid),
            "price_requested": price,
            "assigned_base_price": float(load_afa_admin_base_price(admin_oid, users_col, default=DEFAULT_AFA_PRICE)),
            "saved_price": float((doc or {}).get("price") or 0),
            "doc_id": str((doc or {}).get("_id") or ""),
        })
    except Exception:
        pass

    # (Optional) Mirror flags to a service doc if present (safe no-op otherwise)
    try:
        services_col.update_many(
            {"name": {"$in": ["AFA TALKTIME", "AFA Registration"]}, "admin_id": admin_oid},
            {
                "$set": {
                    "status": "OPEN" if is_open else "CLOSED",
                    "availability": "AVAILABLE" if in_stock else "OUT_OF_STOCK",
                    "updated_at": _now(),
                }
            },
        )
    except Exception:
        pass

    return jsonify(
        success=True,
        data={
            "price": float(doc["price"]),
            "level_prices": level_prices,
            "is_open": bool(doc["is_open"]),
            "in_stock": bool(doc["in_stock"]),
            "updated_at": doc["updated_at"].strftime("%d %b %Y, %I:%M %p"),
        },
    )

# ------------- LIST / FILTER API -------------

@admin_afa_bp.route("/admin/api/afa/list", methods=["GET"])
def admin_afa_list():
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401

    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401

    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    charged = (request.args.get("charged") or "").strip().lower()  # '', 'true', 'false'
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        page_size = int(request.args.get("page_size", 25))
    except Exception:
        page_size = 25
    page_size = max(1, min(page_size, 200))

    query = _scope_query()
    if status:
        query["status"] = status
    if charged in {"true", "false"}:
        query["charged"] = (charged == "true")

    if q:
        rx = re.compile(re.escape(q), re.I)
        query["$or"] = [
            {"name": rx},
            {"phone": rx},
            {"ghana_card": rx},
            {"location": rx},
        ]

    if date_from or date_to:
        rng = {}
        try:
            if date_from:
                rng["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            if date_to:
                rng["$lt"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except Exception:
            pass
        if rng:
            query["created_at"] = rng

    total = afa_col.count_documents(query)

    # Status counts + total amount. Sub admins have one price; main admin may view mixed admin prices.
    agg = list(afa_col.aggregate([{"$match": query}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}]))
    status_counts = {(d["_id"] or "pending"): d["count"] for d in agg}
    if _is_main_admin():
        total_amount = 0.0
        amount_groups = list(afa_col.aggregate([{"$match": query}, {"$group": {"_id": "$admin_id", "count": {"$sum": 1}}}]))
        for group in amount_groups:
            owner_id = group.get("_id") if isinstance(group.get("_id"), ObjectId) else None
            total_amount += float(_settings_price(owner_id)) * int(group.get("count") or 0)
    else:
        total_amount = float(_settings_price(admin_oid)) * int(total or 0)
    profit_summary = _afa_profit_summary(query, admin_oid)

    cur = (
        afa_col.find(query)
        .sort([("created_at", -1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = list(cur)

    # hydrate customers for display
    cust_ids = []
    for d in items:
        cid = d.get("customer_id")
        if isinstance(cid, ObjectId):
            cust_ids.append(cid)
    users_map = {}
    if cust_ids:
        user_query = {"_id": {"$in": list(set(cust_ids))}}
        if not _is_main_admin():
            user_query["admin_id"] = admin_oid
        for u in users_col.find(user_query):
            users_map[u["_id"]] = {
                "username": u.get("username"),
                "first_name": u.get("first_name"),
                "last_name": u.get("last_name"),
                "phone": u.get("phone"),
                "admin_id": u.get("admin_id"),
            }
    admin_ids = []
    if _is_main_admin():
        for d in items:
            aid = d.get("admin_id")
            if isinstance(aid, ObjectId):
                admin_ids.append(aid)
        for u in users_map.values():
            aid = u.get("admin_id")
            if isinstance(aid, ObjectId):
                admin_ids.append(aid)
    admin_map = {}
    if admin_ids:
        for a in users_col.find({"_id": {"$in": list(set(admin_ids))}}, {"username": 1, "business_name": 1, "first_name": 1, "last_name": 1, "phone": 1}):
            admin_map[a["_id"]] = (
                a.get("business_name")
                or a.get("username")
                or f"{a.get('first_name','')} {a.get('last_name','')}".strip()
                or a.get("phone")
                or str(a["_id"])
            )

    owner_price_map = {}
    if _is_main_admin():
        owner_ids = [d.get("admin_id") for d in items if isinstance(d.get("admin_id"), ObjectId)]
        for owner_id in set(owner_ids):
            owner_price_map[owner_id] = float(_settings_price(owner_id))
    else:
        owner_price_map[admin_oid] = float(_settings_price(admin_oid))

    out_items = []
    for d in items:
        created = d.get("created_at")
        cid = d.get("customer_id")
        uinfo = users_map.get(cid) if isinstance(cid, ObjectId) else None

        owner_admin_oid = d.get("admin_id") if isinstance(d.get("admin_id"), ObjectId) else admin_oid
        amount = float(owner_price_map.get(owner_admin_oid, _settings_price(owner_admin_oid)))

        out_items.append(
            {
                "id": str(d["_id"]),
                "customer": {
                    "id": str(cid) if cid is not None else None,
                    "name": (
                        (uinfo.get("username") if uinfo else None)
                        or (f"{uinfo.get('first_name','')} {uinfo.get('last_name','')}".strip() if uinfo else None)
                    ),
                    "phone": uinfo.get("phone") if uinfo else None,
                },
                "name": d.get("name"),
                "phone": d.get("phone"),
                "ghana_card": d.get("ghana_card"),
                "dob": d.get("dob"),
                "location": d.get("location"),
                "amount": amount,
                "admin": {
                    "id": str(d.get("admin_id")) if d.get("admin_id") is not None else None,
                    "name": admin_map.get(d.get("admin_id")) if _is_main_admin() else None,
                },
                "status": (d.get("status") or "pending"),
                "charged": bool(d.get("charged", False)),
                "refunded": bool(d.get("refunded", False)),
                "charged_amount": float(d.get("charged_amount", 0.0)) if d.get("charged") else None,
                "created_at_display": created.strftime("%d %b %Y, %I:%M %p") if created else "",
            }
        )

    return jsonify(
        success=True,
        items=out_items,
        total=total,
        total_amount=round(float(total_amount or 0), 2),
        total_profit=profit_summary["total_profit"],
        today_profit=profit_summary["today_profit"],
        page=page,
        page_size=page_size,
        status_counts=status_counts,
    )

# ------------- UPDATE STATUS -------------

@admin_afa_bp.route("/admin/api/afa/<reg_id>/status", methods=["POST"])
def admin_afa_update_status(reg_id):
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401

    payload = request.get_json(silent=True) or request.form or {}
    new_status = (payload.get("status") or "").strip().lower()
    # Added 'canceled' and 'refunded' to allowed set
    if new_status not in {"pending", "processing", "delivered", "completed", "failed", "rejected", "canceled", "refunded"}:
        return jsonify(success=False, error="Invalid status"), 400

    oid = _to_objectid(reg_id)
    if not oid:
        return jsonify(success=False, error="Invalid id"), 400

    upd = {"status": new_status, "updated_at": _now()}
    res = afa_col.update_one(_scope_query({"_id": oid}), {"$set": upd})
    if not res.matched_count:
        return jsonify(success=False, error="Registration not found"), 404

    return jsonify(success=True, message="Status updated.")

# ------------- CHARGE CUSTOMER (ALWAYS use settings price) -------------

@admin_afa_bp.route("/admin/api/afa/<reg_id>/charge", methods=["POST"])
def admin_afa_charge(reg_id):
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401

    oid = _to_objectid(reg_id)
    if not oid:
        return jsonify(success=False, error="Invalid id"), 400

    admin_oid = _admin_oid()
    reg = afa_col.find_one(_scope_query({"_id": oid}))
    if not reg:
        return jsonify(success=False, error="Registration not found"), 404

    if reg.get("charged"):
        return jsonify(success=False, error="Already charged"), 400

    owner_admin_oid = reg.get("admin_id") if isinstance(reg.get("admin_id"), ObjectId) else admin_oid
    current_price = _settings_price(owner_admin_oid)

    amount = round(float(current_price if current_price >= 0 else AMOUNT_DEFAULT), 2)
    main_base_price = round(float(load_afa_base_price(default=0.0) or 0.0), 2)
    admin_wallet_debit_total = 0.0
    if not _admin_is_main(owner_admin_oid):
        admin_wallet_debit_total = round(
            float(load_afa_admin_base_price(owner_admin_oid, users_col, default=main_base_price) or 0.0),
            2,
        )

    bal = _find_balance_doc(reg.get("customer_id"), owner_admin_oid)
    if not bal:
        return jsonify(success=False, error="Customer balance not found"), 404

    old_amount = float(bal.get("amount", 0.0))
    new_amount = round(old_amount - amount, 2)
    if new_amount < 0:
        return jsonify(success=False, error="Insufficient funds"), 400

    admin_bal = None
    admin_old_amount = 0.0
    admin_new_amount = 0.0
    if admin_wallet_debit_total > 0:
        admin_bal = balances_col.find_one({"user_id": owner_admin_oid})
        if not admin_bal:
            return jsonify(success=False, error="Admin wallet balance not found"), 404
        admin_old_amount = float(admin_bal.get("amount", 0.0))
        admin_new_amount = round(admin_old_amount - admin_wallet_debit_total, 2)
        if admin_new_amount < 0:
            return jsonify(success=False, error=f"Insufficient admin wallet balance. Required GHS {admin_wallet_debit_total:.2f}."), 400

    # update balance
    balances_col.update_one(
        {"_id": bal["_id"], "admin_id": owner_admin_oid},
        {"$set": {"amount": new_amount, "updated_at": _now()}},
    )
    if admin_bal:
        balances_col.update_one(
            {"_id": admin_bal["_id"]},
            {"$set": {"amount": admin_new_amount, "updated_at": _now(), "admin_id": owner_admin_oid}},
        )

    # log
    actor_id, actor_name = _get_actor()
    log_doc = {
        "balance_id": bal["_id"],
        "user_id": bal["user_id"],
        "admin_id": owner_admin_oid,
        "action": "withdraw",
        "delta": -amount,
        "amount_before": old_amount,
        "amount_after": new_amount,
        "currency": bal.get("currency", "GHS"),
        "note": f"AFA registration charge ({reg_id})",
        "actor_id": ObjectId(actor_id) if actor_id else None,
        "actor_name": actor_name,
        "created_at": _now(),
    }
    log_res = balance_logs_col.insert_one(log_doc)
    admin_log_id = None
    if admin_bal and admin_wallet_debit_total > 0:
        admin_log_doc = {
            "balance_id": admin_bal["_id"],
            "user_id": owner_admin_oid,
            "admin_id": owner_admin_oid,
            "action": "purchase_debit",
            "delta": -admin_wallet_debit_total,
            "amount_before": admin_old_amount,
            "amount_after": admin_new_amount,
            "currency": admin_bal.get("currency", "GHS"),
            "note": f"AFA registration admin base debit ({reg_id})",
            "order_id": f"AFA-{str(oid)}",
            "source": "admin_afa_registration",
            "labels": ["admin_base_debit", "afa_registration_debit"],
            "actor_id": ObjectId(actor_id) if actor_id else None,
            "actor_name": actor_name,
            "created_at": _now(),
        }
        admin_log_id = balance_logs_col.insert_one(admin_log_doc).inserted_id

    # mark registration as charged and persist the settings price used
    afa_col.update_one(
        _scope_query({"_id": oid}),
        {
            "$set": {
                "charged": True,
                "charged_amount": float(amount),
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "charged_at": _now(),
                "charged_by": actor_name,
                "charge_log_id": log_res.inserted_id,
                "admin_charge_log_id": admin_log_id,
                "amount": float(amount),  # normalize to settings price for UI/reporting
                "updated_at": _now(),
            }
        },
    )

    finalized_line, profit_split_totals = _afa_order_profit_line(reg, float(amount), owner_admin_oid)
    now = _now()
    order_id = f"AFA-{str(oid)}"
    order_doc = {
        "user_id": reg.get("customer_id"),
        "admin_id": owner_admin_oid,
        "wallet_owner_user_id": owner_admin_oid,
        "order_id": order_id,
        "items": [finalized_line],
        "total_amount": round(float(amount), 2),
        "charged_amount": round(float(amount), 2),
        "admin_wallet_debit_total": admin_wallet_debit_total,
        "agent_wallet_debit_total": round(float(amount), 2),
        "wallet_debit_status": "completed",
        "wallet_debits": [
            {
                "user_id": reg.get("customer_id"),
                "amount": round(float(amount), 2),
                "labels": ["afa_registration_debit"],
            }
        ] + ([
            {
                "user_id": owner_admin_oid,
                "amount": admin_wallet_debit_total,
                "labels": ["admin_base_debit", "afa_registration_debit"],
            }
        ] if admin_wallet_debit_total > 0 else []),
        "profit_amount_total": profit_split_totals["profit_amount_total"],
        "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
        "admin_profit_total": profit_split_totals["admin_profit_total"],
        "store_profit_total": profit_split_totals["store_profit_total"],
        "status": "completed",
        "paid_from": "wallet",
        "kind": "afa_registration",
        "afa_registration_id": oid,
        "created_at": reg.get("created_at") or now,
        "updated_at": now,
    }
    orders_col.update_one(
        {"order_id": order_id},
        {"$setOnInsert": order_doc},
        upsert=True,
    )
    _clear_dashboard_cache_safely()

    return jsonify(success=True, message="Customer charged successfully.")

# ------------- CANCEL (sets status=canceled AND refunds if applicable) -------------

@admin_afa_bp.route("/admin/api/afa/<reg_id>/cancel", methods=["POST"])
def admin_afa_cancel(reg_id):
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401

    oid = _to_objectid(reg_id)
    if not oid:
        return jsonify(success=False, error="Invalid id"), 400

    admin_oid = _admin_oid()
    reg = afa_col.find_one(_scope_query({"_id": oid}))
    if not reg:
        return jsonify(success=False, error="Registration not found"), 404
    owner_admin_oid = reg.get("admin_id") if isinstance(reg.get("admin_id"), ObjectId) else admin_oid

    actor_id, actor_name = _get_actor()

    # Always set status -> canceled
    afa_col.update_one(_scope_query({"_id": oid}), {"$set": {"status": "canceled", "updated_at": _now()}})

    # If charged and not refunded, auto-refund the charged_amount; else no-op
    charged_amount = float(reg.get("charged_amount", 0.0)) if reg.get("charged") else 0.0
    if reg.get("charged") and charged_amount > 0:
        ok, msg, already = _refund_registration(reg, charged_amount, actor_id, actor_name, owner_admin_oid)
        if not ok and not already:
            return jsonify(success=False, error=msg), 400

    return jsonify(success=True, message="Registration canceled" + (" and refunded." if charged_amount > 0 else "."))

# ------------- REFUND (manual button) -------------

@admin_afa_bp.route("/admin/api/afa/<reg_id>/refund", methods=["POST"])
def admin_afa_refund(reg_id):
    """
    Manual refund action (button).
    - If already refunded: returns success, notes already refunded.
    - If charged: refunds charged_amount.
    - If not charged: refunds the current settings price (in case you want to compensate).
      You can change this behavior to block refund when not charged; for now we allow.
    Also sets status='refunded' for clarity.
    """
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401

    oid = _to_objectid(reg_id)
    if not oid:
        return jsonify(success=False, error="Invalid id"), 400

    admin_oid = _admin_oid()
    reg = afa_col.find_one(_scope_query({"_id": oid}))
    if not reg:
        return jsonify(success=False, error="Registration not found"), 404
    owner_admin_oid = reg.get("admin_id") if isinstance(reg.get("admin_id"), ObjectId) else admin_oid

    actor_id, actor_name = _get_actor()

    if reg.get("refunded"):
        # ensure status reflects refunded
        afa_col.update_one(_scope_query({"_id": oid}), {"$set": {"status": "refunded", "updated_at": _now()}})
        return jsonify(success=True, message="Already refunded."), 200

    if reg.get("charged") and float(reg.get("charged_amount", 0.0)) > 0:
        amt = float(reg.get("charged_amount", 0.0))
    else:
        # Decide policy for uncharged refunds; using settings price here
        amt = _settings_price(owner_admin_oid)

    ok, msg, already = _refund_registration(reg, amt, actor_id, actor_name, owner_admin_oid)
    if not ok and not already:
        return jsonify(success=False, error=msg), 400

    # Mark status 'refunded' (distinct from 'canceled')
    afa_col.update_one(_scope_query({"_id": oid}), {"$set": {"status": "refunded", "updated_at": _now()}})
    return jsonify(success=True, message="Refund processed.")

# ---- AFA stats for dashboard ----

@admin_afa_bp.route("/admin/api/afa/stats", methods=["GET"])
def admin_afa_stats():
    if not _require_admin():
        return jsonify(success=False, error="Unauthorized"), 401
    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401

    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    try:
        base = _scope_query()
        total = afa_col.count_documents(base)
        today_cnt = afa_col.count_documents({"created_at": {"$gte": start, "$lt": end}, **base})
        pending = afa_col.count_documents({"status": "pending", **base})
        processing = afa_col.count_documents({"status": "processing", **base})
        delivered = afa_col.count_documents({"status": "delivered", **base})
        completed = afa_col.count_documents({"status": "completed", **base})
        failed = afa_col.count_documents({"status": {"$in": ["failed", "rejected"]}, **base})
        canceled = afa_col.count_documents({"status": "canceled", **base})
        refunded = afa_col.count_documents({"status": "refunded", **base})
        uncharged = afa_col.count_documents(_scope_query({"$or": [{"charged": {"$exists": False}}, {"charged": False}]}))
    except Exception:
        total = today_cnt = pending = processing = delivered = completed = failed = canceled = refunded = uncharged = 0

    return jsonify(
        success=True,
        data={
            "total": total,
            "today": today_cnt,
            "pending": pending,
            "processing": processing,
            "delivered": delivered,
            "completed": completed,
            "failed": failed,
            "rejected": failed,   # mirror key for front-end convenience
            "canceled": canceled,
            "refunded": refunded,
            "uncharged": uncharged,
        },
    )
    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401

    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401

    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401

    admin_oid = _admin_oid()
    if not admin_oid:
        return jsonify(success=False, error="Unauthorized"), 401
