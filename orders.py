# routes/orders.py
from flask import Blueprint, render_template, session, redirect, url_for, request
from bson import ObjectId, Regex
from db import db
from datetime import datetime, timedelta
import math
import re
from order_display import build_order_display_items, build_order_report_message
from tenant import resolve_admin_id_for_user_id

orders_bp = Blueprint("orders", __name__)
orders_col = db["orders"]
users_col = db["users"]
auth_pages_col = db["auth_pages"]
BOOSTING_PROVIDER = "exosupplier"

def _item_amount_total(items):
    total = 0.0
    for item in items:
        try:
            total += float(item.get("amount") or 0)
        except Exception:
            pass
    return round(total, 2)

def _parse_ymd(s: str):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")

def _wa_digits(raw: str | None) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "233" + digits[1:]
    return digits

def _admin_whatsapp_digits_for_customer(user_oid: ObjectId) -> str:
    admin_oid = None
    try:
        if session.get("admin_id"):
            admin_oid = ObjectId(session.get("admin_id"))
    except Exception:
        admin_oid = None
    if not admin_oid:
        admin_oid = resolve_admin_id_for_user_id(users_col, user_oid)
    if not admin_oid:
        return ""

    try:
        bdoc = auth_pages_col.find_one(
            {"admin_id": admin_oid},
            {"whatsapp": 1, "phone": 1},
        ) or {}
        admin_doc = users_col.find_one(
            {"_id": admin_oid},
            {"whatsapp": 1, "phone": 1},
        ) or {}
        return _wa_digits(
            bdoc.get("whatsapp")
            or admin_doc.get("whatsapp")
            or bdoc.get("phone")
            or admin_doc.get("phone")
        )
    except Exception:
        return ""

@orders_bp.route("/customer/orders")
def view_orders():
    # --- auth ---
    if session.get("role") not in {"customer", "agent"}:
        return redirect(url_for("login.login"))
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    # ----- Filters -----
    status       = (request.args.get("status") or "all").strip().lower()
    start_date_s = (request.args.get("start_date") or "").strip()
    end_date_s   = (request.args.get("end_date") or "").strip()
    order_id_q   = (request.args.get("order_id") or "").strip()
    phone_q      = (request.args.get("phone") or "").strip()

    # pagination
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    PER_PAGE = 10

    user_oid = ObjectId(user_id)

    # --- build query ---
    base_query = {
        "user_id": user_oid,
        "items": {"$elemMatch": {"provider": {"$ne": BOOSTING_PROVIDER}}},
    }
    if status and status != "all":
        base_query["status"] = status

    # Date range
    date_filter = {}
    try:
        if start_date_s:
            date_filter["$gte"] = _parse_ymd(start_date_s)
        if end_date_s:
            date_filter["$lt"] = _parse_ymd(end_date_s) + timedelta(days=1)
    except Exception:
        date_filter = {}
    if date_filter:
        base_query["created_at"] = date_filter

    # Order ID search (partial, case-insensitive)
    if order_id_q:
        base_query["order_id"] = Regex(order_id_q, "i")

    # Phone search (within items[].phone)
    if phone_q:
        base_query["items.phone"] = Regex(phone_q, "i")

    query = base_query

    # --- counts + page data ---
    total_count = orders_col.count_documents(query)
    total_pages = max(math.ceil(total_count / PER_PAGE), 1)
    if page > total_pages:
        page = total_pages

    # fetch current page
    cursor = (
        orders_col.find(query)
        .sort([("created_at", -1), ("_id", -1)])
        .skip((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    )
    orders = list(cursor)
    for order in orders:
        order["items"] = [
            item for item in (order.get("items") or [])
            if (item.get("provider") or "").strip().lower() != BOOSTING_PROVIDER
        ]
        order["total_amount"] = _item_amount_total(order["items"])
        order["display_items"] = build_order_display_items(order["items"])
        order["report_message"] = build_order_report_message(order.get("order_id") or "", order["display_items"])

    # status list for dropdown (prioritized order)
    available_statuses = orders_col.distinct(
        "status",
        {
            "user_id": user_oid,
            "items": {"$elemMatch": {"provider": {"$ne": BOOSTING_PROVIDER}}},
        },
    ) or []
    preferred = ["processing", "delivered", "failed", "refunded", "pending", "completed"]
    ordered_statuses = [s for s in preferred if s in available_statuses]
    for s in available_statuses:
        if s not in ordered_statuses:
            ordered_statuses.append(s)

    # Pagination window for template
    window = 2
    start = max(page - window, 1)
    end = min(page + window, total_pages)
    page_numbers = list(range(start, end + 1))

    return render_template(
        "orders.html",
        orders=orders,
        page=page,
        per_page=PER_PAGE,
        total_count=total_count,
        total_pages=total_pages,
        page_numbers=page_numbers,
        # echo filters
        status=status,
        start_date=start_date_s,
        end_date=end_date_s,
        order_id_q=order_id_q,
        phone_q=phone_q,
        statuses=ordered_statuses,
        admin_whatsapp_digits=_admin_whatsapp_digits_for_customer(user_oid),
    )
