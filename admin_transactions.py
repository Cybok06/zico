from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from db import db
from datetime import datetime, timedelta
import re
from tenant import current_admin_id_from_session

admin_transactions_bp = Blueprint("admin_transactions", __name__)

transactions_col = db["transactions"]
users_col = db["users"]
balance_logs_col = db["balance_logs"]


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


ADMIN_ROLES = ["admin", "main_admin", "super_admin", "superadmin", "professional_admin"]


def _to_oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _id_variants(value):
    oid = _to_oid(value)
    variants = []
    if oid:
        variants.append(oid)
        variants.append(str(oid))
    elif value:
        variants.append(value)
    return variants


def _add_query_clause(query, clause):
    if not clause:
        return
    if not query:
        query.update(clause)
        return
    if set(query.keys()) == {"$and"}:
        query["$and"].append(clause)
        return
    existing = dict(query)
    query.clear()
    query["$and"] = [existing, clause]


def _sub_admin_transaction_scope(admin_oid):
    admin_values = _id_variants(admin_oid)
    return {
        "$or": [
            {"admin_id": {"$in": admin_values}},
            {"user_id": {"$in": admin_values}},
            {"wallet_owner_user_id": {"$in": admin_values}},
            {"meta.wallet_owner_user_id": {"$in": admin_values}},
            {"meta.store_owner_id": {"$in": admin_values}},
        ]
    }


