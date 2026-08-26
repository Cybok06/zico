from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from db import db
from bson import ObjectId
from typing import Dict, Any, List, Tuple, Optional, Union
from datetime import datetime, timedelta
from copy import deepcopy
from threading import RLock
from time import time
import os
import re
from withdraw_requests import update_withdraw_request_status
from tenant import current_admin_id_from_session
from announcements import get_popup_announcement
from admin_paystack_ledger import (
    evaluate_admin_wallet_low_balance,
    admin_paystack_balances_col,
    admin_paystack_payout_requests_col,
)

admin_dashboard_bp = Blueprint("admin_dashboard", __name__)

# Collections
orders_col = db["orders"]
users_col = db["users"]
balance_logs_col = db["balance_logs"]          # audit logs to compute deposits/deductions
balances_col = db["balances"]                  # for USER ACCOUNT BALANCE total
afa_col = db["afa_registrations"]
transactions_col = db["transactions"]          # for transaction KPIs
activity_logs_col = db["activity_logs"]

# ✅ Store withdrawal requests collection
store_withdraw_requests_col = db["store_withdraw_requests"]
store_accounts_col = db["store_accounts"]
bulk_sms_deliveries_col = db["bulk_sms_deliveries"]

_DASHBOARD_CACHE: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
_DASHBOARD_CACHE_LOCK = RLock()
_DASHBOARD_DEBUG = os.getenv("DASHBOARD_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
_DASHBOARD_INDEXES_READY = False


# ----------------------------
# Helpers
# ----------------------------


def _admin_oid():
    return current_admin_id_from_session(session)

def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"

def _dashboard_admin_scope() -> Dict[str, Any]:
    role = (session.get("role") or "").strip().lower()
    admin_oid = current_admin_id_from_session(session)

    if role == "main_admin":
        return {}

    if not admin_oid:
        try:
            uid = ObjectId(str(session.get("user_id")))
            user = users_col.find_one({"_id": uid}, {"_id": 1, "role": 1, "admin_id": 1})
            user_role = (user.get("role") or "").strip().lower() if user else ""
            if user and user_role in {"admin", "super_admin", "professional_admin", "superadmin"}:
                admin_oid = user["_id"]
            elif user and user.get("admin_id"):
                admin_oid = user["admin_id"]
        except Exception:
            admin_oid = None

    if not admin_oid:
        if _DASHBOARD_DEBUG:
            try:
                print("[dashboard_scope_missing]", {"role": role, "user_id": str(session.get("user_id") or "")})
            except Exception:
                pass
        return {"_id": {"$exists": False}}

    return {"admin_id": {"$in": [admin_oid, str(admin_oid)]}}


def _admin_match() -> Dict[str, Any]:
    return _dashboard_admin_scope()


_EXCLUDED_ORDER_STATUSES = ["skipped", "cancelled", "canceled", "failed", "refunded"]
_COUNTED_ORDER_STATUSES = ["pending", "processing", "delivered", "success", "completed", "paid"]
_COUNTED_PAID_FROM = ["from_account", "paystack_inline", "wallet", "Wallet", "paystack", "Paystack"]


def _dashboard_order_match(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    match = {**_dashboard_admin_scope(), "status": {"$nin": _EXCLUDED_ORDER_STATUSES}}
    if extra:
        match.update(extra)
    return match


def _sub_admin_user_query() -> Dict[str, Any]:
    query: Dict[str, Any] = {"role": "admin"}
    own_id = _admin_oid()
    if own_id:
        query["_id"] = {"$ne": own_id}
    return query


def _sub_admin_ids() -> List[ObjectId]:
    try:
        return [u["_id"] for u in users_col.find(_sub_admin_user_query(), {"_id": 1}) if isinstance(u.get("_id"), ObjectId)]
    except Exception:
        return []


def _admin_wallet_user_scope() -> Dict[str, Any]:
    if not _is_main_admin():
        return _admin_match()
    admin_ids = _sub_admin_ids()
    if not admin_ids:
        return {"user_id": {"$in": []}}
    return {"user_id": {"$in": admin_ids}}


def _dashboard_profit_field() -> str:
    return "main_admin_profit_total" if _is_main_admin() else "admin_profit_total"


def _if_null_chain(values: Tuple[Any, ...]) -> Any:
    if not values:
        return 0
    expr: Any = values[-1]
    for value in reversed(values[:-1]):
        expr = {"$ifNull": [value, expr]}
    return expr


def _convert_to_double_expr(input_expr: Any) -> Dict[str, Any]:
    return {
        "$convert": {
            "input": input_expr,
            "to": "double",
            "onError": 0,
            "onNull": 0,
        }
    }


def _money_expr(*fields: Any) -> Dict[str, Any]:
    refs: List[Any] = []
    for field in fields:
        if not field:
            continue
        if isinstance(field, str):
            refs.append(field if field.startswith("$") else f"${field}")
        else:
            refs.append(field)
    refs.append(0)
    return _convert_to_double_expr(_if_null_chain(tuple(refs)))


def _charged_amount_expr() -> Dict[str, Any]:
    return _money_expr("charged_amount", "total_amount", "amount")


def _total_amount_expr() -> Dict[str, Any]:
    return _money_expr("total_amount", "charged_amount", "amount")


def _dashboard_profit_expr() -> Dict[str, Any]:
    # Split orders historically saved role-specific order totals as zero because
    # the splitter read `*_profit_amount` while normalized lines use
    # `main_admin_profit` and `admin_profit`. Prefer the item-level split so both
    # old and new orders follow the actual pricing layers.
    if _is_main_admin():
        line_profit = _money_expr(
            "$$line.main_admin_profit",
            "$$line.main_admin_profit_amount",
            {
                "$max": [
                    0,
                    {
                        "$subtract": [
                            _money_expr("$$line.admin_base_amount", "$$line.base_amount"),
                            _money_expr("$$line.main_base_amount", "$$line.admin_base_amount", "$$line.base_amount"),
                        ]
                    },
                ]
            },
        )
    else:
        calculated_admin_profit = {
            "$max": [
                0,
                {
                    "$subtract": [
                        _money_expr("$$line.store_owner_base_amount", "$$line.selling_amount", "$$line.amount"),
                        _money_expr("$$line.admin_base_amount", "$$line.base_amount"),
                    ]
                },
            ]
        }
        line_profit = _money_expr(
            "$$line.admin_profit",
            "$$line.admin_profit_amount",
            calculated_admin_profit,
            "$$line.profit_amount",
        )

    safe_items = {"$cond": [{"$isArray": "$items"}, "$items", []]}
    item_total = {
        "$sum": {
            "$map": {
                "input": safe_items,
                "as": "line",
                "in": line_profit,
            }
        }
    }
    return {
        "$cond": [
            {"$gt": [{"$size": safe_items}, 0]},
            item_total,
            _money_expr(_dashboard_profit_field(), "profit_amount_total"),
        ]
    }


def _log_dashboard_agg_error(event: str, exc: Exception, **extra: Any) -> None:
    if not _DASHBOARD_DEBUG:
        return
    try:
        print(f"[{event}]", {
            "error": str(exc),
            "role": session.get("role"),
            "user_id": str(session.get("user_id") or ""),
            "admin_id": str(session.get("admin_id") or ""),
            "admin_oid": str(_admin_oid() or ""),
            "scope": str(_dashboard_admin_scope()),
            **extra,
        })
    except Exception:
        pass


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _num(value: Any) -> float:
    return safe_float(value)


def _order_profit_value(order: Dict[str, Any]) -> float:
    items = order.get("items") or []
    if isinstance(items, list) and items:
        if _is_main_admin():
            return round(sum(
                safe_float(line.get("main_admin_profit"))
                if line.get("main_admin_profit") not in (None, "")
                else max(
                    0.0,
                    safe_float(line.get("admin_base_amount") or line.get("base_amount"))
                    - safe_float(line.get("main_base_amount") or line.get("admin_base_amount") or line.get("base_amount")),
                )
                for line in items
            ), 2)
        return round(sum(
            safe_float(line.get("admin_profit"))
            if line.get("admin_profit") not in (None, "")
            else max(
                0.0,
                safe_float(line.get("store_owner_base_amount") or line.get("selling_amount") or line.get("amount"))
                - safe_float(line.get("admin_base_amount") or line.get("base_amount")),
            )
            for line in items
        ), 2)

    return safe_float(order.get(_dashboard_profit_field()) or order.get("profit_amount_total"))


def _dashboard_cache_key(*parts: Any) -> Tuple[Any, ...]:
    admin_oid = _admin_oid()
    role = (session.get("role") or "").strip().lower()
    return ("admin_dashboard", role, str(admin_oid) if admin_oid else "", *parts)


def _cached_copy(key: Tuple[Any, ...], ttl_seconds: float, loader):
    now = time()
    ttl_seconds = max(1.0, float(ttl_seconds or 1))
    with _DASHBOARD_CACHE_LOCK:
        entry = _DASHBOARD_CACHE.get(key)
        if entry and float(entry.get("expires_at") or 0) > now:
            return deepcopy(entry.get("value"))
        if entry:
            _DASHBOARD_CACHE.pop(key, None)
    value = loader()
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE[key] = {
            "expires_at": now + ttl_seconds,
            "value": deepcopy(value),
        }
    return deepcopy(value)


def ensure_dashboard_indexes() -> None:
    """Indexes used by the async dashboard API loaders. Safe to call repeatedly."""
    global _DASHBOARD_INDEXES_READY
    if _DASHBOARD_INDEXES_READY:
        return
    specs = [
        (orders_col, [("admin_id", 1), ("created_at", -1)], "dash_orders_admin_created"),
        (orders_col, [("admin_id", 1), ("status", 1), ("created_at", -1)], "dash_orders_admin_status_created"),
        (orders_col, [("status", 1), ("created_at", -1)], "dash_orders_status_created"),
        (orders_col, [("paid_from", 1)], "dash_orders_paid_from"),
        (orders_col, [("user_id", 1)], "dash_orders_user"),
        (orders_col, [("created_at", -1)], "dash_orders_created"),
        (users_col, [("role", 1)], "dash_users_role"),
        (users_col, [("admin_id", 1), ("role", 1)], "dash_users_admin_role"),
        (balance_logs_col, [("user_id", 1), ("action", 1), ("created_at", -1)], "dash_balance_logs_user_action_created"),
        (balance_logs_col, [("action", 1), ("created_at", -1)], "dash_balance_logs_action_created"),
        (balances_col, [("user_id", 1)], "dash_balances_user"),
        (store_withdraw_requests_col, [("admin_id", 1), ("status", 1), ("created_at", -1)], "dash_withdraw_admin_status_created"),
        (store_withdraw_requests_col, [("status", 1), ("created_at", -1)], "dash_withdraw_status_created"),
        (afa_col, [("admin_id", 1), ("status", 1), ("created_at", -1)], "dash_afa_admin_status_created"),
        (afa_col, [("status", 1), ("created_at", -1)], "dash_afa_status_created"),
        (transactions_col, [("admin_id", 1), ("status", 1), ("created_at", -1)], "dash_tx_admin_status_created"),
        (transactions_col, [("status", 1), ("created_at", -1)], "dash_tx_status_created"),
        (transactions_col, [("gateway", 1)], "dash_tx_gateway"),
        (transactions_col, [("source", 1)], "dash_tx_source"),
        (bulk_sms_deliveries_col, [("admin_id", 1), ("delivery_status", 1), ("created_at", -1)], "dash_sms_admin_delivery_created"),
        (bulk_sms_deliveries_col, [("delivery_status", 1), ("delivered_at", -1)], "dash_sms_delivery_delivered"),
    ]
    for collection, keys, name in specs:
        try:
            collection.create_index(keys, name=name, background=True)
        except Exception as exc:
            _log_dashboard_agg_error("dashboard_index_error", exc, index=name)
    _DASHBOARD_INDEXES_READY = True


ensure_dashboard_indexes()


def clear_dashboard_cache():
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE.clear()


def _users_display_map(user_ids: List[ObjectId]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not user_ids:
        return out
    try:
        for u in users_col.find({"_id": {"$in": user_ids}}, {"username": 1, "name": 1, "phone": 1}):
            disp = (u.get("username") or u.get("name") or u.get("phone") or "").strip()
            if not disp:
                disp = f"User {str(u['_id'])[:6].upper()}"
            out[str(u["_id"])] = disp
    except Exception:
        pass
    return out

def _admins_display_map(admin_ids: List[ObjectId]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not admin_ids:
        return out
    try:
        for u in users_col.find(
            {"_id": {"$in": admin_ids}},
            {"first_name": 1, "last_name": 1, "username": 1, "email": 1}
        ):
            name = (f"{u.get('first_name','')} {u.get('last_name','')}".strip() or u.get("username") or u.get("email") or "")
            if not name:
                name = f"Admin {str(u['_id'])[:6].upper()}"
            out[str(u["_id"])] = name
    except Exception:
        pass
    return out


def _normalize_customer_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return "0" + digits[3:]
    if digits.startswith("0") and len(digits) == 10:
        return digits
    if len(digits) == 9:
        return "0" + digits
    return digits


def _order_customer_phones(order: Dict[str, Any]) -> List[str]:
    phones: List[str] = []
    for key in ("phone", "customer_phone", "payer_phone", "msisdn"):
        phone = _normalize_customer_phone(order.get(key))
        if phone:
            phones.append(phone)

    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        phone = _normalize_customer_phone(item.get("phone") or item.get("recipient") or item.get("msisdn"))
        if phone:
            phones.append(phone)
        for recipient in item.get("recipients") or []:
            if isinstance(recipient, dict):
                phone = _normalize_customer_phone(recipient.get("number") or recipient.get("phone"))
            else:
                phone = _normalize_customer_phone(recipient)
            if phone:
                phones.append(phone)
    return phones


def top_admins_by_sales(limit: int = 10) -> Tuple[List[str], List[float], List[Dict[str, Any]]]:
    pipeline = [
        {"$match": _dashboard_order_match({"admin_id": {"$ne": None}})},
        {"$group": {
            "_id": "$admin_id",
            "order_count": {"$sum": 1},
            "sales_sum": {"$sum": _charged_amount_expr()}
        }},
        {"$sort": {"sales_sum": -1}},
        {"$limit": int(limit)},
    ]
    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    admin_ids = [doc.get("_id") for doc in agg if isinstance(doc.get("_id"), ObjectId)]
    admin_map = _admins_display_map(admin_ids)

    labels: List[str] = []
    values: List[float] = []
    rows: List[Dict[str, Any]] = []
    for doc in agg:
        aid = doc.get("_id")
        name = admin_map.get(str(aid), f"Admin {str(aid)[:6].upper()}" if aid else "Admin")
        sales = float(doc.get("sales_sum", 0) or 0)
        orders = int(doc.get("order_count", 0) or 0)
        labels.append(name)
        values.append(round(sales, 2))
        rows.append({"admin_id": str(aid), "admin": name, "sales": round(sales, 2), "orders": orders})
    return labels, values, rows


def top_admins_by_orders(limit: int = 10) -> Tuple[List[str], List[int]]:
    pipeline = [
        {"$match": _dashboard_order_match({"admin_id": {"$ne": None}})},
        {"$group": {"_id": "$admin_id", "order_count": {"$sum": 1}}},
        {"$sort": {"order_count": -1}},
        {"$limit": int(limit)},
    ]
    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    admin_ids = [doc.get("_id") for doc in agg if isinstance(doc.get("_id"), ObjectId)]
    admin_map = _admins_display_map(admin_ids)

    labels: List[str] = []
    values: List[int] = []
    for doc in agg:
        aid = doc.get("_id")
        name = admin_map.get(str(aid), f"Admin {str(aid)[:6].upper()}" if aid else "Admin")
        labels.append(name)
        values.append(int(doc.get("order_count", 0) or 0))
    return labels, values


def recent_activities(limit: int = 12) -> List[Dict[str, Any]]:
    if not _is_main_admin():
        return []
    try:
        docs = list(activity_logs_col.find({}, sort=[("created_at", -1)], limit=int(limit)))
    except Exception:
        docs = []

    admin_ids = [d.get("admin_id") for d in docs if isinstance(d.get("admin_id"), ObjectId)]
    admin_map = _admins_display_map(admin_ids)

    out: List[Dict[str, Any]] = []
    for d in docs:
        aid = d.get("admin_id")
        out.append({
            "actor": d.get("actor_name") or "User",
            "role": (d.get("actor_role") or "user").replace("_", " "),
            "action": (d.get("action") or "activity").replace("_", " "),
            "target": (d.get("target_type") or "").replace("_", " "),
            "message": d.get("message") or "",
            "admin": admin_map.get(str(aid), "") if aid else "",
            "created_at": d.get("created_at"),
        })
    return out


def top_customers_by_orders(limit: int = 10) -> Tuple[List[str], List[int]]:
    pipeline = [
        {"$match": _dashboard_order_match({"user_id": {"$ne": None}})},
        {"$group": {"_id": "$user_id", "order_count": {"$sum": 1}}},
        {"$sort": {"order_count": -1}},
        {"$limit": int(limit)},
    ]
    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    obj_ids = [oid for oid in (doc.get("_id") for doc in agg) if isinstance(oid, ObjectId)]
    users_map = _users_display_map(obj_ids)

    labels: List[str] = []
    values: List[int] = []
    for doc in agg:
        uid = doc.get("_id")
        count = int(doc.get("order_count", 0) or 0)
        if isinstance(uid, ObjectId):
            label = users_map.get(str(uid), f"User {str(uid)[:6].upper()}")
        else:
            label = "Unknown"
        labels.append(label)
        values.append(count)
    return labels, values


def top_customers_by_profit(limit: int = 10) -> Tuple[List[str], List[float]]:
    pipeline = [
        {"$match": _dashboard_order_match({"user_id": {"$ne": None}})},
        {"$group": {
            "_id": "$user_id",
            "profit_sum": {"$sum": _dashboard_profit_expr()}
        }},
        {"$sort": {"profit_sum": -1}},
        {"$limit": int(limit)},
    ]
    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    obj_ids = [oid for oid in (doc.get("_id") for doc in agg) if isinstance(oid, ObjectId)]
    users_map = _users_display_map(obj_ids)

    labels: List[str] = []
    values: List[float] = []
    for doc in agg:
        uid = doc.get("_id")
        profit = float(doc.get("profit_sum", 0) or 0)
        if isinstance(uid, ObjectId):
            label = users_map.get(str(uid), f"User {str(uid)[:6].upper()}")
        else:
            label = "Unknown"
        labels.append(label)
        values.append(profit)
    return labels, values


# ✅ FIXED FOREVER: Top offers purchased (safe pipeline; no bracket chaos)
def top_offers_by_purchases(limit: int = 10) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {"$match": _dashboard_order_match()},
        {"$unwind": "$items"},

        {"$addFields": {
            "service": {"$ifNull": ["$items.serviceName", "Unknown"]},
            "offer_label": {"$ifNull": ["$items.value_obj.label", None]},
            "offer_volume": {"$ifNull": ["$items.value_obj.volume", None]},
            "offer_id": {"$ifNull": ["$items.value_obj.id", None]},
            "offer_value": {"$ifNull": ["$items.value", None]},
            "offer_bundle": {"$ifNull": ["$items.shared_bundle", None]},
        }},

        {"$addFields": {
            "offer_raw": {
                "$ifNull": [
                    {"$cond": [{"$and": [{"$ne": ["$offer_label", None]}, {"$ne": ["$offer_label", ""]}]}, "$offer_label", None]},
                    {"$ifNull": [
                        {"$cond": [{"$and": [{"$ne": ["$offer_volume", None]}, {"$ne": ["$offer_volume", ""]}]}, "$offer_volume", None]},
                        {"$ifNull": [
                            {"$cond": [{"$and": [{"$ne": ["$offer_id", None]}, {"$ne": ["$offer_id", ""]}]}, "$offer_id", None]},
                            {"$ifNull": [
                                {"$cond": [{"$and": [{"$ne": ["$offer_value", None]}, {"$ne": ["$offer_value", ""]}]}, "$offer_value", None]},
                                {"$ifNull": ["$offer_bundle", "N/A"]}
                            ]}
                        ]}
                    ]}
                ]
            }
        }},

        {"$addFields": {"offer": {"$toString": "$offer_raw"}}},

        {"$group": {"_id": {"service": "$service", "offer": "$offer"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": int(limit)},
    ]

    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    results: List[Dict[str, Any]] = []
    for doc in agg:
        _id = doc.get("_id") or {}
        results.append({
            "service": (_id.get("service") or "Unknown") or "Unknown",
            "offer": (_id.get("offer") or "N/A"),
            "count": int(doc.get("count", 0) or 0),
        })
    return results


def compute_totals() -> Dict[str, Any]:
    match = _dashboard_order_match()
    try:
        doc = next(orders_col.aggregate([
            {"$match": match},
            {"$group": {
                "_id": None,
                "sum_total_amount": {"$sum": _total_amount_expr()},
                "sum_charged_amount": {"$sum": _charged_amount_expr()},
                "sum_profit_amount": {"$sum": _dashboard_profit_expr()},
                "matched_order_count": {"$sum": 1},
            }},
        ]), None)
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_compute_totals_error", exc, match=match)
        doc = None

    return {
        "sum_total_amount": round(float((doc or {}).get("sum_total_amount", 0) or 0.0), 2),
        "sum_charged_amount": round(float((doc or {}).get("sum_charged_amount", 0) or 0.0), 2),
        "sum_profit_amount": round(float((doc or {}).get("sum_profit_amount", 0) or 0.0), 2),
        "matched_order_count": int((doc or {}).get("matched_order_count", 0) or 0),
    }


def compute_customer_counts() -> Dict[str, int]:
    unique_phones = set()
    missing_phone_user_ids = set()
    try:
        for order in orders_col.find(_dashboard_order_match(), {"items": 1, "phone": 1, "customer_phone": 1, "payer_phone": 1, "msisdn": 1, "user_id": 1}):
            phones = _order_customer_phones(order)
            if phones:
                unique_phones.update(phones)
            else:
                uid = order.get("user_id")
                if isinstance(uid, ObjectId):
                    missing_phone_user_ids.add(uid)
                elif uid:
                    try:
                        missing_phone_user_ids.add(ObjectId(str(uid)))
                    except Exception:
                        pass
    except Exception:
        unique_phones = set()
        missing_phone_user_ids = set()

    if missing_phone_user_ids:
        try:
            for user in users_col.find({"_id": {"$in": list(missing_phone_user_ids)}}, {"phone": 1, "whatsapp": 1}):
                phone = _normalize_customer_phone(user.get("phone") or user.get("whatsapp"))
                if phone:
                    unique_phones.add(phone)
        except Exception:
            pass

    total_customers = len(unique_phones)
    return {
        "total_customers": int(total_customers),
        "blocked_customers": 0,
        "active_customers": int(total_customers),
    }


def compute_agent_counts() -> Dict[str, int]:
    conditions: List[Dict[str, Any]] = [{"role": "agent"}]
    admin_oid = _admin_oid()
    if admin_oid and not _is_main_admin():
        conditions.append({"admin_id": admin_oid})
    conditions.append({"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]})

    base_query = {"$and": conditions} if len(conditions) > 1 else conditions[0]
    blocked_query = {"$and": [base_query, {"status": "blocked"}]}
    pending_query = {"$and": [base_query, {"status": "pending", "approval_status": "pending"}]}
    active_query = {"$and": [base_query, {"$or": [{"status": "active"}, {"status": {"$exists": False}}]}]}

    try:
        total_agents = int(users_col.count_documents(base_query))
    except Exception:
        total_agents = 0
    try:
        blocked_agents = int(users_col.count_documents(blocked_query))
    except Exception:
        blocked_agents = 0
    try:
        pending_agents = int(users_col.count_documents(pending_query))
    except Exception:
        pending_agents = 0
    try:
        active_agents = int(users_col.count_documents(active_query))
    except Exception:
        active_agents = 0

    return {
        "total_agents": total_agents,
        "blocked_agents": blocked_agents,
        "pending_agents": pending_agents,
        "active_agents": active_agents,
    }


def compute_balance_flow_totals() -> Dict[str, float]:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    def _sum(pipeline: List[Dict[str, Any]]) -> float:
        try:
            doc = next(balance_logs_col.aggregate(pipeline), None)
            return float((doc or {}).get("total", 0) or 0)
        except Exception:
            return 0.0

    scope = _admin_wallet_user_scope()
    deposits_overall = _sum([
        {"$match": {"action": "deposit", **scope}},
        {"$group": {"_id": None, "total": {"$sum": {"$convert": {"input": "$delta", "to": "double", "onError": 0, "onNull": 0}}}}}
    ])

    withdrawals_overall = _sum([
        {"$match": {"action": "withdraw", **scope}},
        {"$group": {"_id": None, "total": {"$sum": {"$abs": {"$convert": {"input": "$delta", "to": "double", "onError": 0, "onNull": 0}}}}}}
    ])

    deposits_today = _sum([
        {"$match": {"action": "deposit", "created_at": {"$gte": start, "$lt": end}, **scope}},
        {"$group": {"_id": None, "total": {"$sum": {"$convert": {"input": "$delta", "to": "double", "onError": 0, "onNull": 0}}}}}
    ])

    withdrawals_today = _sum([
        {"$match": {"action": "withdraw", "created_at": {"$gte": start, "$lt": end}, **scope}},
        {"$group": {"_id": None, "total": {"$sum": {"$abs": {"$convert": {"input": "$delta", "to": "double", "onError": 0, "onNull": 0}}}}}}
    ])

    return {
        "deposits_overall": deposits_overall,
        "withdrawals_overall": withdrawals_overall,
        "deposits_today": deposits_today,
        "withdrawals_today": withdrawals_today,
    }


def compute_transaction_kpis() -> Dict[str, float]:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    base_match = _dashboard_order_match({
        "status": {"$in": _COUNTED_ORDER_STATUSES},
        "$or": [
            {"paid_from": {"$in": _COUNTED_PAID_FROM}},
            {"wallet_debit_status": "completed"},
            {"charged_amount": {"$gt": 0}},
        ],
    })
    amt_expr = _charged_amount_expr()
    today_amount_expr = {
        "$cond": [
            {"$and": [{"$gte": ["$created_at", start]}, {"$lt": ["$created_at", end]}]},
            amt_expr,
            0,
        ]
    }
    today_count_expr = {
        "$cond": [
            {"$and": [{"$gte": ["$created_at", start]}, {"$lt": ["$created_at", end]}]},
            1,
            0,
        ]
    }

    try:
        doc = next(orders_col.aggregate([
            {"$match": base_match},
            {"$group": {
                "_id": None,
                "txn_total_count": {"$sum": 1},
                "txn_total_amount": {"$sum": amt_expr},
                "txn_today_count": {"$sum": today_count_expr},
                "txn_today_amount": {"$sum": today_amount_expr},
            }},
        ]), None)
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_txn_kpis_error", exc, match=base_match)
        doc = None

    return {
        "txn_total_count": int((doc or {}).get("txn_total_count", 0) or 0),
        "txn_today_count": int((doc or {}).get("txn_today_count", 0) or 0),
        "txn_total_amount": float((doc or {}).get("txn_total_amount", 0) or 0.0),
        "txn_today_amount": float((doc or {}).get("txn_today_amount", 0) or 0.0),
    }


def compute_bulk_sms_kpis() -> Dict[str, Union[int, float]]:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    scope = _dashboard_admin_scope()
    order_scope = _dashboard_order_match()
    base_match: Dict[str, Any] = {"delivery_status": "delivered", **scope}
    today_match = {
        **base_match,
        "$or": [
            {"delivered_at": {"$gte": start, "$lt": end}},
            {"delivered_at": {"$exists": False}, "created_at": {"$gte": start, "$lt": end}},
        ],
    }
    profit_expr = {
        "$convert": {
            "input": {
                "$ifNull": [
                    "$main_admin_profit_amount" if _is_main_admin() else "$admin_profit_amount",
                    {"$ifNull": ["$profit_amount_total", 0]},
                ]
            },
            "to": "double",
            "onError": 0,
            "onNull": 0,
        }
    }

    def _agg(match: Dict[str, Any]) -> Dict[str, Union[int, float]]:
        try:
            doc = next(bulk_sms_deliveries_col.aggregate([
                {"$match": match},
                {"$group": {
                    "_id": None,
                    "delivery_count": {"$sum": 1},
                    "sms_count": {"$sum": {"$convert": {"input": {"$ifNull": ["$recipient_count", 0]}, "to": "int", "onError": 0, "onNull": 0}}},
                    "profit": {"$sum": profit_expr},
                }},
            ]), None)
        except Exception:
            doc = None
        return {
            "delivery_count": int((doc or {}).get("delivery_count", 0) or 0),
            "sms_count": int((doc or {}).get("sms_count", 0) or 0),
            "profit": round(float((doc or {}).get("profit", 0) or 0.0), 2),
        }

    today_stats = _agg(today_match)
    all_stats = _agg(base_match)
    return {
        "sms_delivered_today": today_stats["sms_count"],
        "sms_delivery_orders_today": today_stats["delivery_count"],
        "sms_profit_today": today_stats["profit"],
        "sms_delivered_total": all_stats["sms_count"],
        "sms_profit_total": all_stats["profit"],
    }


def compute_user_balances_summary() -> Dict[str, Union[float, int]]:
    try:
        doc = next(balances_col.aggregate([
            {"$match": _admin_wallet_user_scope()},
            {"$group": {
                "_id": None,
                "total_balance_amount": {"$sum": {"$convert": {"input": "$amount", "to": "double", "onError": 0, "onNull": 0}}},
                "doc_count": {"$sum": 1},
                "positive_count": {"$sum": {"$cond": [
                    {"$gt": [{"$convert": {"input": "$amount", "to": "double", "onError": 0, "onNull": 0}}, 0]}, 1, 0
                ]}}
            }}
        ]), None)
    except Exception:
        doc = None
    return {
        "total_balance_amount": float((doc or {}).get("total_balance_amount", 0) or 0.0),
        "balance_doc_count": int((doc or {}).get("doc_count", 0) or 0),
        "positive_balance_count": int((doc or {}).get("positive_count", 0) or 0),
    }


def compute_platform_admin_counts() -> Dict[str, int]:
    scope = _admin_match()
    try:
        if _is_main_admin():
            total_admins = users_col.count_documents(_sub_admin_user_query())
            total_agents = users_col.count_documents({"role": "agent"})
        else:
            total_admins = 0
            total_agents = users_col.count_documents({"role": "agent", **scope})
    except Exception:
        total_admins = total_agents = 0
    return {
        "total_admins": int(total_admins),
        "total_agents": int(total_agents),
    }


def compute_paystack_payout_summary() -> Dict[str, Union[float, int]]:
    balance_match: Dict[str, Any] = {}
    if _is_main_admin():
        admin_ids = _sub_admin_ids()
        balance_match["admin_id"] = {"$in": admin_ids} if admin_ids else {"$in": []}
    else:
        admin_oid = _admin_oid()
        balance_match["admin_id"] = admin_oid if admin_oid else None

    try:
        bal_doc = next(admin_paystack_balances_col.aggregate([
            {"$match": balance_match},
            {"$group": {
                "_id": None,
                "total_inflow": {"$sum": {"$convert": {"input": "$total_inflow", "to": "double", "onError": 0, "onNull": 0}}},
                "available_balance": {"$sum": {"$convert": {"input": "$available_balance", "to": "double", "onError": 0, "onNull": 0}}},
                "pending_balance": {"$sum": {"$convert": {"input": "$pending_balance", "to": "double", "onError": 0, "onNull": 0}}},
                "withdrawn_balance": {"$sum": {"$convert": {"input": "$withdrawn_balance", "to": "double", "onError": 0, "onNull": 0}}},
                "withdrawn_net_total": {"$sum": {"$convert": {"input": "$withdrawn_net_total", "to": "double", "onError": 0, "onNull": 0}}},
                "fee_total": {"$sum": {"$convert": {"input": "$fee_total", "to": "double", "onError": 0, "onNull": 0}}},
                "balance_count": {"$sum": 1},
            }},
        ]), None)
    except Exception:
        bal_doc = None

    req_match: Dict[str, Any] = {"status": "pending"}
    if _is_main_admin():
        admin_ids = _sub_admin_ids()
        req_match["admin_id"] = {"$in": admin_ids} if admin_ids else {"$in": []}
    else:
        admin_oid = _admin_oid()
        req_match["admin_id"] = admin_oid if admin_oid else None
    try:
        doc = next(admin_paystack_payout_requests_col.aggregate([
            {"$match": req_match},
            {"$group": {
                "_id": None,
                "pending_amount": {"$sum": {"$convert": {"input": "$gross_amount", "to": "double", "onError": 0, "onNull": 0}}},
                "pending_count": {"$sum": 1},
            }}
        ]), None)
    except Exception:
        doc = None
    return {
        "total_inflow": float((bal_doc or {}).get("total_inflow", 0) or 0.0),
        "available_balance": float((bal_doc or {}).get("available_balance", 0) or 0.0),
        "pending_balance": float((bal_doc or {}).get("pending_balance", 0) or 0.0),
        "withdrawn_balance": float((bal_doc or {}).get("withdrawn_balance", 0) or 0.0),
        "withdrawn_net_total": float((bal_doc or {}).get("withdrawn_net_total", 0) or 0.0),
        "fee_total": float((bal_doc or {}).get("fee_total", 0) or 0.0),
        "balance_count": int((bal_doc or {}).get("balance_count", 0) or 0),
        "pending_request_amount": float((doc or {}).get("pending_amount", 0) or 0.0),
        "pending_request_count": int((doc or {}).get("pending_count", 0) or 0),
    }


def compute_paystack_gateway_cashflow() -> Dict[str, float]:
    base: Dict[str, Any] = {
        "status": "success",
        "source": {"$ne": "admin_paystack_payout"},
        "meta.paystack_payout": {"$ne": True},
        "$or": [
            {"gateway": {"$regex": "paystack", "$options": "i"}},
            {"source": {"$regex": "paystack", "$options": "i"}},
            {"meta.paystack_profile": {"$in": ["store", "deposit", "subscription"]}},
            {"source": "admin_subscription"},
        ],
    }
    if not _is_main_admin():
        admin_oid = _admin_oid()
        if admin_oid:
            base["admin_id"] = admin_oid

    inflow_expr = {
        "$convert": {
            "input": {
                "$ifNull": [
                    "$meta.paystack_credit_ghs",
                    {"$ifNull": [
                        "$meta.net_credit_ghs",
                        {"$ifNull": [
                            "$meta.expected_order_total_ghs",
                            {"$ifNull": [
                                "$meta.amount_due",
                                "$amount",
                            ]},
                        ]},
                    ]},
                ]
            },
            "to": "double",
            "onError": 0,
            "onNull": 0,
        }
    }

    try:
        doc = next(transactions_col.aggregate([
            {"$match": base},
            {"$group": {"_id": None, "total": {"$sum": inflow_expr}, "count": {"$sum": 1}}},
        ]), None)
    except Exception:
        doc = None

    payout = compute_paystack_payout_summary()
    inflow = float((doc or {}).get("total", 0) or 0.0)
    outflow = float(payout.get("withdrawn_balance", 0) or 0.0)
    return {
        "inflow": inflow,
        "outflow": outflow,
        "net_flow": inflow - outflow,
        "transaction_count": int((doc or {}).get("count", 0) or 0),
    }

def compute_store_accounts_outstanding() -> float:
    try:
        doc = next(store_accounts_col.aggregate([
            {"$match": _admin_match()},
            {"$group": {
                "_id": None,
                "total": {"$sum": {"$convert": {"input": "$total_profit_balance", "to": "double", "onError": 0, "onNull": 0}}}
            }}
        ]), None)
    except Exception:
        doc = None
    return float((doc or {}).get("total", 0) or 0.0)


def _json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _dashboard_debug_payload() -> Dict[str, Any]:
    admin_scope = _dashboard_admin_scope()
    admin_oid = _admin_oid()
    debug_object_count = 0
    debug_string_count = 0
    if admin_oid:
        try:
            debug_object_count = int(orders_col.count_documents({"admin_id": admin_oid}))
        except Exception:
            debug_object_count = 0
        try:
            debug_string_count = int(orders_col.count_documents({"admin_id": str(admin_oid)}))
        except Exception:
            debug_string_count = 0

    either_admin_query: Dict[str, Any] = admin_scope
    if admin_oid:
        either_admin_query = {"admin_id": {"$in": [admin_oid, str(admin_oid)]}}

    sample_orders: List[Dict[str, Any]] = []
    try:
        docs = list(
            orders_col.find(
                either_admin_query,
                {
                    "order_id": 1,
                    "admin_id": 1,
                    "charged_amount": 1,
                    "total_amount": 1,
                    "main_admin_profit_total": 1,
                    "admin_profit_total": 1,
                    "profit_amount_total": 1,
                    "status": 1,
                    "paid_from": 1,
                    "created_at": 1,
                },
                sort=[("created_at", -1)],
                limit=10,
            )
        )
    except Exception:
        docs = []
    for d in docs:
        created = d.get("created_at")
        sample_orders.append({
            "order_id": d.get("order_id"),
            "admin_id": str(d.get("admin_id") or ""),
            "charged_amount": d.get("charged_amount"),
            "total_amount": d.get("total_amount"),
            "main_admin_profit_total": d.get("main_admin_profit_total"),
            "admin_profit_total": d.get("admin_profit_total"),
            "profit_amount_total": d.get("profit_amount_total"),
            "status": d.get("status"),
            "paid_from": d.get("paid_from"),
            "created_at": created.isoformat() if isinstance(created, datetime) else str(created or ""),
        })

    try:
        sample_order_count = int(orders_col.count_documents(either_admin_query))
    except Exception:
        sample_order_count = 0

    return {
        "role": session.get("role"),
        "user_id": str(session.get("user_id") or ""),
        "session": {
            "user_id": str(session.get("user_id") or ""),
            "role": session.get("role"),
            "admin_id": str(session.get("admin_id") or ""),
            "admin_level": session.get("admin_level"),
        },
        "admin_oid": str(admin_oid or ""),
        "resolved_admin_oid": str(admin_oid or ""),
        "admin_match": _json_safe(admin_scope),
        "admin_scope": _json_safe(admin_scope),
        "debug_object_count": debug_object_count,
        "debug_string_count": debug_string_count,
        "sample_order_count": sample_order_count,
        "sample_orders": sample_orders,
        "compute_totals_result": compute_totals(),
        "transaction_kpis_result": compute_transaction_kpis(),
        "daily_profit_result": compute_daily_profits(days_back=6),
    }


def _day_range(d: datetime.date):
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end


def compute_daily_profits(days_back: int = 6) -> Dict[str, Any]:
    today = datetime.utcnow().date()
    count = max(1, int(days_back or 6))
    days = [today - timedelta(days=i) for i in range(count)][::-1]
    if not days:
        return {
            "labels": [],
            "values": [],
            "today_profit": 0.0,
            "yesterday_profit": 0.0,
            "change_pct": 0.0,
            "trend": "flat",
            "statement": "No data."
        }

    window_start, _ = _day_range(days[0])
    _, window_end = _day_range(days[-1])
    match = _dashboard_order_match({"created_at": {"$gte": window_start, "$lt": window_end}})

    by_day: Dict[str, float] = {}
    try:
        docs = list(orders_col.aggregate([
            {"$match": match},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "profit": {"$sum": _dashboard_profit_expr()},
            }},
        ]))
        by_day = {str(doc.get("_id")): float(doc.get("profit", 0) or 0.0) for doc in docs}
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_daily_profit_error", exc, match=match)

    labels: List[str] = []
    values: List[float] = []
    for d in days:
        labels.append("Today" if d == today else d.strftime("%b %d"))
        values.append(round(by_day.get(d.strftime("%Y-%m-%d"), 0.0), 2))

    today_profit = values[-1] if values else 0.0
    yesterday_profit = values[-2] if len(values) >= 2 else 0.0

    if yesterday_profit == 0:
        change_pct = 100.0 if today_profit > 0 else 0.0
    else:
        change_pct = ((today_profit - yesterday_profit) / abs(yesterday_profit)) * 100.0

    if abs(today_profit - yesterday_profit) < 1e-9:
        trend = "flat"
        statement = "Today’s profit is the same as yesterday."
    elif today_profit > yesterday_profit:
        trend = "up"
        diff = round(today_profit - yesterday_profit, 2)
        pct = round(change_pct, 2)
        statement = f"Today’s profit has risen by {pct}% compared to yesterday (up GHS {diff:,.2f})."
    else:
        trend = "down"
        diff = round(yesterday_profit - today_profit, 2)
        pct = round(abs(change_pct), 2)
        statement = f"Today’s profit has fallen by {pct}% compared to yesterday (down GHS {diff:,.2f})."

    return {
        "labels": labels,
        "values": values,
        "today_profit": round(today_profit, 2),
        "yesterday_profit": round(yesterday_profit, 2),
        "change_pct": round(change_pct, 2),
        "trend": trend,
        "statement": statement,
    }


def _display_for_actor(actor_id: str, users_map: Dict[str, str], source: str) -> str:
    label = None
    try:
        oid = ObjectId(actor_id)
        label = users_map.get(str(oid))
    except Exception:
        pass
    if not label:
        prefix = "Agent" if source == "agent" else "Customer"
        label = f"{prefix} {actor_id[:6].upper()}"
    return label


def agents_cumulative_sales(limit: int = 10) -> Tuple[List[str], List[float], List[Dict[str, Any]]]:
    pipeline: List[Dict[str, Any]] = [
        {"$match": _dashboard_order_match()},
        {"$unwind": "$items"},
        {"$addFields": {
            "amount_num": {"$convert": {"input": {"$ifNull": ["$items.amount", 0]}, "to": "double", "onError": 0, "onNull": 0}},
            "agent1": {"$ifNull": ["$items.agent_id", None]},
            "agent2": {"$ifNull": ["$items.agentId", None]},
            "agent3": {"$ifNull": ["$items.value_obj.agent_id", None]},
            "agent4": {"$ifNull": ["$items.value_obj.agentId", None]},
        }},
        {"$addFields": {
            "agent_coalesced": {
                "$let": {
                    "vars": {"a1": "$agent1", "a2": "$agent2", "a3": "$agent3", "a4": "$agent4"},
                    "in": {"$ifNull": [
                        {"$cond": [{"$ne": ["$$a1", ""]}, "$$a1", None]},
                        {"$ifNull": [
                            {"$cond": [{"$ne": ["$$a2", ""]}, "$$a2", None]},
                            {"$ifNull": [
                                {"$cond": [{"$ne": ["$$a3", ""]}, "$$a3", None]},
                                {"$cond": [{"$ne": ["$$a4", ""]}, "$$a4", None]}
                            ]}
                        ]}
                    ]}
                }
            }
        }},
        {"$addFields": {
            "actor_id": {"$toString": {"$ifNull": ["$agent_coalesced", "$user_id"]}},
            "actor_source": {"$cond": [{"$ne": ["$agent_coalesced", None]}, "agent", "customer"]}
        }},
        {"$match": {"amount_num": {"$gt": 0}}},
        {"$group": {
            "_id": {"actor_id": "$actor_id", "actor_source": "$actor_source"},
            "total_sales": {"$sum": "$amount_num"},
            "line_count": {"$sum": 1}
        }},
        {"$sort": {"total_sales": -1}},
        {"$limit": int(limit)},
    ]

    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    to_resolve: List[ObjectId] = []
    for doc in agg:
        actor_id = (doc.get("_id") or {}).get("actor_id")
        try:
            to_resolve.append(ObjectId(actor_id))
        except Exception:
            pass
    users_map = _users_display_map(to_resolve)

    labels: List[str] = []
    values: List[float] = []
    table_rows: List[Dict[str, Any]] = []

    for doc in agg:
        _id = doc.get("_id") or {}
        actor_id = str(_id.get("actor_id"))
        actor_source = _id.get("actor_source")
        total_sales = float(doc.get("total_sales", 0) or 0)
        line_count = int(doc.get("line_count", 0) or 0)

        label = _display_for_actor(actor_id, users_map, actor_source)

        labels.append(label)
        values.append(round(total_sales, 2))
        table_rows.append({
            "agent_id": actor_id,
            "agent": label if actor_source == "agent" else f"{label} (Customer)",
            "sales": round(total_sales, 2),
            "lines": line_count
        })

    return labels, values, table_rows


# ✅ Withdrawal Requests KPI counters
def compute_withdraw_requests_pending() -> int:
    try:
        return int(
            store_withdraw_requests_col.count_documents(
                {"status": {"$in": ["requested", "pending", "processing"]}, **_admin_match()}
            )
        )
    except Exception:
        return 0


def compute_withdraw_requests_total_open() -> int:
    # “open” = pending or processing
    try:
        return int(
            store_withdraw_requests_col.count_documents(
                {"status": {"$in": ["requested", "pending", "processing"]}, **_admin_match()}
            )
        )
    except Exception:
        return 0


# ----------------------------
# API for modal (dashboard will call these)
# ----------------------------

@admin_dashboard_bp.route("/admin/withdrawals/list")
def admin_withdrawals_list():
    if not session.get("admin_logged_in"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # return latest 50
    try:
        status = (request.args.get("status") or "").strip().lower()
        q = (request.args.get("q") or "").strip()
        limit_raw = request.args.get("limit") or "50"
        offset_raw = request.args.get("offset") or "0"
        try:
            limit = max(1, min(200, int(limit_raw)))
        except Exception:
            limit = 50
        try:
            offset = max(0, int(offset_raw))
        except Exception:
            offset = 0

        query: Dict[str, Any] = dict(_admin_match())
        if status == "unpaid":
            query["status"] = {"$in": ["requested", "pending", "processing"]}
        elif status:
            query["status"] = status
        if q:
            q_re = {"$regex": q, "$options": "i"}
            query["$or"] = [
                {"store_slug": q_re},
                {"store": q_re},
                {"account": q_re},
                {"msisdn": q_re},
                {"wallet": q_re},
                {"network": q_re},
                {"recipient_name": q_re},
                {"reference": q_re},
                {"method": q_re},
            ]

        docs = list(
            store_withdraw_requests_col.find(query, sort=[("created_at", -1)], limit=limit, skip=offset)
        )
    except Exception:
        docs = []

    def _safe_str(x):
        try:
            return str(x)
        except Exception:
            return ""

    out: List[Dict[str, Any]] = []
    for d in docs:
        out.append({
            "_id": _safe_str(d.get("_id")),
            "reference": d.get("reference") or d.get("ref") or d.get("request_ref") or "",
            "status": (d.get("status") or "pending"),
            "amount": d.get("amount", 0),
            "currency": d.get("currency", "GHS"),
            "owner_id": _safe_str(d.get("owner_id") or d.get("user_id") or ""),
            "store_slug": d.get("store_slug") or d.get("store") or "",
            "method": d.get("method") or d.get("payout_method") or d.get("type") or "",
            "account": d.get("account") or d.get("msisdn") or d.get("wallet") or "",
            "network": d.get("network") or "",
            "recipient_name": d.get("recipient_name") or "",
            "created_at": (d.get("created_at").isoformat() if isinstance(d.get("created_at"), datetime) else ""),
        })
    return jsonify({"ok": True, "items": out})


@admin_dashboard_bp.route("/admin/withdrawals/update", methods=["POST"])
def admin_withdrawals_update():
    if not session.get("admin_logged_in"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    req_id = (data.get("id") or "").strip()
    new_status = (data.get("status") or "").strip().lower()
    note = (data.get("note") or "").strip()

    ok, payload, code = update_withdraw_request_status(
        req_id=req_id,
        new_status=new_status,
        actor_id=session.get("admin_id") or session.get("user_id") or "admin",
        note=note,
    )
    if ok:
        return jsonify({"ok": True, **payload}), code
    return jsonify({"ok": False, "error": payload.get("message")}), code


@admin_dashboard_bp.route("/admin/dashboard/debug-totals")
def admin_dashboard_debug_totals():
    if not session.get("admin_logged_in") and (session.get("role") not in {"admin", "main_admin", "super_admin", "professional_admin", "superadmin"}):
        return jsonify({"success": False, "message": "unauthorized"}), 401
    return jsonify({"success": True, **_dashboard_debug_payload()})


@admin_dashboard_bp.route("/admin/dashboard/raw-money-debug")
def admin_dashboard_raw_money_debug():
    if not session.get("admin_logged_in") and ((session.get("role") or "").strip().lower() not in {"admin", "main_admin", "super_admin", "professional_admin", "superadmin"}):
        return jsonify({"success": False, "message": "unauthorized"}), 401

    dashboard_scope = _dashboard_admin_scope()
    dashboard_order_match = _dashboard_order_match()
    admin_oid = _admin_oid()
    latest_orders: List[Dict[str, Any]] = []

    try:
        docs = list(orders_col.find(
            dashboard_order_match,
            {
                "order_id": 1,
                "status": 1,
                "total_amount": 1,
                "charged_amount": 1,
                "main_admin_profit_total": 1,
                "admin_profit_total": 1,
                "profit_amount_total": 1,
                "created_at": 1,
            },
            sort=[("created_at", -1)],
            limit=10,
        ))
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_raw_money_latest_orders_error", exc, match=dashboard_order_match)
        docs = []

    for order in docs:
        latest_orders.append({
            "order_id": order.get("order_id") or str(order.get("_id") or ""),
            "status": order.get("status"),
            "total_amount": order.get("total_amount"),
            "charged_amount": order.get("charged_amount"),
            "main_admin_profit_total": order.get("main_admin_profit_total"),
            "admin_profit_total": order.get("admin_profit_total"),
            "profit_amount_total": order.get("profit_amount_total"),
            "created_at": _json_safe(order.get("created_at")),
        })

    try:
        order_count_scope = int(orders_col.count_documents(dashboard_scope))
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_raw_money_count_scope_error", exc, match=dashboard_scope)
        order_count_scope = 0

    try:
        order_count_dashboard_match = int(orders_col.count_documents(dashboard_order_match))
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_raw_money_count_match_error", exc, match=dashboard_order_match)
        order_count_dashboard_match = 0

    return jsonify(_json_safe({
        "role": session.get("role"),
        "user_id": session.get("user_id"),
        "admin_id": session.get("admin_id"),
        "admin_oid": str(admin_oid or ""),
        "dashboard_scope": dashboard_scope,
        "dashboard_order_match": dashboard_order_match,
        "order_count_scope": order_count_scope,
        "order_count_dashboard_match": order_count_dashboard_match,
        "latest_orders": latest_orders,
        "manual_sums": compute_totals(),
        "daily_profit": compute_daily_profits(6),
    }))


def _dashboard_authorized() -> bool:
    role = (session.get("role") or "").strip().lower()
    return bool(session.get("admin_logged_in")) or role in {"admin", "main_admin", "super_admin", "professional_admin", "superadmin"}


def _dashboard_template_name() -> str:
    if _is_main_admin():
        return "main_admin_dashboard.html"
    admin_level = (session.get("admin_level") or "").strip().lower()
    if admin_level == "super_admin":
        return "admin/super_admin_dashboard.html"
    if admin_level == "super_professional":
        return "admin/professional_admin_dashboard.html"
    return "admin/admin_dashboard.html"


def _compute_total_orders_today() -> int:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    try:
        return int(orders_col.count_documents(_dashboard_order_match({"created_at": {"$gte": start, "$lt": end}})))
    except Exception:
        return 0


def _compute_afa_stats() -> Dict[str, int]:
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    scope = _admin_match()
    try:
        doc = next(afa_col.aggregate([
            {"$match": scope},
            {"$group": {
                "_id": None,
                "afa_total": {"$sum": 1},
                "afa_pending": {"$sum": {"$cond": [{"$eq": ["$status", "pending"]}, 1, 0]}},
                "afa_today": {"$sum": {"$cond": [
                    {"$and": [{"$gte": ["$created_at", start]}, {"$lt": ["$created_at", end]}]},
                    1,
                    0,
                ]}},
            }},
        ]), None)
    except Exception as exc:
        _log_dashboard_agg_error("dashboard_afa_stats_error", exc, match=scope)
        doc = None
    return {
        "afa_total": int((doc or {}).get("afa_total", 0) or 0),
        "afa_pending": int((doc or {}).get("afa_pending", 0) or 0),
        "afa_today": int((doc or {}).get("afa_today", 0) or 0),
    }


def _dashboard_summary_payload() -> Dict[str, Any]:
    """Cached summary loader for the async dashboard API."""
    totals = compute_totals()
    customer_counts = compute_customer_counts()
    agent_counts = compute_agent_counts()
    platform_counts = compute_platform_admin_counts()
    bal_summary = compute_user_balances_summary()
    paystack_payout_summary = compute_paystack_payout_summary()
    paystack_cashflow = compute_paystack_gateway_cashflow()
    afa_stats = _compute_afa_stats()
    tx = compute_transaction_kpis()
    sms_kpis = compute_bulk_sms_kpis()
    dp = compute_daily_profits(days_back=6)
    flow = compute_balance_flow_totals()

    admin_wallet_status = {"low": False, "balance": 0.0, "limit": 50.0, "auto_credit": {}}
    if not _is_main_admin():
        try:
            admin_wallet_status = evaluate_admin_wallet_low_balance(_admin_oid(), send_alert=True, run_auto_credit=True)
        except Exception:
            pass

    return {
        "total_orders": int(totals["matched_order_count"]),
        "total_orders_today": _compute_total_orders_today(),
        "sum_total_amount": float(totals["sum_total_amount"]),
        "sum_charged_amount": float(totals["sum_charged_amount"]),
        "sum_profit_amount": float(totals["sum_profit_amount"]),
        "total_sales": float(totals["sum_total_amount"]),
        "total_charged": float(totals["sum_charged_amount"]),
        "total_profit": float(totals["sum_profit_amount"]),
        "admin_profit": float(totals["sum_profit_amount"]),
        "dashboard_profit": float(totals["sum_profit_amount"]),
        **customer_counts,
        **agent_counts,
        "dashboard_total_agents": agent_counts["total_agents"],
        "dashboard_blocked_agents": agent_counts["blocked_agents"],
        "dashboard_pending_agents": agent_counts["pending_agents"],
        "dashboard_active_agents": agent_counts["active_agents"],
        **platform_counts,
        "total_user_balance_amount": float(bal_summary["total_balance_amount"]),
        "balance_doc_count": int(bal_summary["balance_doc_count"]),
        "positive_balance_count": int(bal_summary["positive_balance_count"]),
        "outstanding_payouts": float(compute_store_accounts_outstanding()),
        "withdraw_requests_pending": compute_withdraw_requests_pending(),
        "withdraw_requests_open": compute_withdraw_requests_total_open(),
        "today_profit": dp["today_profit"],
        "yesterday_profit": dp["yesterday_profit"],
        "profit_change_pct": dp["change_pct"],
        "profit_trend": dp["trend"],
        "profit_statement": dp["statement"],
        "deposits_overall": flow["deposits_overall"],
        "withdrawals_overall": flow["withdrawals_overall"],
        "deposits_today": flow["deposits_today"],
        "withdrawals_today": flow["withdrawals_today"],
        **afa_stats,
        **tx,
        "transaction_total_amount": tx["txn_total_amount"],
        "transaction_today_amount": tx["txn_today_amount"],
        **sms_kpis,
        "paystack_total_inflow": float(paystack_payout_summary["total_inflow"]),
        "paystack_total_settled": float(paystack_payout_summary["withdrawn_balance"]),
        "paystack_pending_balance": float(paystack_payout_summary["pending_balance"]),
        "paystack_unwithdrawn_balance": float(paystack_payout_summary["available_balance"]),
        "paystack_withdrawn_net_total": float(paystack_payout_summary["withdrawn_net_total"]),
        "paystack_fee_total": float(paystack_payout_summary["fee_total"]),
        "paystack_balance_count": int(paystack_payout_summary["balance_count"]),
        "total_paystack_payouts": float(paystack_payout_summary["available_balance"]),
        "paystack_payout_request_count": int(paystack_payout_summary["pending_request_count"]),
        "paystack_pending_request_amount": float(paystack_payout_summary["pending_request_amount"]),
        "paystack_gateway_inflow": float(paystack_cashflow["inflow"]),
        "paystack_gateway_outflow": float(paystack_cashflow["outflow"]),
        "paystack_gateway_net_flow": float(paystack_cashflow["net_flow"]),
        "paystack_gateway_transaction_count": int(paystack_cashflow["transaction_count"]),
        "admin_wallet_low": bool(admin_wallet_status.get("low")),
        "admin_wallet_balance": admin_wallet_status.get("balance", 0.0),
        "admin_wallet_low_limit": admin_wallet_status.get("limit", 50.0),
        "admin_wallet_auto_credit_result": admin_wallet_status.get("auto_credit") or {},
    }


def _dashboard_charts_payload() -> Dict[str, Any]:
    dp = compute_daily_profits(days_back=6)
    top_admin_sales_labels: List[str] = []
    top_admin_sales_values: List[float] = []
    top_admin_orders_labels: List[str] = []
    top_admin_orders_values: List[int] = []
    if _is_main_admin():
        top_admin_sales_labels, top_admin_sales_values, _rows = top_admins_by_sales(limit=10)
        top_admin_orders_labels, top_admin_orders_values = top_admins_by_orders(limit=10)
    chart_labels, chart_values = top_customers_by_orders(limit=10)
    profit_chart_labels, profit_chart_values = top_customers_by_profit(limit=10)
    agent_sales_labels, agent_sales_values, _agent_rows = agents_cumulative_sales(limit=10)
    return {
        "daily_profit_labels": dp["labels"],
        "daily_profit_values": dp["values"],
        "top_admin_orders_labels": top_admin_orders_labels,
        "top_admin_orders_values": top_admin_orders_values,
        "top_admin_sales_labels": top_admin_sales_labels,
        "top_admin_sales_values": top_admin_sales_values,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "profit_chart_labels": profit_chart_labels,
        "profit_chart_values": profit_chart_values,
        "agent_sales_labels": agent_sales_labels,
        "agent_sales_values": agent_sales_values,
    }


def _dashboard_tables_payload() -> Dict[str, Any]:
    top_admin_rows: List[Dict[str, Any]] = []
    recent_activity_rows: List[Dict[str, Any]] = []
    if _is_main_admin():
        _labels, _values, top_admin_rows = top_admins_by_sales(limit=10)
        recent_activity_rows = recent_activities(limit=12)
    _agent_labels, _agent_values, top_agents_rows = agents_cumulative_sales(limit=10)
    return {
        "top_admin_rows": top_admin_rows,
        "recent_activity_rows": recent_activity_rows,
        "top_offers": top_offers_by_purchases(limit=10),
        "top_agents_rows": top_agents_rows,
    }


def _dashboard_placeholder_context() -> Dict[str, Any]:
    """Fast render defaults; async API calls replace these values on the page."""
    return {
        "total_orders": 0,
        "total_orders_today": 0,
        "sum_total_amount": 0.0,
        "sum_charged_amount": 0.0,
        "sum_profit_amount": 0.0,
        "total_sales": 0.0,
        "total_charged": 0.0,
        "total_profit": 0.0,
        "admin_profit": 0.0,
        "dashboard_profit": 0.0,
        "total_admins": 0,
        "total_agents": 0,
        "total_user_balance_amount": 0.0,
        "balance_doc_count": 0,
        "positive_balance_count": 0,
        "outstanding_payouts": 0.0,
        "total_paystack_payouts": 0.0,
        "paystack_payout_request_count": 0,
        "paystack_total_inflow": 0.0,
        "paystack_total_settled": 0.0,
        "paystack_pending_balance": 0.0,
        "paystack_unwithdrawn_balance": 0.0,
        "paystack_withdrawn_net_total": 0.0,
        "paystack_fee_total": 0.0,
        "paystack_balance_count": 0,
        "paystack_pending_request_amount": 0.0,
        "paystack_gateway_inflow": 0.0,
        "paystack_gateway_outflow": 0.0,
        "paystack_gateway_net_flow": 0.0,
        "paystack_gateway_transaction_count": 0,
        "withdraw_requests_pending": 0,
        "withdraw_requests_open": 0,
        "today_profit": 0.0,
        "yesterday_profit": 0.0,
        "profit_change_pct": 0,
        "profit_trend": "flat",
        "profit_statement": "Loading dashboard data...",
        "daily_profit_labels": [],
        "daily_profit_values": [],
        "chart_labels": [],
        "chart_values": [],
        "profit_chart_labels": [],
        "profit_chart_values": [],
        "agent_sales_labels": [],
        "agent_sales_values": [],
        "top_agents_rows": [],
        "top_offers": [],
        "dashboard_total_agents": 0,
        "dashboard_blocked_agents": 0,
        "dashboard_pending_agents": 0,
        "dashboard_active_agents": 0,
        "total_customers": 0,
        "blocked_customers": 0,
        "active_customers": 0,
        "deposits_overall": 0.0,
        "withdrawals_overall": 0.0,
        "deposits_today": 0.0,
        "withdrawals_today": 0.0,
        "afa_total": 0,
        "afa_pending": 0,
        "afa_today": 0,
        "txn_total_count": 0,
        "txn_today_count": 0,
        "txn_total_amount": 0.0,
        "txn_today_amount": 0.0,
        "transaction_total_amount": 0.0,
        "transaction_today_amount": 0.0,
        "sms_delivered_today": 0,
        "sms_delivery_orders_today": 0,
        "sms_profit_today": 0.0,
        "sms_delivered_total": 0,
        "sms_profit_total": 0.0,
        "top_admin_sales_labels": [],
        "top_admin_sales_values": [],
        "top_admin_rows": [],
        "top_admin_orders_labels": [],
        "top_admin_orders_values": [],
        "recent_activity_rows": [],
        "announcement_popup": None,
        "admin_wallet_low": False,
        "admin_wallet_balance": 0.0,
        "admin_wallet_low_limit": 50.0,
        "admin_wallet_auto_credit_result": {},
    }


def _load_dashboard_announcement_popup():
    """Lightweight popup lookup kept in the fast render route."""
    try:
        return get_popup_announcement(session.get("role"), _admin_oid(), session.get("user_id"))
    except Exception:
        return None


@admin_dashboard_bp.route("/admin/api/dashboard/summary")
def admin_dashboard_summary_api():
    if not _dashboard_authorized():
        return jsonify({"success": False, "message": "unauthorized"}), 401
    payload = _cached_copy(_dashboard_cache_key("api_summary"), 25, _dashboard_summary_payload)
    return jsonify({"success": True, "data": _json_safe(payload)})


@admin_dashboard_bp.route("/admin/api/dashboard/charts")
def admin_dashboard_charts_api():
    if not _dashboard_authorized():
        return jsonify({"success": False, "message": "unauthorized"}), 401
    payload = _cached_copy(_dashboard_cache_key("api_charts"), 60, _dashboard_charts_payload)
    return jsonify({"success": True, "data": _json_safe(payload)})


@admin_dashboard_bp.route("/admin/api/dashboard/tables")
def admin_dashboard_tables_api():
    if not _dashboard_authorized():
        return jsonify({"success": False, "message": "unauthorized"}), 401
    payload = _cached_copy(_dashboard_cache_key("api_tables"), 60, _dashboard_tables_payload)
    return jsonify({"success": True, "data": _json_safe(payload)})


@admin_dashboard_bp.route("/admin/api/dashboard/wallet-flows")
def admin_dashboard_wallet_flows_api():
    if not _dashboard_authorized():
        return jsonify({"success": False, "message": "unauthorized"}), 401
    payload = _cached_copy(_dashboard_cache_key("api_wallet_flows"), 30, compute_balance_flow_totals)
    return jsonify({"success": True, "data": _json_safe(payload)})


# ----------------------------
# Dashboard Route
# ----------------------------

@admin_dashboard_bp.route("/admin/dashboard")
def admin_dashboard():
    # Fast render route: return the dashboard shell immediately.
    # Heavy MongoDB calculations are loaded after paint by /admin/api/dashboard/*.
    if not _dashboard_authorized():
        return redirect(url_for("login.login"))
    context = _dashboard_placeholder_context()
    context["announcement_popup"] = _load_dashboard_announcement_popup()
    return render_template(_dashboard_template_name(), **context)

    scope = _admin_match()
    order_scope = _dashboard_order_match()
    is_main_admin = _is_main_admin()
    if _DASHBOARD_DEBUG:
        try:
            print("[dashboard_debug_scope]", {
                "role": session.get("role"),
                "user_id": str(session.get("user_id")),
                "session_admin_id": str(session.get("admin_id")),
                "resolved_admin_oid": str(_admin_oid()),
                "scope": str(_dashboard_admin_scope()),
            })
        except Exception:
            pass

    # Orders totals
    def _load_total_orders() -> int:
        try:
            return int(orders_col.count_documents(order_scope))
        except Exception:
            return 0

    total_orders = _cached_copy(_dashboard_cache_key("total_orders"), 20, _load_total_orders)
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)

    def _load_total_orders_today() -> int:
        try:
            return int(orders_col.count_documents(_dashboard_order_match({"created_at": {"$gte": start, "$lt": end}})))
        except Exception:
            return 0

    total_orders_today = _cached_copy(
        _dashboard_cache_key("total_orders_today", str(today)),
        20,
        _load_total_orders_today,
    )

    totals = _cached_copy(_dashboard_cache_key("totals"), 20, compute_totals)
    sum_total_amount = totals["sum_total_amount"]
    sum_charged_amount = totals["sum_charged_amount"]
    sum_profit_amount = totals["sum_profit_amount"]
    if _DASHBOARD_DEBUG:
        try:
            print("[dashboard_runtime_debug]", {
                "role": session.get("role"),
                "user_id": session.get("user_id"),
                "admin_id": session.get("admin_id"),
                "admin_oid": str(_admin_oid() or ""),
                "scope": str(_dashboard_admin_scope()),
                "order_count_scope": orders_col.count_documents(_dashboard_admin_scope()),
                "order_count_match": orders_col.count_documents(_dashboard_order_match()),
                "totals": compute_totals(),
            })
        except Exception as exc:
            _log_dashboard_agg_error("dashboard_runtime_debug_error", exc, match=_dashboard_order_match())

    # Total amount at USER ACCOUNT BALANCE
    bal_summary = _cached_copy(_dashboard_cache_key("balances_summary"), 30, compute_user_balances_summary)
    total_user_balance_amount = float(bal_summary["total_balance_amount"])
    balance_doc_count = int(bal_summary["balance_doc_count"])
    positive_balance_count = int(bal_summary["positive_balance_count"])

    platform_counts = _cached_copy(_dashboard_cache_key("platform_admin_counts"), 30, compute_platform_admin_counts)
    total_admins = int(platform_counts["total_admins"])
    total_agents = int(platform_counts["total_agents"])

    paystack_payout_summary = _cached_copy(
        _dashboard_cache_key("paystack_payout_summary"),
        30,
        compute_paystack_payout_summary,
    )
    paystack_total_inflow = float(paystack_payout_summary["total_inflow"])
    paystack_total_settled = float(paystack_payout_summary["withdrawn_balance"])
    paystack_pending_balance = float(paystack_payout_summary["pending_balance"])
    paystack_unwithdrawn_balance = float(paystack_payout_summary["available_balance"])
    paystack_withdrawn_net_total = float(paystack_payout_summary["withdrawn_net_total"])
    paystack_fee_total = float(paystack_payout_summary["fee_total"])
    paystack_balance_count = int(paystack_payout_summary["balance_count"])
    total_paystack_payouts = paystack_unwithdrawn_balance
    paystack_payout_request_count = int(paystack_payout_summary["pending_request_count"])
    paystack_pending_request_amount = float(paystack_payout_summary["pending_request_amount"])

    paystack_cashflow = _cached_copy(
        _dashboard_cache_key("paystack_gateway_cashflow"),
        30,
        compute_paystack_gateway_cashflow,
    )
    paystack_gateway_inflow = float(paystack_cashflow["inflow"])
    paystack_gateway_outflow = float(paystack_cashflow["outflow"])
    paystack_gateway_net_flow = float(paystack_cashflow["net_flow"])
    paystack_gateway_transaction_count = int(paystack_cashflow["transaction_count"])

    # Outstanding payouts across all store accounts
    outstanding_payouts = _cached_copy(
        _dashboard_cache_key("store_accounts_outstanding"),
        30,
        compute_store_accounts_outstanding,
    )

    # Daily profits (today + previous 5)
    dp = _cached_copy(_dashboard_cache_key("daily_profits", 6), 30, lambda: compute_daily_profits(days_back=6))

    # Top customers (orders & profit)
    chart_labels, chart_values = _cached_copy(
        _dashboard_cache_key("top_customers_orders", 10),
        45,
        lambda: top_customers_by_orders(limit=10),
    )
    profit_chart_labels, profit_chart_values = _cached_copy(
        _dashboard_cache_key("top_customers_profit", 10),
        45,
        lambda: top_customers_by_profit(limit=10),
    )

    # Top offers
    top_offers = _cached_copy(
        _dashboard_cache_key("top_offers", 10),
        45,
        lambda: top_offers_by_purchases(limit=10),
    )

    # Accumulative sales (agent first, fallback to customer)
    agent_sales_labels, agent_sales_values, top_agents_rows = _cached_copy(
        _dashboard_cache_key("agent_sales", 10),
        45,
        lambda: agents_cumulative_sales(limit=10),
    )

    # Agent counts for this admin dashboard
    agent_counts = _cached_copy(_dashboard_cache_key("agent_counts"), 30, compute_agent_counts)

    # Balance flows (overall + today)
    flow = _cached_copy(_dashboard_cache_key("balance_flows"), 30, compute_balance_flow_totals)

    # AFA registration KPIs
    def _load_afa_stats() -> Dict[str, int]:
        try:
            return {
                "afa_total": int(afa_col.count_documents(scope)),
                "afa_pending": int(afa_col.count_documents({"status": "pending", **scope})),
                "afa_today": int(afa_col.count_documents({"created_at": {"$gte": start, "$lt": end}, **scope})),
            }
        except Exception:
            return {"afa_total": 0, "afa_pending": 0, "afa_today": 0}

    afa_stats = _cached_copy(_dashboard_cache_key("afa_stats", str(today)), 30, _load_afa_stats)
    afa_total = afa_stats["afa_total"]
    afa_pending = afa_stats["afa_pending"]
    afa_today = afa_stats["afa_today"]

    # Transactions KPIs
    tx = _cached_copy(_dashboard_cache_key("transaction_kpis", str(today)), 30, compute_transaction_kpis)
    sms_kpis = _cached_copy(_dashboard_cache_key("bulk_sms_kpis", str(today)), 30, compute_bulk_sms_kpis)

    # Main admin: Admin performance charts + activities
    top_admin_sales_labels = []
    top_admin_sales_values = []
    top_admin_rows = []
    top_admin_orders_labels = []
    top_admin_orders_values = []
    activity_rows = []
    if is_main_admin:
        top_admin_sales_labels, top_admin_sales_values, top_admin_rows = _cached_copy(
            _dashboard_cache_key("top_admin_sales", 10),
            45,
            lambda: top_admins_by_sales(limit=10),
        )
        top_admin_orders_labels, top_admin_orders_values = _cached_copy(
            _dashboard_cache_key("top_admin_orders", 10),
            45,
            lambda: top_admins_by_orders(limit=10),
        )
        activity_rows = _cached_copy(
            _dashboard_cache_key("recent_activity", 12),
            15,
            lambda: recent_activities(limit=12),
        )

    # ✅ Withdrawal requests KPI
    withdraw_requests_pending = _cached_copy(
        _dashboard_cache_key("withdraw_requests_pending"),
        20,
        compute_withdraw_requests_pending,
    )
    withdraw_requests_open = _cached_copy(
        _dashboard_cache_key("withdraw_requests_open"),
        20,
        compute_withdraw_requests_total_open,
    )

    def _load_announcement_popup():
        try:
            return get_popup_announcement(session.get("role"), _admin_oid(), session.get("user_id"))
        except Exception:
            return None

    announcement_popup = _cached_copy(_dashboard_cache_key("announcement_popup"), 20, _load_announcement_popup)
    admin_wallet_status = {"low": False, "balance": 0.0, "limit": 50.0, "auto_credit": {}}
    if not is_main_admin:
        try:
            admin_wallet_status = evaluate_admin_wallet_low_balance(_admin_oid(), send_alert=True, run_auto_credit=True)
        except Exception:
            admin_wallet_status = {"low": False, "balance": 0.0, "limit": 50.0, "auto_credit": {}}

    if is_main_admin:
        template_name = "main_admin_dashboard.html"
    else:
        admin_level = (session.get("admin_level") or "").strip().lower()
        if admin_level == "super_admin":
            template_name = "admin/super_admin_dashboard.html"
        elif admin_level == "super_professional":
            template_name = "admin/professional_admin_dashboard.html"
        else:
            template_name = "admin/admin_dashboard.html"
    return render_template(
        template_name,
        # KPIs
        total_orders=total_orders,
        total_orders_today=total_orders_today,
        sum_total_amount=sum_total_amount,
        sum_charged_amount=sum_charged_amount,
        sum_profit_amount=sum_profit_amount,
        total_sales=sum_total_amount,
        total_charged=sum_charged_amount,
        total_profit=sum_profit_amount,
        admin_profit=sum_profit_amount,
        dashboard_profit=sum_profit_amount,
        total_admins=total_admins,
        total_agents=total_agents,

        # user balances KPI
        total_user_balance_amount=total_user_balance_amount,
        balance_doc_count=balance_doc_count,
        positive_balance_count=positive_balance_count,
        outstanding_payouts=outstanding_payouts,
        total_paystack_payouts=total_paystack_payouts,
        paystack_payout_request_count=paystack_payout_request_count,
        paystack_total_inflow=paystack_total_inflow,
        paystack_total_settled=paystack_total_settled,
        paystack_pending_balance=paystack_pending_balance,
        paystack_unwithdrawn_balance=paystack_unwithdrawn_balance,
        paystack_withdrawn_net_total=paystack_withdrawn_net_total,
        paystack_fee_total=paystack_fee_total,
        paystack_balance_count=paystack_balance_count,
        paystack_pending_request_amount=paystack_pending_request_amount,
        paystack_gateway_inflow=paystack_gateway_inflow,
        paystack_gateway_outflow=paystack_gateway_outflow,
        paystack_gateway_net_flow=paystack_gateway_net_flow,
        paystack_gateway_transaction_count=paystack_gateway_transaction_count,

        # ✅ withdrawal requests KPI
        withdraw_requests_pending=withdraw_requests_pending,
        withdraw_requests_open=withdraw_requests_open,

        # Profit trend + last 5 days (plus today)
        today_profit=dp["today_profit"],
        yesterday_profit=dp["yesterday_profit"],
        profit_change_pct=dp["change_pct"],
        profit_trend=dp["trend"],
        profit_statement=dp["statement"],
        daily_profit_labels=dp["labels"],
        daily_profit_values=dp["values"],

        # Charts
        chart_labels=chart_labels,
        chart_values=chart_values,
        profit_chart_labels=profit_chart_labels,
        profit_chart_values=profit_chart_values,

        # Accumulative sales (chart + table)
        agent_sales_labels=agent_sales_labels,
        agent_sales_values=agent_sales_values,
        top_agents_rows=top_agents_rows,

        # Lists
        top_offers=top_offers,

        # Agent counters
        dashboard_total_agents=agent_counts["total_agents"],
        dashboard_blocked_agents=agent_counts["blocked_agents"],
        dashboard_pending_agents=agent_counts["pending_agents"],
        dashboard_active_agents=agent_counts["active_agents"],

        # Legacy customer counters kept for main admin and existing charts
        total_customers=_cached_copy(_dashboard_cache_key("customer_counts"), 30, compute_customer_counts)["total_customers"],
        blocked_customers=_cached_copy(_dashboard_cache_key("customer_counts"), 30, compute_customer_counts)["blocked_customers"],
        active_customers=_cached_copy(_dashboard_cache_key("customer_counts"), 30, compute_customer_counts)["active_customers"],

        # Balance flows
        deposits_overall=flow["deposits_overall"],
        withdrawals_overall=flow["withdrawals_overall"],
        deposits_today=flow["deposits_today"],
        withdrawals_today=flow["withdrawals_today"],

        # AFA stats
        afa_total=afa_total,
        afa_pending=afa_pending,
        afa_today=afa_today,

        # Transactions KPIs
        txn_total_count=tx["txn_total_count"],
        txn_today_count=tx["txn_today_count"],
        txn_total_amount=tx["txn_total_amount"],
        txn_today_amount=tx["txn_today_amount"],
        transaction_total_amount=tx["txn_total_amount"],
        transaction_today_amount=tx["txn_today_amount"],
        sms_delivered_today=sms_kpis["sms_delivered_today"],
        sms_delivery_orders_today=sms_kpis["sms_delivery_orders_today"],
        sms_profit_today=sms_kpis["sms_profit_today"],
        sms_delivered_total=sms_kpis["sms_delivered_total"],
        sms_profit_total=sms_kpis["sms_profit_total"],
        top_admin_sales_labels=top_admin_sales_labels,
        top_admin_sales_values=top_admin_sales_values,
        top_admin_rows=top_admin_rows,
        top_admin_orders_labels=top_admin_orders_labels,
        top_admin_orders_values=top_admin_orders_values,
        recent_activity_rows=activity_rows,
        announcement_popup=announcement_popup,
        admin_wallet_low=bool(admin_wallet_status.get("low")),
        admin_wallet_balance=admin_wallet_status.get("balance", 0.0),
        admin_wallet_low_limit=admin_wallet_status.get("limit", 50.0),
        admin_wallet_auto_credit_result=admin_wallet_status.get("auto_credit") or {},
    )
