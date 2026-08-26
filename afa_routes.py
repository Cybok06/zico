# afa_routes.py
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from db import db
from datetime import datetime, timedelta
from bson import ObjectId
import re
from afa_settings_utils import load_afa_admin_base_price, load_afa_base_price, load_afa_settings
from profit_ledger import apply_profit_split, normalize_profit_line, profit_totals
from tenant import current_admin_id_from_session, resolve_admin_id_for_user_id

print("[AFA_ROUTES_FILE_LOADED]", __file__)

afa_bp = Blueprint("afa", __name__)
afa_col = db["afa_registrations"]
users_col = db["users"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
orders_col = db["orders"]

PHONE_RE = re.compile(r"^0\d{9}$")  # 0xxxxxxxxx
DEFAULT_AFA_PRICE = 2.00

def _current_customer_ids():
    """Return (raw_id, [both string id and ObjectId (if valid)]) for querying."""
    raw = session.get("user_id") or session.get("customer_id")
    if not raw:
        return None, []
    ids = [raw]
    try:
        ids.append(ObjectId(raw))
    except Exception:
        pass
    return raw, ids


def _balance_user_candidates(user_id):
    out = []
    try:
        out.append(ObjectId(str(user_id)))
    except Exception:
        pass
    out.append(str(user_id))
    return out


def _json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _now():
    return datetime.utcnow()


def _is_main_admin(admin_id):
    try:
        return bool(users_col.find_one({"_id": admin_id, "role": "main_admin"}, {"_id": 1}))
    except Exception:
        return False


def _customer_balance_doc(customer_ids, admin_id):
    for customer_id in reversed(customer_ids or []):
        query = {"user_id": customer_id}
        if admin_id:
            query["admin_id"] = admin_id
        bal = balances_col.find_one(query)
        if bal:
            return bal
    for customer_id in reversed(customer_ids or []):
        bal = balances_col.find_one({"user_id": customer_id})
        if bal:
            return bal
    return None


def _afa_main_base_price() -> float:
    return round(float(load_afa_base_price(default=DEFAULT_AFA_PRICE) or DEFAULT_AFA_PRICE), 2)


def _afa_admin_base_price(admin_id) -> float:
    return round(float(load_afa_admin_base_price(admin_id, users_col, default=_afa_main_base_price()) or 0.0), 2)


def _afa_profit_line(reg, amount, admin_id):
    main_base_price = _afa_main_base_price()
    admin_base_price = _afa_admin_base_price(admin_id)
    if _is_main_admin(admin_id):
        admin_base_price = round(float(amount or 0.0), 2)
    line = {
        "phone": reg.get("phone"),
        "base_amount": admin_base_price,
        "main_base_amount": main_base_price,
        "admin_base_amount": admin_base_price,
        "selling_amount": round(float(amount or 0.0), 2),
        "amount": round(float(amount or 0.0), 2),
        "profit_amount": 0.0,
        "profit_percent_used": 0.0,
        "value": "AFA Registration",
        "value_obj": {"registration_id": str(reg.get("_id") or ""), "source": "customer_dashboard_afa"},
        "serviceId": "afa_registration",
        "serviceName": "AFA Registration",
        "service_type": "AFA",
        "line_status": "completed",
        "api_status": "not_applicable",
        "api_response": {"note": "AFA registration charged from customer dashboard."},
    }
    finalized = apply_profit_split(
        normalize_profit_line(
            line,
            selling_amount=round(float(amount or 0.0), 2),
            main_base_amount=main_base_price,
            admin_base_amount=admin_base_price,
        )
    )
    return finalized, profit_totals([finalized])


def _clear_dashboard_cache_safely():
    try:
        from admin_dashboard import clear_dashboard_cache

        clear_dashboard_cache()
    except Exception:
        pass

@afa_bp.route("/api/afa/register", methods=["POST"])
def afa_register():
    # require logged-in customer
    raw, ids = _current_customer_ids()
    if not raw:
        return jsonify(success=False, error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = re.sub(r"\D+", "", (data.get("phone") or ""))
    dob = (data.get("dob") or "").strip() or None
    location = (data.get("location") or "").strip() or None
    ghana_card = (data.get("ghana_card") or data.get("ghanaCard") or "").strip() or None

    if not name:
        return jsonify(success=False, error="Name is required"), 400
    if not PHONE_RE.match(phone):
        return jsonify(success=False, error="Phone must be 0xxxxxxxxx"), 400

    admin_id = current_admin_id_from_session(session)
    if not admin_id:
        admin_id = resolve_admin_id_for_user_id(users_col, raw)
    if not admin_id:
        return jsonify(success=False, error="Unable to resolve admin account"), 400

    afa_settings = load_afa_settings(admin_id, default_price=2.00)
    if not afa_settings.get("is_open", True):
        return jsonify(success=False, error="Service closed"), 400
    if not afa_settings.get("in_stock", True):
        return jsonify(success=False, error="Out of stock"), 400

    amount = round(float(afa_settings.get("price") or 0.0), 2)
    if amount <= 0:
        return jsonify(success=False, error="AFA registration price is not configured"), 400

    customer_id = ids[-1] if ids else raw
    customer_bal = _customer_balance_doc(ids, admin_id)
    if not customer_bal:
        return jsonify(success=False, error="Customer balance not found"), 404

    old_customer_amount = float(customer_bal.get("amount", 0.0))
    new_customer_amount = round(old_customer_amount - amount, 2)
    if new_customer_amount < 0:
        return jsonify(success=False, error="Insufficient wallet balance"), 400

    main_base_price = _afa_main_base_price()
    owner_is_main_admin = _is_main_admin(admin_id)
    admin_wallet_debit_total = 0.0
    if not owner_is_main_admin:
        admin_wallet_debit_total = _afa_admin_base_price(admin_id)
        if admin_wallet_debit_total <= 0:
            return jsonify(success=False, error="Admin AFA base price is not configured."), 400

    try:
        print("[afa_debit_price_debug]", {
            "session_role": session.get("role"),
            "session_user_id": str(session.get("user_id") or ""),
            "session_admin_id": str(session.get("admin_id") or ""),
            "resolved_admin_id": str(admin_id or ""),
            "selling_price": amount,
            "main_base_price": main_base_price,
            "admin_base_price": _afa_admin_base_price(admin_id),
            "admin_wallet_debit_total": admin_wallet_debit_total,
            "owner_is_main_admin": owner_is_main_admin,
        })
    except Exception:
        pass

    admin_bal = None
    old_admin_amount = 0.0
    new_admin_amount = 0.0
    if admin_wallet_debit_total > 0:
        admin_bal = balances_col.find_one({
            "user_id": {"$in": _balance_user_candidates(admin_id)}
        })
        if not admin_bal:
            return jsonify(success=False, error="Admin wallet balance not found"), 404
        old_admin_amount = float(admin_bal.get("amount", 0.0))
        new_admin_amount = round(old_admin_amount - admin_wallet_debit_total, 2)
        if new_admin_amount < 0:
            return jsonify(success=False, error=f"Insufficient admin wallet balance. Required GHS {admin_wallet_debit_total:.2f}."), 400

    now = _now()
    doc = {
        "customer_id": customer_id,   # prefer ObjectId if available
        "admin_id": admin_id,
        "name": name,
        "phone": phone,                            # digits only
        "dob": dob,
        "location": location,
        "ghana_card": ghana_card,
        "amount": amount,
        "status": "pending",
        "charged": True,
        "charged_amount": amount,
        "admin_wallet_debit_total": admin_wallet_debit_total,
        "agent_wallet_debit_total": amount,
        "wallet_debit_status": "completed",
        "charged_at": now,
        "charged_by": "customer_dashboard",
        "created_at": now,
        "updated_at": now,
    }
    res = afa_col.insert_one(doc)
    reg_id = res.inserted_id
    reg_doc = {**doc, "_id": reg_id}

    if admin_wallet_debit_total > 0:
        try:
            print("[afa_admin_wallet_debit_before]", {
                "admin_id": str(admin_id),
                "balance_doc_id": str(admin_bal.get("_id")) if admin_bal else "",
                "old_balance": old_admin_amount,
                "debit": admin_wallet_debit_total,
            })
        except Exception:
            pass
        admin_debit_res = balances_col.update_one(
            {
                "_id": admin_bal["_id"],
                "amount": {"$gte": admin_wallet_debit_total},
            },
            {
                "$inc": {"amount": -admin_wallet_debit_total},
                "$set": {"updated_at": now, "admin_id": admin_id},
            },
        )
        try:
            print("[afa_admin_wallet_debit_after]", {
                "admin_id": str(admin_id),
                "balance_doc_id": str(admin_bal["_id"]),
                "old_balance": old_admin_amount,
                "debit": admin_wallet_debit_total,
                "expected_new_balance": round(old_admin_amount - admin_wallet_debit_total, 2),
                "modified_count": admin_debit_res.modified_count,
            })
        except Exception:
            pass
        if admin_debit_res.modified_count != 1:
            return jsonify(success=False, error="Admin wallet debit failed."), 400

    balances_col.update_one(
        {"_id": customer_bal["_id"]},
        {"$set": {"amount": new_customer_amount, "updated_at": now, "admin_id": admin_id}},
    )
    customer_log = {
        "balance_id": customer_bal["_id"],
        "user_id": customer_bal["user_id"],
        "admin_id": admin_id,
        "action": "withdraw",
        "delta": -amount,
        "amount_before": old_customer_amount,
        "amount_after": new_customer_amount,
        "currency": customer_bal.get("currency", "GHS"),
        "note": f"AFA registration charge ({str(reg_id)})",
        "source": "customer_dashboard_afa",
        "labels": ["afa_registration_debit"],
        "created_at": now,
    }
    customer_log_id = balance_logs_col.insert_one(customer_log).inserted_id

    admin_log_id = None
    if admin_bal and admin_wallet_debit_total > 0:
        admin_log = {
            "balance_id": admin_bal["_id"],
            "user_id": admin_id,
            "admin_id": admin_id,
            "action": "purchase_debit",
            "delta": -admin_wallet_debit_total,
            "amount_before": old_admin_amount,
            "amount_after": new_admin_amount,
            "currency": admin_bal.get("currency", "GHS"),
            "note": f"AFA registration admin base debit ({str(reg_id)})",
            "source": "customer_dashboard_afa",
            "labels": ["admin_base_debit", "afa_registration_debit"],
            "created_at": now,
        }
        admin_log_id = balance_logs_col.insert_one(admin_log).inserted_id

    afa_col.update_one(
        {"_id": reg_id},
        {"$set": {"charge_log_id": customer_log_id, "admin_charge_log_id": admin_log_id}},
    )

    finalized_line, profit_split_totals = _afa_profit_line(reg_doc, amount, admin_id)
    order_id = f"AFA-{str(reg_id)}"
    wallet_debits = [
        {"user_id": customer_id, "amount": amount, "labels": ["afa_registration_debit"]},
    ]
    if admin_wallet_debit_total > 0:
        wallet_debits.append(
            {"user_id": admin_id, "amount": admin_wallet_debit_total, "labels": ["admin_base_debit", "afa_registration_debit"]}
        )
    orders_col.update_one(
        {"order_id": order_id},
        {
            "$setOnInsert": {
                "user_id": customer_id,
                "admin_id": admin_id,
                "wallet_owner_user_id": admin_id,
                "order_id": order_id,
                "items": [finalized_line],
                "total_amount": amount,
                "charged_amount": amount,
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "agent_wallet_debit_total": amount,
                "wallet_debit_status": "completed",
                "wallet_debits": wallet_debits,
                "profit_amount_total": profit_split_totals["profit_amount_total"],
                "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
                "admin_profit_total": profit_split_totals["admin_profit_total"],
                "store_profit_total": profit_split_totals["store_profit_total"],
                "status": "completed",
                "paid_from": "wallet",
                "kind": "afa_registration",
                "afa_registration_id": reg_id,
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    _clear_dashboard_cache_safely()

    return jsonify(
        success=True,
        id=str(reg_id),
        balance=new_customer_amount,
        admin_wallet_balance=new_admin_amount if admin_bal else None,
        admin_wallet_debit_total=admin_wallet_debit_total,
        agent_wallet_debit_total=amount,
        message="AFA registration submitted and charged. Status: pending.",
    )


@afa_bp.route("/admin/api/afa/<registration_id>/debug-debit", methods=["GET"])
def afa_debug_debit(registration_id):
    role = (session.get("role") or "").strip().lower()
    if role not in {"admin", "main_admin", "super_admin", "professional_admin", "super_professional"}:
        return jsonify(success=False, error="Unauthorized"), 401

    try:
        reg_obj_id = ObjectId(str(registration_id))
    except Exception:
        return jsonify(success=False, error="Invalid registration ID"), 400

    registration = afa_col.find_one({"_id": reg_obj_id})
    if not registration:
        return jsonify(success=False, error="Registration not found"), 404

    admin_id = registration.get("admin_id")
    customer_id = registration.get("customer_id")
    order = orders_col.find_one({
        "$or": [
            {"afa_registration_id": reg_obj_id},
            {"order_id": f"AFA-{registration_id}"},
        ]
    })

    admin_balance = None
    if admin_id:
        admin_balance = balances_col.find_one({
            "user_id": {"$in": _balance_user_candidates(admin_id)}
        })

    customer_balance = None
    if customer_id:
        customer_balance = balances_col.find_one({
            "user_id": {"$in": _balance_user_candidates(customer_id)}
        })

    admin_log = None
    if registration.get("admin_charge_log_id"):
        admin_log = balance_logs_col.find_one({"_id": registration.get("admin_charge_log_id")})
    if not admin_log and admin_id:
        admin_log = balance_logs_col.find_one(
            {
                "user_id": {"$in": _balance_user_candidates(admin_id)},
                "source": "customer_dashboard_afa",
                "labels": {"$all": ["admin_base_debit", "afa_registration_debit"]},
                "note": {"$regex": re.escape(str(reg_obj_id))},
            },
            sort=[("created_at", -1)],
        )

    customer_log = None
    if registration.get("charge_log_id"):
        customer_log = balance_logs_col.find_one({"_id": registration.get("charge_log_id")})
    if not customer_log and customer_id:
        customer_log = balance_logs_col.find_one(
            {
                "user_id": {"$in": _balance_user_candidates(customer_id)},
                "source": "customer_dashboard_afa",
                "labels": "afa_registration_debit",
                "note": {"$regex": re.escape(str(reg_obj_id))},
            },
            sort=[("created_at", -1)],
        )

    admin_wallet_debit_total = registration.get("admin_wallet_debit_total")
    if admin_wallet_debit_total in (None, "") and order:
        admin_wallet_debit_total = order.get("admin_wallet_debit_total")

    return jsonify(
        success=True,
        registration=_json_safe(registration),
        linked_order=_json_safe(order),
        admin_balance=_json_safe(admin_balance),
        customer_balance=_json_safe(customer_balance),
        admin_debit_balance_log=_json_safe(admin_log),
        customer_debit_balance_log=_json_safe(customer_log),
        admin_wallet_debit_total=round(float(admin_wallet_debit_total or 0.0), 2),
    )


@afa_bp.route("/api/afa/list", methods=["GET"])
def afa_list_api():
    raw, ids = _current_customer_ids()
    if not raw:
        return jsonify(success=False, error="Unauthorized"), 401

    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        page_size = int(request.args.get("page_size", 10))
    except Exception:
        page_size = 10
    page_size = max(1, min(page_size, 100))

    query = {"customer_id": {"$in": ids}} if ids else {"customer_id": raw}
    if status:
        query["status"] = status

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
    cursor = (afa_col.find(query)
              .sort([("created_at", -1)])
              .skip((page - 1) * page_size)
              .limit(page_size))

    items = []
    for d in cursor:
        created = d.get("created_at")
        items.append({
            "id": str(d.get("_id")),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "ghana_card": d.get("ghana_card"),
            "dob": d.get("dob"),
            "location": d.get("location"),
            "amount": float(d.get("amount", 0)),
            "status": (d.get("status") or "pending"),
            "created_at": created.isoformat() if created else None,
            "created_at_display": created.strftime("%d %b %Y, %I:%M %p") if created else ""
        })

    return jsonify(success=True, items=items, total=total, page=page, page_size=page_size)

@afa_bp.route("/customer/afa", methods=["GET"])
def afa_list_page():
    # gate with your login if needed
    if not (session.get("user_id") or session.get("customer_id")):
        return redirect(url_for("login.login"))
    return render_template("afa_list.html")