def _money(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _user_search_or(search_q: str):
    safe_q = re.escape(search_q)
    regex = {"$regex": safe_q, "$options": "i"}
    return [
        {"first_name": regex},
        {"last_name": regex},
        {"username": regex},
        {"email": regex},
        {"phone": regex},
    ]


@admin_transactions_bp.route("/admin/transactions")
def admin_view_transactions():
    # Auth
    if session.get("role") not in {"admin", "main_admin"}:
        return redirect(url_for("login.login"))

    selected_user_id = (request.args.get("admin") or request.args.get("customer") or "").strip()
    search_q = (request.args.get("q") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    range_preset = (request.args.get("range") or "").strip().lower()
    gateway = (request.args.get("gateway") or "").strip().lower()

    # pagination
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)

    per_page = 20

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = _is_main_admin()
    query = {}
    if admin_oid and not is_main_admin:
        _add_query_clause(query, _sub_admin_transaction_scope(admin_oid))

    admin_users = []
    admin_user_ids = []
    if is_main_admin:
        admin_user_query = {"role": {"$in": ADMIN_ROLES}}
        own_oid = _to_oid(session.get("user_id"))
        if own_oid:
            admin_user_query["_id"] = {"$ne": own_oid}
        if search_q:
            admin_user_query["$or"] = _user_search_or(search_q)
        admin_users = list(
            users_col.find(
                admin_user_query,
                {"first_name": 1, "last_name": 1, "username": 1, "email": 1, "phone": 1, "role": 1},
            ).sort("first_name", 1)
        )
        admin_user_ids = [u["_id"] for u in admin_users]
        query["user_id"] = {"$in": admin_user_ids}
        query["$and"] = [
            {
                "$or": [
                    {
                        "source": {
                            "$in": [
                                "admin_wallet",
                                "manual_topup",
                                "customer_dashboard_checkout",
                                "store_checkout",
                                "store_order",
                                "store_page_paystack",
                            ]
                        }
                    },
                    {"balance_log_id": {"$exists": True}},
                    {"type": {"$in": ["deposit", "withdraw", "purchase", "purchase_debit", "payment"]}},
                    {"meta.order_id": {"$exists": True}},
                    {"meta.store_checkout": True},
                ]
            }
        ]
    elif search_q:
        owner_query = {"role": {"$in": ["customer", "agent"]}}
        if admin_oid:
            owner_query["admin_id"] = {"$in": _id_variants(admin_oid)}
        owner_query["$or"] = _user_search_or(search_q)
        matched_owner_ids = [
            u["_id"]
            for u in users_col.find(owner_query, {"_id": 1})
            if u.get("_id")
        ]
        matched_owner_values = []
        for owner_id in matched_owner_ids:
            matched_owner_values.extend(_id_variants(owner_id))
        _add_query_clause(query, {"user_id": {"$in": matched_owner_values}})

    # Filter by selected admin/customer wallet owner
    if selected_user_id:
        oid = _to_oid(selected_user_id)
        if oid:
            _add_query_clause(query, {"user_id": {"$in": _id_variants(oid)}})
        else:
            flash("Invalid admin selected." if is_main_admin else "Invalid customer selected.", "warning")

    # Date range filter (verified_at)
    verified_filter = {}
    now = datetime.utcnow()
    start_dt = None
    end_dt = None

    if range_preset in ("today", "yesterday", "last7"):
        today = datetime(now.year, now.month, now.day)
        if range_preset == "today":
            start_dt = today
            end_dt = today + timedelta(days=1)
        elif range_preset == "yesterday":
            start_dt = today - timedelta(days=1)
            end_dt = today
        else:
            start_dt = today - timedelta(days=6)
            end_dt = today + timedelta(days=1)
    else:
        if start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except Exception:
                flash("Invalid start date.", "warning")
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            except Exception:
                flash("Invalid end date.", "warning")

    if start_dt:
        verified_filter["$gte"] = start_dt
    if end_dt:
        verified_filter["$lt"] = end_dt
    if verified_filter:
        _add_query_clause(
            query,
            {
                "$or": [
                    {"verified_at": verified_filter},
                    {"created_at": verified_filter},
                ]
            },
        )

    if gateway:
        gateway_regex = {"$regex": f"^{re.escape(gateway)}$", "$options": "i"}
        gateway_filter = {"$or": [{"gateway": gateway_regex}, {"source": gateway_regex}]}
        _add_query_clause(query, gateway_filter)

    # Count
    total_txns = transactions_col.count_documents(query)
    total_pages = max((total_txns + per_page - 1) // per_page, 1)

    # Clamp page to range (prevents dead pages when filters reduce results)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    # Fetch transactions
    transactions = list(
        transactions_col.find(query)
        .sort([("verified_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(per_page)
    )

    # Load wallet owners for dropdown
    if is_main_admin:
        owners = admin_users
    else:
        cust_q = {"role": {"$in": ["customer", "agent"]}}
        if admin_oid:
            cust_q["admin_id"] = {"$in": _id_variants(admin_oid)}
        if search_q:
            cust_q["$or"] = _user_search_or(search_q)
        owners = list(users_col.find(cust_q).sort("first_name", 1))
    gateways_raw = transactions_col.distinct("gateway", query)
    sources_raw = transactions_col.distinct("source", query)
    gateways = sorted({g for g in (gateways_raw + sources_raw) if g})

    # Attach user info efficiently
    user_ids = [t.get("user_id") for t in transactions if t.get("user_id")]
    users_map = {}
    if user_ids:
        user_lookup_ids = []
        for raw_user_id in user_ids:
            user_lookup_ids.extend(_id_variants(raw_user_id))
        for u in users_col.find({"_id": {"$in": list(set(user_lookup_ids))}}):
            users_map[u["_id"]] = u
            users_map[str(u["_id"])] = u

    log_ids = [t.get("balance_log_id") for t in transactions if t.get("balance_log_id")]
    logs_map = {}
    if log_ids:
        for lg in balance_logs_col.find({"_id": {"$in": list(set(log_ids))}}):
            logs_map[lg["_id"]] = lg

    for txn in transactions:
        user_doc = users_map.get(txn.get("user_id"), {}) or {}
        if not user_doc and (txn.get("source") == "store_order" or (txn.get("meta") or {}).get("store_checkout")):
            meta = txn.get("meta") or {}
            user_doc = {
                "first_name": "Store",
                "last_name": "Order",
                "phone": meta.get("payer_phone") or meta.get("customer_phone") or "N/A",
            }
        txn["user"] = user_doc
        log_doc = logs_map.get(txn.get("balance_log_id"), {}) or {}
        meta = txn.get("meta") or {}
        txn["amount_before_display"] = _money(txn.get("amount_before", meta.get("amount_before", log_doc.get("amount_before"))))
        txn["amount_after_display"] = _money(txn.get("amount_after", meta.get("amount_after", log_doc.get("amount_after"))))
        txn["actor_name_display"] = txn.get("actor_name") or meta.get("actor_name") or log_doc.get("actor_name") or "admin"
        labels = [str(x or "") for x in (meta.get("labels") or [])]
        action_display = meta.get("adjustment_action") or log_doc.get("action") or txn.get("type") or ""
        if txn.get("type") == "purchase_debit" or action_display == "purchase_debit":
            if "admin_base_debit" in labels:
                action_display = "order_admin_debit"
            elif "agent_purchase_debit" in labels:
                action_display = "order_agent_debit"
            else:
                action_display = "order_debit"
        elif txn.get("type") in {"purchase", "payment"}:
            source = str(txn.get("source") or "").lower()
            if source in {"store_order", "store_checkout"} or meta.get("store_checkout"):
                action_display = "store_order"
            else:
                action_display = "customer_order"
        txn["action_display"] = action_display
        if txn.get("type") == "purchase_debit":
            txn["note_display"] = txn.get("note") or log_doc.get("note") or f"Order wallet debit ({txn.get('reference') or 'N/A'})"
        else:
            txn["note_display"] = txn.get("note") or log_doc.get("note") or ""

    return render_template(
        "admin_transactions.html",
        transactions=transactions,
        owners=owners,
        customers=owners,
        selected_owner=selected_user_id,
        selected_customer=selected_user_id,
        search_q=search_q,
        start_date=start_date,
        end_date=end_date,
        selected_gateway=gateway,
        range_preset=range_preset,
        gateways=gateways,
        is_main_admin=is_main_admin,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
