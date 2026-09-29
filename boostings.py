from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from bson import ObjectId, Regex
from flask import Blueprint, redirect, render_template, request, session, url_for

from db import db
from tenant import current_admin_id_from_session, is_admin_role, to_object_id


boostings_bp = Blueprint("boostings", __name__)

orders_col = db["orders"]
users_col = db["users"]

BOOSTING_PROVIDER = "exosupplier"
ALLOWED_STATUSES = {"pending", "processing", "delivered", "failed", "completed", "refunded"}
ADMIN_EDITABLE_STATUSES = {"processing", "delivered", "failed"}
PER_PAGE = 20


def _parse_ymd(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except Exception:
        return None


def _date_range_filter(start_s: str, end_s: str) -> Dict[str, datetime]:
    date_filter: Dict[str, datetime] = {}
    start = _parse_ymd(start_s)
    end = _parse_ymd(end_s)
    if start:
        date_filter["$gte"] = start
    if end:
        date_filter["$lt"] = end + timedelta(days=1)
    return date_filter


def _display_user(user: Optional[dict]) -> str:
    if not user:
        return "N/A"
    name = " ".join(
        part.strip()
        for part in [str(user.get("first_name") or ""), str(user.get("last_name") or "")]
        if part and part.strip()
    )
    return (
        name
        or str(user.get("username") or "").strip()
        or str(user.get("email") or "").strip()
        or str(user.get("phone") or "").strip()
        or str(user.get("_id") or "N/A")
    )


def _load_user_map(ids: Iterable[Any]) -> Dict[ObjectId, dict]:
    object_ids = []
    for value in ids:
        oid = to_object_id(value)
        if oid:
            object_ids.append(oid)
    if not object_ids:
        return {}

    unique_ids = list({oid for oid in object_ids})
    docs = users_col.find(
        {"_id": {"$in": unique_ids}},
        {"first_name": 1, "last_name": 1, "username": 1, "email": 1, "phone": 1, "role": 1},
    )
    return {doc["_id"]: doc for doc in docs}


def _customer_ids_for_search(term: str, admin_oid: Optional[ObjectId] = None) -> List[ObjectId]:
    if not term:
        return []
    rx = Regex(term, "i")
    query: Dict[str, Any] = {
        "$or": [
            {"first_name": rx},
            {"last_name": rx},
            {"username": rx},
            {"email": rx},
            {"phone": rx},
        ]
    }
    if admin_oid:
        query["admin_id"] = admin_oid
    return [doc["_id"] for doc in users_col.find(query, {"_id": 1})]


def _build_order_match_for_admin(args) -> Dict[str, Any]:
    role = (session.get("role") or "").strip().lower()
    order_match: Dict[str, Any] = {}

    admin_oid = current_admin_id_from_session(session)
    if role != "main_admin" and admin_oid:
        order_match["admin_id"] = admin_oid

    start_date_s = (args.get("start_date") or "").strip()
    end_date_s = (args.get("end_date") or "").strip()
    date_filter = _date_range_filter(start_date_s, end_date_s)
    if date_filter:
        order_match["created_at"] = date_filter

    customer_q = (args.get("customer") or "").strip()
    if customer_q:
        customer_ids = _customer_ids_for_search(customer_q, admin_oid if role != "main_admin" else None)
        order_match["user_id"] = {"$in": customer_ids or []}

    return order_match


def _build_order_match_for_customer(args) -> Dict[str, Any]:
    user_oid = to_object_id(session.get("user_id"))
    order_match: Dict[str, Any] = {"user_id": user_oid} if user_oid else {"user_id": None}

    start_date_s = (args.get("start_date") or "").strip()
    end_date_s = (args.get("end_date") or "").strip()
    date_filter = _date_range_filter(start_date_s, end_date_s)
    if date_filter:
        order_match["created_at"] = date_filter

    return order_match


def _build_item_match(args) -> Dict[str, Any]:
    clauses: List[Dict[str, Any]] = [{"items.provider": BOOSTING_PROVIDER}]

    status = (args.get("status") or "all").strip().lower()
    if status and status != "all" and status in ALLOWED_STATUSES:
        clauses.append({"$or": [{"items.line_status": status}, {"status": status}]})

    search = (args.get("q") or "").strip()
    if search:
        rx = Regex(search, "i")
        clauses.append(
            {
                "$or": [
                    {"order_id": rx},
                    {"items.value": rx},
                    {"items.target_link": rx},
                    {"items.phone": rx},
                    {"items.social_media": rx},
                    {"items.category": rx},
                    {"items.provider_reference": rx},
                    {"items.provider_request_order_id": rx},
                    {"items.value_obj.social_media": rx},
                    {"items.value_obj.category": rx},
                ]
            }
        )

    platform = (args.get("platform") or "all").strip()
    if platform and platform.lower() != "all":
        rx = Regex(f"^{platform}$", "i")
        clauses.append({"$or": [{"items.social_media": rx}, {"items.value_obj.social_media": rx}]})

    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _aggregate_lines(order_match: Dict[str, Any], item_match: Dict[str, Any], page: int, per_page: int):
    skip = max(0, (page - 1) * per_page)
    pipeline = [
        {"$match": order_match},
        {"$unwind": {"path": "$items", "includeArrayIndex": "item_index"}},
        {"$match": item_match},
        {"$sort": {"created_at": -1, "_id": -1, "item_index": 1}},
        {
            "$facet": {
                "rows": [{"$skip": skip}, {"$limit": per_page}],
                "total": [{"$count": "count"}],
            }
        },
    ]
    result = list(orders_col.aggregate(pipeline))
    if not result:
        return [], 0
    rows = result[0].get("rows") or []
    total_rows = result[0].get("total") or []
    total = int(total_rows[0].get("count", 0)) if total_rows else 0
    return rows, total


def _status_options(order_match: Dict[str, Any]) -> List[str]:
    pipeline = [
        {"$match": order_match},
        {"$unwind": "$items"},
        {"$match": {"items.provider": BOOSTING_PROVIDER}},
        {
            "$group": {
                "_id": None,
                "line_statuses": {"$addToSet": "$items.line_status"},
                "order_statuses": {"$addToSet": "$status"},
            }
        },
    ]
    result = list(orders_col.aggregate(pipeline))
    values = set()
    if result:
        for status in (result[0].get("line_statuses") or []) + (result[0].get("order_statuses") or []):
            status = str(status or "").strip().lower()
            if status:
                values.add(status)

    preferred = ["pending", "processing", "delivered", "failed", "refunded", "completed"]
    ordered = [status for status in preferred if status in values]
    ordered.extend(sorted(status for status in values if status not in ordered))
    return ordered


def _platform_options(order_match: Dict[str, Any]) -> List[str]:
    pipeline = [
        {"$match": order_match},
        {"$unwind": "$items"},
        {"$match": {"items.provider": BOOSTING_PROVIDER}},
        {
            "$group": {
                "_id": None,
                "platforms": {"$addToSet": {"$ifNull": ["$items.social_media", "$items.value_obj.social_media"]}},
            }
        },
    ]
    result = list(orders_col.aggregate(pipeline))
    values = []
    if result:
        for platform in result[0].get("platforms") or []:
            platform = str(platform or "").strip()
            if platform:
                values.append(platform)
    return sorted(set(values), key=str.lower)


def _line_amount(item: dict, key: str) -> float:
    try:
        return float(item.get(key) or 0)
    except Exception:
        return 0.0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _build_line_rows(rows: List[dict], include_admin: bool = False) -> List[dict]:
    user_map = _load_user_map(row.get("user_id") for row in rows)
    admin_map = _load_user_map(row.get("admin_id") for row in rows) if include_admin else {}

    lines = []
    for row in rows:
        item = row.get("items") or {}
        value_obj = item.get("value_obj") if isinstance(item.get("value_obj"), dict) else {}
        user_oid = to_object_id(row.get("user_id"))
        admin_oid = to_object_id(row.get("admin_id"))
        target_link = (
            item.get("target_link")
            or item.get("phone")
            or value_obj.get("link")
            or ""
        )
        line_status = (item.get("line_status") or row.get("status") or "").strip().lower()
        lines.append(
            {
                "order_id": row.get("order_id") or str(row.get("_id")),
                "order_mongo_id": str(row.get("_id") or ""),
                "item_index": row.get("item_index", 0),
                "customer_name": _display_user(user_map.get(user_oid)),
                "customer_phone": (user_map.get(user_oid) or {}).get("phone") or "",
                "customer_email": (user_map.get(user_oid) or {}).get("email") or "",
                "admin_name": _display_user(admin_map.get(admin_oid)) if include_admin else "",
                "created_at": row.get("created_at"),
                "paid_from": row.get("paid_from") or "",
                "order_status": row.get("status") or "",
                "line_status": line_status,
                "api_status": item.get("api_status") or "",
                "service": item.get("value") or value_obj.get("name") or "Social Media Boosting",
                "platform": item.get("social_media") or value_obj.get("social_media") or "",
                "category": item.get("category") or value_obj.get("category") or "",
                "target_link": target_link,
                "quantity": item.get("quantity") or value_obj.get("quantity") or "",
                "amount": _line_amount(item, "amount"),
                "amount_usd": _line_amount(item, "amount_usd"),
                "base_amount": _line_amount(item, "base_amount"),
                "base_amount_usd": _line_amount(item, "base_amount_usd"),
                "profit_amount": _line_amount(item, "profit_amount"),
                "profit_amount_usd": _line_amount(item, "profit_amount_usd"),
                "rate_per_1000": _number(value_obj.get("rate_per_1000")),
                "rate_per_1000_usd": _number(value_obj.get("rate_per_1000_usd") or item.get("customer_rate_per_1000_usd")),
                "rate_per_1000_ghs": _number(value_obj.get("rate_per_1000_ghs") or item.get("customer_rate_per_1000_ghs") or value_obj.get("rate_per_1000")),
                "admin_rate_per_1000": _number(value_obj.get("admin_rate_per_1000")),
                "admin_rate_per_1000_usd": _number(value_obj.get("admin_rate_per_1000_usd") or item.get("admin_rate_per_1000_usd")),
                "admin_rate_per_1000_ghs": _number(value_obj.get("admin_rate_per_1000_ghs") or item.get("admin_rate_per_1000_ghs") or value_obj.get("admin_rate_per_1000")),
                "base_rate_per_1000": _number(value_obj.get("base_rate_per_1000")),
                "base_rate_per_1000_usd": _number(value_obj.get("base_rate_per_1000_usd") or item.get("base_rate_per_1000_usd")),
                "base_rate_per_1000_ghs": _number(value_obj.get("base_rate_per_1000_ghs") or item.get("base_rate_per_1000_ghs") or value_obj.get("base_rate_per_1000")),
                "usd_to_ghs_rate": item.get("usd_to_ghs_rate") or value_obj.get("usd_to_ghs_rate") or "",
                "provider_service_id": item.get("provider_service_id") or value_obj.get("provider_service_id") or "",
                "provider_order_id": item.get("provider_order_id") or "",
                "provider_reference": item.get("provider_reference") or "",
                "provider_request_order_id": item.get("provider_request_order_id") or "",
            }
        )
    return lines


def _pagination(page: int, total_count: int, per_page: int) -> tuple[int, int, List[int]]:
    total_pages = max(1, math.ceil(total_count / per_page))
    normalized_page = min(max(1, page), total_pages)
    start = max(1, normalized_page - 2)
    end = min(total_pages, normalized_page + 2)
    return normalized_page, total_pages, list(range(start, end + 1))


def _current_page() -> int:
    try:
        return max(1, int(request.args.get("page", 1)))
    except Exception:
        return 1


def _redirect_back_to_boostings():
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_url:
        return redirect(next_url)
    return redirect(url_for("boostings.admin_boostings"))


def _normalized_line_status(item: dict, order_status: Any = "") -> str:
    return str((item or {}).get("line_status") or order_status or "").strip().lower()


def _derive_order_status(items: List[dict], current_status: Any = "") -> str:
    statuses = [_normalized_line_status(item, current_status) for item in items if isinstance(item, dict)]
    statuses = [status for status in statuses if status]
    if not statuses:
        return str(current_status or "pending").strip().lower() or "pending"
    if any(status == "processing" for status in statuses):
        return "processing"
    if any(status == "pending" for status in statuses):
        return "processing"
    if all(status == "delivered" for status in statuses):
        return "delivered"
    if all(status == "failed" for status in statuses):
        return "failed"
    if any(status == "failed" for status in statuses):
        return "failed"
    return str(current_status or statuses[0]).strip().lower() or statuses[0]


@boostings_bp.route("/admin/boostings")
def admin_boostings():
    role = (session.get("role") or "").strip().lower()
    if not is_admin_role(role):
        return redirect(url_for("login.login"))

    requested_page = _current_page()
    page = requested_page
    order_match = _build_order_match_for_admin(request.args)
    item_match = _build_item_match(request.args)

    rows, total_count = _aggregate_lines(order_match, item_match, page, PER_PAGE)
    page, total_pages, page_numbers = _pagination(page, total_count, PER_PAGE)
    if page != requested_page:
        rows, total_count = _aggregate_lines(order_match, item_match, page, PER_PAGE)

    is_main_admin = role == "main_admin"
    lines = _build_line_rows(rows, include_admin=is_main_admin)

    return render_template(
        "boostings.html",
        page_mode="admin",
        title="Boostings",
        lines=lines,
        total_count=total_count,
        page=page,
        per_page=PER_PAGE,
        total_pages=total_pages,
        page_numbers=page_numbers,
        statuses=_status_options(order_match),
        platforms=_platform_options(order_match),
        status=(request.args.get("status") or "all").strip().lower(),
        platform=(request.args.get("platform") or "all").strip(),
        q=(request.args.get("q") or "").strip(),
        customer_q=(request.args.get("customer") or "").strip(),
        start_date=(request.args.get("start_date") or "").strip(),
        end_date=(request.args.get("end_date") or "").strip(),
        is_main_admin=is_main_admin,
    )


@boostings_bp.route("/admin/boostings/<order_id>/items/<int:item_index>/status", methods=["POST"])
def admin_update_boosting_status(order_id: str, item_index: int):
    role = (session.get("role") or "").strip().lower()
    if role != "main_admin":
        return _redirect_back_to_boostings()

    status = (request.form.get("status") or "").strip().lower()
    if status not in ADMIN_EDITABLE_STATUSES:
        return _redirect_back_to_boostings()

    order_oid = to_object_id(order_id)
    if not order_oid or item_index < 0:
        return _redirect_back_to_boostings()

    order = orders_col.find_one({"_id": order_oid}, {"items": 1, "status": 1})
    if not order:
        return _redirect_back_to_boostings()

    items = list(order.get("items") or [])
    if item_index >= len(items):
        return _redirect_back_to_boostings()

    item = items[item_index] if isinstance(items[item_index], dict) else {}
    if item.get("provider") != BOOSTING_PROVIDER:
        return _redirect_back_to_boostings()

    items[item_index] = {**item, "line_status": status, "updated_at": datetime.utcnow()}
    derived_status = _derive_order_status(items, order.get("status"))

    orders_col.update_one(
        {"_id": order_oid},
        {
            "$set": {
                "items": items,
                "status": derived_status,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return _redirect_back_to_boostings()


@boostings_bp.route("/customer/boostings")
def customer_boostings():
    if session.get("role") not in {"customer", "agent"}:
        return redirect(url_for("login.login"))
    if not session.get("user_id"):
        return redirect(url_for("login.login"))

    requested_page = _current_page()
    page = requested_page
    order_match = _build_order_match_for_customer(request.args)
    item_match = _build_item_match(request.args)

    rows, total_count = _aggregate_lines(order_match, item_match, page, PER_PAGE)
    page, total_pages, page_numbers = _pagination(page, total_count, PER_PAGE)
    if page != requested_page:
        rows, total_count = _aggregate_lines(order_match, item_match, page, PER_PAGE)

    lines = _build_line_rows(rows, include_admin=False)

    return render_template(
        "boostings.html",
        page_mode="customer",
        title="My Boostings",
        lines=lines,
        total_count=total_count,
        page=page,
        per_page=PER_PAGE,
        total_pages=total_pages,
        page_numbers=page_numbers,
        statuses=_status_options(order_match),
        platforms=_platform_options(order_match),
        status=(request.args.get("status") or "all").strip().lower(),
        platform=(request.args.get("platform") or "all").strip(),
        q=(request.args.get("q") or "").strip(),
        customer_q="",
        start_date=(request.args.get("start_date") or "").strip(),
        end_date=(request.args.get("end_date") or "").strip(),
        is_main_admin=False,
    )
