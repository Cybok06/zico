# admin_orders.py  — Admin Orders + DB-Backed Scheduler (Render-safe) + Bulk Deliver (Selected)
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, make_response
from bson import ObjectId, Regex
from db import db
from datetime import datetime, timedelta
import json
import os
import time
import threading
import hashlib
from urllib.parse import urlencode
import uuid
from typing import List, Tuple, Dict, Any
from collections import OrderedDict
from io import BytesIO, StringIO
import csv
import re
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from tenant import current_admin_id_from_session

admin_orders_bp = Blueprint("admin_orders", __name__)

orders_col        = db["orders"]
users_col         = db["users"]
balances_col      = db["balances"]         # for refunds
transactions_col  = db["transactions"]     # for refund ledger
schedules_col     = db["order_schedules"]  # NEW: persistent job queue
auto_update_rules_col = db["order_auto_update_rules"]
services_col      = db["services"]
order_export_batches_col = db["order_export_batches"]
afa_col           = db["afa_registrations"]
complaints_col    = db["complaints"]
PAYMENT_CONFIRMED_STATUS = "payment_confirmed"
PAYMENT_CONFIRMED_REPLY = "Payment Confirmed, order can be processed"
FALSE_COMPLAINT_REPLY = "False complaint"
stores_col        = db["stores"]


def _clear_dashboard_cache_safely():
    try:
        from admin_dashboard import clear_dashboard_cache

        clear_dashboard_cache()
    except Exception:
        pass


# Keep legacy; primary set includes refunded
ALLOWED_STATUSES   = {"pending", "processing", "delivered", "failed", "completed", "refunded", "cancelled", "canceled"}
STATUS_FILTER_GROUPS = {
    "delivered_processing": ["delivered", "processing"],
}
ALLOWED_SORTS      = {"newest", "oldest", "amount_desc", "amount_asc"}
DEFAULT_PER_PAGE   = 10
FINAL_STATUS       = "completed"
FINAL_STATUSES     = {"delivered", "completed", "refunded", "cancelled", "canceled"}
API_PROVIDER_LABELS = {
    "codecraft": "CodeCraft",
    "dataconnect": "DataConnect",
    "datakazina": "DataKazina",
    "portal02": "Portal-02",
    "skplug": "SKPlug",
    "exosupplier": "ExoSupplier",
    "bundleportal": "BundlePortal",
}
API_PROVIDERS = set(API_PROVIDER_LABELS)
BOOSTING_PROVIDER = "exosupplier"
NON_BOOSTING_ORDER_CLAUSE = {"items": {"$elemMatch": {"provider": {"$ne": BOOSTING_PROVIDER}}}}
ALLOWED_TRANSITIONS = {
    "pending": {"processing", "refunded"},
    "processing": {"delivered", "failed", "refunded"},
    "delivered": {"refunded"},
    "failed": set(),
    "refunded": set(),
    "cancelled": set(),
    "canceled": set(),
    "completed": set(),
}


def _normalize_status(s: str | None) -> str:
    val = (s or "").strip().lower()
    if val == "completed":
        return "delivered"
    return val


def _compute_order_status_from_items(items: List[Dict[str, Any]], current_status: str | None = None, allow_final_recompute: bool = False) -> str:
    current = _normalize_status(current_status)
    if current in FINAL_STATUSES and not allow_final_recompute:
        return current

    statuses = [_normalize_status(i.get("line_status")) for i in (items or [])]
    if not statuses:
        return "processing"
    if all(s == "delivered" for s in statuses):
        return "delivered"
    if all(s == "pending" for s in statuses):
        return "pending"
    if all(s == "refunded" for s in statuses):
        return "refunded"
    if all(s in {"cancelled", "canceled"} for s in statuses):
        return "cancelled"
    if any(s in {"processing", "queued"} for s in statuses):
        return "processing"
    if all(s == "failed" for s in statuses):
        return "failed"
    return "processing"

# Admin cancel window (seconds)
CANCEL_WINDOW_SECONDS = 60

# --------- CACHING (Render-safe) ----------
_ORDERS_CACHE_TTL_SECONDS = 45
_ORDERS_CACHE_MAX_ITEMS = 512

class _MemoryTTLCache:
    def __init__(self, max_items: int = _ORDERS_CACHE_MAX_ITEMS):
        self.max_items = max_items
        self._lock = threading.Lock()
        self._store = OrderedDict()  # key -> (expires_at, value)

    def get(self, key: str):
        now = time.time()
        with self._lock:
            if key not in self._store:
                return None
            exp, val = self._store[key]
            if exp < now:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return val

    def set(self, key: str, value, ttl: int):
        exp = time.time() + max(1, int(ttl))
        with self._lock:
            self._store[key] = (exp, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_items:
                self._store.popitem(last=False)

_memory_cache = _MemoryTTLCache()
_redis_client = None

def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    redis_url = os.getenv("REDIS_URL") or ""
    if not redis_url:
        _redis_client = False
        return _redis_client
    try:
        import redis  # type: ignore
        _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception:
        _redis_client = False
    return _redis_client

def get_cached_json(key: str):
    client = _get_redis_client()
    if client:
        try:
            raw = client.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception:
            return None
    return _memory_cache.get(key)

def set_cached_json(key: str, value, ttl: int = _ORDERS_CACHE_TTL_SECONDS):
    client = _get_redis_client()
    if client:
        try:
            client.setex(key, int(ttl), json.dumps(value, separators=(",", ":"), ensure_ascii=False))
            return
        except Exception:
            pass
    _memory_cache.set(key, value, ttl)

def _jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")

def _can_transition(old_status: str, new_status: str) -> bool:
    if old_status == new_status:
        return True
    if old_status in FINAL_STATUSES and not (old_status == "delivered" and new_status == "refunded"):
        return False
    return new_status in ALLOWED_TRANSITIONS.get(old_status, set())

def _log_status_blocked(order, attempted_status: str, reason: str, source: str, actor_admin_id=None):
    _jlog(
        "order_status_blocked",
        order_id=order.get("order_id"),
        mongo_id=str(order.get("_id")),
        attempted_status=attempted_status,
        current_status=(order.get("status") or ""),
        reason=reason,
        source=source,
        actor_admin_id=actor_admin_id,
    )

# --------- HELPERS ----------
def _parse_date(dstr):
    if not dstr:
        return None
    try:
        s = dstr.strip()
        if len(s) <= 10:
            return datetime.strptime(s, "%Y-%m-%d")
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return None

def _parse_time_hm(tstr):
    if not tstr:
        return None
    try:
        return datetime.strptime(tstr.strip(), "%H:%M").time()
    except Exception:
        return None

def _fmt_complaint_dt(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if value:
        return str(value)
    return ""

def _complaint_admin_label(admin_doc: dict | None) -> str:
    admin_doc = admin_doc or {}
    name = f"{admin_doc.get('first_name', '')} {admin_doc.get('last_name', '')}".strip()
    return name or admin_doc.get("business_name") or admin_doc.get("username") or admin_doc.get("email") or ""

def _serialize_main_admin_complaint(c: dict, admin_doc: dict | None = None) -> dict:
    can_process_store_order = bool(c.get("store_slug") and c.get("cart_snapshot") and c.get("payment_confirmed"))
    shots = c.get("screenshots") or {}
    order_ref = c.get("order_ref") or {}
    return {
        "id": str(c.get("_id")),
        "customer_name": c.get("customer_name") or "",
        "customer_phone": c.get("customer_phone") or "",
        "admin_name": _complaint_admin_label(admin_doc),
        "admin_phone": (admin_doc or {}).get("phone") or "",
        "store_name": c.get("store_name") or "",
        "store_slug": c.get("store_slug") or "",
        "order_ref": c.get("order_number_provided") or order_ref.get("order_id") or order_ref.get("order_no") or "",
        "flagged_ref_order_id": c.get("flagged_ref_order_id") or "",
        "paystack_reference": c.get("paystack_reference") or "",
        "payment_date": c.get("payment_date_str") or _fmt_complaint_dt(c.get("payment_date_dt")) or _fmt_complaint_dt(c.get("payment_date")),
        "service_name": c.get("service_name") or "",
        "offer": c.get("offer") or "",
        "cart_total": c.get("cart_total") or 0,
        "message": c.get("message") or c.get("description") or "",
        "main_admin_reply": c.get("main_admin_reply") or "",
        "payment_confirmed": bool(c.get("payment_confirmed")),
        "can_process_store_order": can_process_store_order,
        "status": c.get("status") or "pending",
        "submitted_at": _fmt_complaint_dt(c.get("submitted_at")),
        "resolved_at": _fmt_complaint_dt(c.get("main_admin_resolved_at") or c.get("updated_at")),
        "proofs": {
            "data_balance": shots.get("data_balance") or "",
            "phone_msisdn": shots.get("phone_msisdn") or "",
            "image_path": c.get("image_path") or "",
        },
    }

def _build_preserved_query(args, exclude=("page",)):
    kept = {k: v for k, v in args.items() if k not in exclude and v not in (None, "", "None")}
    return urlencode(kept)

def _append_and(query: dict, clause: dict) -> dict:
    query["$and"] = (query.get("$and") or []) + [clause]
    return query

def _is_boosting_item(item: dict) -> bool:
    return (item.get("provider") or "").strip().lower() == BOOSTING_PROVIDER

def _visible_order_items(order: dict) -> list:
    return [item for item in (order.get("items") or []) if not _is_boosting_item(item)]

def _item_amount_total(items: list) -> float:
    total = 0.0
    for item in items:
        try:
            total += float(item.get("amount") or 0)
        except Exception:
            pass
    return round(total, 2)

def _build_query_from_params(args):
    """Central builder so list + bulk share identical filters."""
    status_filter = (args.get("status") or "").strip().lower()
    order_id_q    = (args.get("order_id") or "").strip()
    customer_q    = (args.get("customer") or "").strip()
    paid_from     = (args.get("paid_from") or "").strip().lower()
    min_total     = (args.get("min_total") or "").strip()
    max_total     = (args.get("max_total") or "").strip()
    date_from     = _parse_date((args.get("date_from") or "").strip())
    date_to_raw   = _parse_date((args.get("date_to") or "").strip())
    time_from     = _parse_time_hm((args.get("time_from") or "").strip())
    time_to       = _parse_time_hm((args.get("time_to") or "").strip())
    date_to       = datetime(date_to_raw.year, date_to_raw.month, date_to_raw.day) + timedelta(days=1) if date_to_raw else None

    item_service  = (args.get("item_service") or "").strip()
    item_offer    = (args.get("item_offer") or "").strip()
    item_phone    = (args.get("item_phone") or "").strip()
    agent_q       = (args.get("agent") or "").strip()

    query = {}
    admin_oid = current_admin_id_from_session(session)
    if admin_oid and (session.get("role") or "").strip().lower() != "main_admin":
        query["admin_id"] = admin_oid

    if status_filter in STATUS_FILTER_GROUPS:
        query["status"] = {"$in": STATUS_FILTER_GROUPS[status_filter]}
    elif status_filter and status_filter in ALLOWED_STATUSES:
        query["status"] = status_filter
    if paid_from:
        if paid_from == "paystack":
            query["paid_from"] = {"$in": ["paystack", "paystack_inline"]}
        else:
            query["paid_from"] = paid_from
    if order_id_q:
        query["order_id"] = Regex(order_id_q, "i")

    if time_from or time_to:
        start_day = date_from or datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = date_to_raw or start_day
        start_time = time_from or datetime.min.time()
        end_time = time_to or datetime.max.time().replace(microsecond=0)
        start_dt = datetime.combine(start_day.date(), start_time)
        end_dt = datetime.combine(end_day.date(), end_time)
        if end_dt < start_dt:
            end_dt = end_dt + timedelta(days=1)
        query["created_at"] = {"$gte": start_dt, "$lte": end_dt}
    elif date_from or date_to:
        dt = {}
        if date_from: dt["$gte"] = date_from
        if date_to:   dt["$lt"]  = date_to
        query["created_at"] = dt

    amt = {}
    try:
        if min_total != "": amt["$gte"] = float(min_total)
    except Exception:
        pass
    try:
        if max_total != "": amt["$lte"] = float(max_total)
    except Exception:
        pass
    if amt:
        query["total_amount"] = amt

    if customer_q:
        rx = Regex(customer_q, "i")
        user_ids = [u["_id"] for u in users_col.find(
            {"$or": [
                {"first_name": rx}, {"last_name": rx}, {"email": rx},
                {"phone": rx}, {"username": rx},
            ]},
            {"_id": 1},
        )]
        customer_clauses = [
            {"items.phone": rx},
            {"customer_name": rx},
            {"customer_phone": rx},
            {"phone": rx},
        ]
        if user_ids:
            customer_clauses.append({"user_id": {"$in": user_ids}})
        query["$or"] = customer_clauses

    if agent_q:
        rx = Regex(agent_q, "i")
        agent_ids = [u["_id"] for u in users_col.find(
            {"$or": [
                {"first_name": rx}, {"last_name": rx}, {"email": rx},
                {"phone": rx}, {"username": rx}, {"business_name": rx},
            ]},
            {"_id": 1},
        )]
        agent_store_slugs = []
        if agent_ids:
            agent_store_slugs = [
                s.get("slug")
                for s in stores_col.find({"owner_id": {"$in": agent_ids}}, {"slug": 1})
                if s.get("slug")
            ]
        if agent_ids:
            agent_clauses = [{"user_id": {"$in": agent_ids}}]
            if agent_store_slugs:
                agent_clauses.append({"store_slug": {"$in": agent_store_slugs}})
            _append_and(query, {"$or": agent_clauses})
        else:
            _append_and(query, {"user_id": {"$in": []}})

    item_and = []
    if item_service: item_and.append({"items.serviceName": Regex(item_service, "i")})
    if item_offer:   item_and.append({"items.value": Regex(item_offer, "i")})
    if item_phone:   item_and.append({"items.phone": Regex(item_phone, "i")})
    if item_and:
        query["$and"] = (query.get("$and") or []) + item_and

    _append_and(query, NON_BOOSTING_ORDER_CLAUSE)
    return query

def _apply_api_filter(query: dict, api_filter: str) -> None:
    if api_filter in {"passed", "not_passed"}:
        api_elem = {
            "items": {
                "$elemMatch": {
                    "provider": {"$in": sorted(API_PROVIDERS)},
                    "api_status": {"$ne": "skipped"},
                    "line_status": {"$nin": ["skipped_duplicate_processing", "skipped_duplicate_in_cart"]},
                }
            }
        }
        if api_filter == "passed":
            _append_and(query, api_elem)
        else:
            _append_and(query, {"$nor": [api_elem]})

def _format_dt(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")

def _cancel_seconds_left(created_at: datetime | None) -> int:
    if not isinstance(created_at, datetime):
        return 0
    age = (datetime.utcnow() - created_at).total_seconds()
    remaining = int(CANCEL_WINDOW_SECONDS - age)
    return max(0, remaining)

def _cancel_allowed(created_at: datetime | None, status: str | None) -> bool:
    if _cancel_seconds_left(created_at) <= 0:
        return False
    st = (status or "").strip().lower()
    if st in FINAL_STATUSES or st == "failed":
        return False
    return True

def _serialize_user(user: dict) -> dict:
    return {
        "first_name": user.get("first_name") or "",
        "last_name": user.get("last_name") or "",
        "business_name": user.get("business_name") or "",
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "username": user.get("username") or "",
        "role": user.get("role") or "",
    }

def _serialize_item(item: dict) -> dict:
    return {
        "serviceName": item.get("serviceName") or "",
        "value": item.get("value") or "",
        "phone": item.get("phone") or "",
        "amount": item.get("amount") or 0,
        "profit_amount": item.get("admin_profit") if item.get("admin_profit") is not None else item.get("profit_amount") or 0,
        "provider": item.get("provider") or "",
        "api_status": item.get("api_status") or "",
        "line_status": item.get("line_status") or "",
        "target_link": item.get("target_link") or "",
        "quantity": item.get("quantity") or "",
        "provider_order_id": item.get("provider_order_id") or "",
    }

def _serialize_order(order: dict) -> dict:
    created_text = _format_dt(order.get("created_at"))
    cancel_left = _cancel_seconds_left(order.get("created_at"))
    cancel_allowed = _cancel_allowed(order.get("created_at"), order.get("status"))
    visible_items = _visible_order_items(order)
    return {
        "order_id": order.get("order_id"),
        "order_id_param": order.get("order_id_param"),
        "order_id_key": str(order.get("_id")) if order.get("_id") is not None else "",
        "source": order.get("source") or "main",
        "user": _serialize_user(order.get("user") or {}),
        "admin_user": _serialize_user(order.get("admin_user") or {}),
        "items": [_serialize_item(i) for i in visible_items],
        "total_amount": _item_amount_total(visible_items),
        "profit_amount_total": order.get("admin_profit_total") if order.get("admin_profit_total") is not None else order.get("profit_amount_total") or 0,
        "status": order.get("status") or "",
        "paid_from": order.get("paid_from") or "",
        "api_passed": bool(order.get("api_passed")),
        "api_providers": order.get("api_providers") or [],
        "created_at_display": created_text,
        "created_at_text": created_text,
        "created_at_iso": order.get("created_at").isoformat() if order.get("created_at") else None,
        "cancel_allowed": bool(cancel_allowed),
        "cancel_expires_in": int(cancel_left),
    }

def _serialize_line(line: dict) -> dict:
    created_text = _format_dt(line.get("created_at"))
    cancel_left = _cancel_seconds_left(line.get("created_at"))
    cancel_allowed = _cancel_allowed(line.get("created_at"), line.get("status"))
    return {
        "order_id": line.get("order_id"),
        "order_mongo_id_param": line.get("order_mongo_id_param"),
        "item_index": line.get("item_index"),
        "line_id": line.get("line_id"),
        "source": line.get("source") or "main",
        "user": _serialize_user(line.get("user") or {}),
        "admin_user": _serialize_user(line.get("admin_user") or {}),
        "item": _serialize_item(line.get("item") or {}),
        "profit_amount_total": line.get("profit_amount_total") or 0,
        "status": line.get("status") or "",
        "created_at_display": created_text,
        "created_at_text": created_text,
        "created_at_iso": line.get("created_at").isoformat() if line.get("created_at") else None,
        "cancel_allowed": bool(cancel_allowed),
        "cancel_expires_in": int(cancel_left),
    }

def _to_oid(value):
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None

def _load_store_owner_map(orders: List[dict]) -> dict:
    slugs = sorted({str(o.get("store_slug") or "").strip() for o in orders if str(o.get("store_slug") or "").strip()})
    if not slugs:
        return {}
    store_docs = stores_col.find({"slug": {"$in": slugs}}, {"slug": 1, "owner_id": 1})
    return {s.get("slug"): _to_oid(s.get("owner_id")) for s in store_docs if s.get("slug") and _to_oid(s.get("owner_id"))}

def _order_agent_id(order: dict, store_owner_map: dict | None = None):
    slug = str(order.get("store_slug") or "").strip()
    if slug and store_owner_map and store_owner_map.get(slug):
        return store_owner_map.get(slug)
    return _to_oid(order.get("store_owner_id") or order.get("user_id"))

def _load_users_for_orders(orders: List[dict], store_owner_map: dict | None = None) -> dict:
    ids = []
    for o in orders:
        uid = _order_agent_id(o, store_owner_map)
        if uid:
            ids.append(uid)
    if not ids:
        return {}
    unique_ids = list({i for i in ids})
    users = users_col.find(
        {"_id": {"$in": unique_ids}},
        {"first_name": 1, "last_name": 1, "business_name": 1, "email": 1, "phone": 1, "username": 1, "role": 1},
    )
    return {u["_id"]: u for u in users}


def _load_admins_for_orders(orders: List[dict]) -> dict:
    ids = []
    for o in orders:
        aid = o.get("admin_id")
        if isinstance(aid, str):
            try:
                aid = ObjectId(aid)
            except Exception:
                aid = None
        if aid:
            ids.append(aid)
    if not ids:
        return {}
    unique_ids = list({i for i in ids})
    admins = users_col.find(
        {"_id": {"$in": unique_ids}},
        {"first_name": 1, "last_name": 1, "business_name": 1, "email": 1, "phone": 1, "username": 1, "role": 1},
    )
    return {u["_id"]: u for u in admins}

def _build_orders_cache_key(args) -> str:
    keys = [
        "source", "view", "page", "per_page", "sort", "status", "customer", "order_id",
        "paid_from", "min_total", "max_total", "date_from", "date_to", "time_from", "time_to",
        "item_service", "item_offer", "item_phone", "agent", "api_filter",
    ]
    normalized = {}
    admin_oid = current_admin_id_from_session(session)
    normalized["admin_id"] = str(admin_oid) if admin_oid else ""
    for k in keys:
        normalized[k] = (args.get(k) or "").strip()
    normalized["source"] = _normalize_source_filter(normalized.get("source"))
    normalized["view"] = (normalized.get("view") or "lines").strip().lower()
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return f"admin_orders:data:{payload}"

def _etag_for_payload(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"\"{digest}\""

def _build_orders_data_payload(args) -> dict:
    sort = (args.get("sort") or "newest").strip().lower()
    if sort not in ALLOWED_SORTS:
        sort = "newest"

    try:
        per_page = int(args.get("per_page", DEFAULT_PER_PAGE))
        per_page = max(1, min(per_page, 100))
    except Exception:
        per_page = DEFAULT_PER_PAGE

    try:
        page = int(args.get("page", 1))
        page = max(1, page)
    except Exception:
        page = 1

    skip = (page - 1) * per_page
    query = _build_query_from_params(args)
    api_filter = (args.get("api_filter") or "").strip().lower()
    _apply_api_filter(query, api_filter)

    sort_spec = [("created_at", -1), ("_id", -1)]
    if sort == "oldest":
        sort_spec = [("created_at", 1), ("_id", 1)]
    elif sort == "newest":
        sort_spec = [("created_at", -1), ("_id", -1)]
    elif sort == "amount_desc":
        sort_spec = [("total_amount", -1), ("created_at", -1), ("_id", -1)]
    elif sort == "amount_asc":
        sort_spec = [("total_amount", 1), ("created_at", 1), ("_id", 1)]

    view_mode = (args.get("view") or "lines").strip().lower()
    if view_mode not in {"lines", "orders"}:
        view_mode = "lines"

    projection = {
        "order_id": 1,
        "user_id": 1,
        "admin_id": 1,
        "items": 1,
        "paid_from": 1,
        "created_at": 1,
        "status": 1,
        "total_amount": 1,
        "profit_amount": 1,
        "profit_amount_total": 1,
        "main_admin_profit_total": 1,
        "admin_profit_total": 1,
        "store_profit_total": 1,
        "charged_amount": 1,
        "delivered_at": 1,
        "refunded_at": 1,
        "store_slug": 1,
        "store_owner_id": 1,
    }

    orders = []
    order_lines = []
    total_orders = 0
    total_pages = 1

    total_orders = orders_col.count_documents(query)
    total_pages = max(1, (total_orders + per_page - 1) // per_page)
    orders = list(orders_col.find(query, projection).sort(sort_spec).skip(skip).limit(per_page))
    store_owner_map = _load_store_owner_map(orders)
    user_map = _load_users_for_orders(orders, store_owner_map=store_owner_map)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    admin_map = _load_admins_for_orders(orders) if is_main_admin else {}
    for o in orders:
        _prepare_order(o, "main", order_lines, orders_col, user_map=user_map, admin_map=admin_map, include_admin=is_main_admin, store_owner_map=store_owner_map)

    payload = {
        "ok": True,
        "view_mode": view_mode,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_orders": total_orders,
        "order_lines_count": len(order_lines) if view_mode != "orders" else 0,
    }
    if view_mode == "orders":
        payload["orders"] = [_serialize_order(o) for o in orders]
        payload["order_lines"] = []
    else:
        payload["order_lines"] = [_serialize_line(l) for l in order_lines]
        payload["orders"] = []
    return payload

def _json_with_cache(payload: dict, etag: str | None):
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "private, max-age=30"
    if etag:
        resp.headers["ETag"] = etag
    return resp

def _require_admin():
    return session.get("role") in {"admin", "main_admin"}


def _require_main_admin():
    return _require_admin() and _is_main_admin_session()


def _cron_runner_token() -> str:
    return (os.getenv("ORDER_AUTOMATION_TOKEN") or os.getenv("AUTO_UPDATE_CRON_TOKEN") or "").strip()


def _cron_runner_authorized() -> bool:
    token = _cron_runner_token()
    if not token:
        return False
    candidates = [
        request.headers.get("X-Order-Automation-Token"),
        request.headers.get("X-Cron-Token"),
        request.headers.get("Authorization"),
        request.args.get("token"),
        request.form.get("token"),
    ]
    for raw in candidates:
        value = (raw or "").strip()
        if not value:
            continue
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value == token:
            return True
    return False

def _money(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def _compute_api_fields(order: dict) -> None:
    items = order.get("items") or []
    providers = set()
    for item in items:
        if _is_boosting_item(item):
            continue
        prov = (item.get("provider") or "").strip().lower()
        if prov not in API_PROVIDERS:
            continue
        if (item.get("api_status") or "").strip().lower() == "skipped":
            continue
        line_status = (item.get("line_status") or "").strip().lower()
        if line_status in ("skipped_duplicate_processing", "skipped_duplicate_in_cart"):
            continue
        providers.add(prov)

    order["api_passed"] = bool(providers)
    order["api_providers"] = sorted(providers)
    labels = [API_PROVIDER_LABELS.get(p, p) for p in order["api_providers"]]
    order["api_providers_label"] = ", ".join(labels)

def _normalize_line_status(s: str | None) -> str:
    try:
        return _normalize_status(s)
    except Exception:
        return (s or "").strip().lower()

def _normalize_source(src: str | None, default: str = "main") -> str:
    return "main"

def _normalize_source_filter(src: str | None) -> str:
    return "main"

# --------- EXPORT HELPERS ----------
NETWORK_ID_TO_NAME = {
    1: "AIRTELTIGO",
    2: "VODAFONE",
    3: "MTN",
}

SKIP_LINE_STATUSES = {
    "skipped",
    "skipped_duplicate_processing",
    "skipped_duplicate_in_cart",
}
AUTO_UPDATE_ELIGIBLE_STATUSES = {"pending", "processing"}
_AUTO_UPDATE_PROCESS_INTERVAL_SECONDS = 30
_auto_update_process_lock = threading.Lock()
_auto_update_last_run_ts = 0.0


def _ensure_auto_update_rule_indexes():
    try:
        auto_update_rules_col.create_index([("state", 1), ("target_status", 1), ("updated_at", -1)], background=True)
    except Exception:
        pass


_ensure_auto_update_rule_indexes()

def _to_objectid(value):
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _is_main_admin_session() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"

def _normalize_network_name(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return "UNKNOWN"
    key = re.sub(r"[^a-z0-9]+", "", s.lower())
    if "mtn" in key:
        return "MTN"
    if "telecel" in key:
        return "TELECEL"
    if "vodafone" in key:
        return "VODAFONE"
    if "airteltigo" in key or ("airtel" in key and "tigo" in key) or "tigo" in key:
        return "AIRTELTIGO"
    return s.upper()

def _extract_service_network(doc: dict | None) -> str:
    if not doc:
        return "UNKNOWN"
    for key in ("service_network", "network", "network_name", "provider_network"):
        val = doc.get(key)
        if val not in (None, ""):
            return _normalize_network_name(str(val))
    name = (doc.get("name") or "").strip()
    return _normalize_network_name(name) if name else "UNKNOWN"

def _collect_export_catalog(admin_oid: ObjectId | None, is_main_admin: bool) -> Dict[str, Any]:
    if is_main_admin:
        query = {}
    elif admin_oid:
        query = {"$or": [{"admin_id": admin_oid}, {"admin_id": {"$exists": False}}, {"admin_id": None}]}
    else:
        query = {"_id": {"$exists": False}}

    services = []
    networks = set()
    service_network_map = {}
    for doc in services_col.find(query, {"name": 1, "service_network": 1, "network": 1}):
        name = (doc.get("name") or "").strip()
        if not name:
            continue
        net = _extract_service_network(doc)
        services.append(name)
        if net and net != "UNKNOWN":
            networks.add(net)
        service_network_map[name] = net

    services = sorted(set(services))
    networks = sorted(networks)
    return {
        "services": services,
        "networks": networks,
        "service_network_map": service_network_map,
    }


def _clean_service_names(raw_values: List[str]) -> List[str]:
    seen = set()
    cleaned = []
    for value in raw_values or []:
        name = str(value or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def _parse_auto_rule_statuses(raw_values: List[str] | None) -> List[str]:
    parsed = []
    for value in raw_values or []:
        st = _normalize_status(value)
        if st in AUTO_UPDATE_ELIGIBLE_STATUSES and st not in parsed:
            parsed.append(st)
    return parsed or ["processing"]


def _service_line_ids_for_auto_rule(order: dict, service_names_lower: set[str], eligible_statuses: set[str]) -> List[str]:
    oid = order.get("_id")
    if not oid:
        return []
    order_status = _normalize_status(order.get("status"))
    line_ids: List[str] = []
    for idx, item in enumerate(order.get("items") or []):
        svc_name = (item.get("serviceName") or "").strip()
        if not svc_name or svc_name.lower() not in service_names_lower:
            continue
        line_status = _normalize_line_status(item.get("line_status")) or order_status
        if line_status in SKIP_LINE_STATUSES or _is_final_line_status(line_status):
            continue
        if line_status not in eligible_statuses:
            continue
        line_ids.append(f"{oid}:{idx}")
    return line_ids


def _serialize_auto_update_rule(rule: dict) -> dict:
    return {
        "id": str(rule.get("_id")),
        "name": rule.get("name") or "",
        "service_names": rule.get("service_names") or [],
        "service_count": len(rule.get("service_names") or []),
        "threshold_minutes": int(rule.get("threshold_minutes") or 0),
        "eligible_statuses": rule.get("eligible_statuses") or ["processing"],
        "state": rule.get("state") or "paused",
        "last_run_at": rule.get("last_run_at").strftime("%Y-%m-%d %H:%M:%S UTC") if rule.get("last_run_at") else None,
        "last_match_count": int(rule.get("last_match_count") or 0),
        "last_update_count": int(rule.get("last_update_count") or 0),
        "run_count": int(rule.get("run_count") or 0),
        "note": rule.get("note") or "",
        "created_at": rule.get("created_at").strftime("%Y-%m-%d %H:%M:%S UTC") if rule.get("created_at") else None,
    }


def _process_auto_update_rules(
    *,
    force: bool = False,
    max_rules: int = 20,
    max_orders_per_rule: int = 500,
    specific_rule_ids: List[ObjectId] | None = None,
) -> Dict[str, int]:
    global _auto_update_last_run_ts
    now_ts = time.time()
    if not force and (now_ts - _auto_update_last_run_ts) < _AUTO_UPDATE_PROCESS_INTERVAL_SECONDS:
        return {"processed_rules": 0, "matched_lines": 0, "updated_lines": 0}

    if not _auto_update_process_lock.acquire(blocking=False):
        return {"processed_rules": 0, "matched_lines": 0, "updated_lines": 0}

    try:
        now = datetime.utcnow()
        if not force and (time.time() - _auto_update_last_run_ts) < _AUTO_UPDATE_PROCESS_INTERVAL_SECONDS:
            return {"processed_rules": 0, "matched_lines": 0, "updated_lines": 0}

        processed_rules = 0
        matched_lines = 0
        updated_lines = 0
        rule_query: Dict[str, Any] = {"state": "active", "target_status": "delivered"}
        if specific_rule_ids:
            rule_query["_id"] = {"$in": specific_rule_ids}
        rules = list(
            auto_update_rules_col.find(rule_query)
            .sort([("updated_at", -1), ("created_at", -1)])
            .limit(max_rules)
        )

        for rule in rules:
            processed_rules += 1
            threshold_minutes = max(1, int(rule.get("threshold_minutes") or 0))
            service_names = _clean_service_names(rule.get("service_names") or [])
            eligible_statuses = set(_parse_auto_rule_statuses(rule.get("eligible_statuses") or []))
            if not service_names:
                auto_update_rules_col.update_one(
                    {"_id": rule["_id"]},
                    {"$set": {"state": "paused", "updated_at": now, "last_error": "No services configured."}},
                )
                continue

            cutoff = now - timedelta(minutes=threshold_minutes)
            query = {
                "created_at": {"$lte": cutoff},
                "status": {"$nin": list(FINAL_STATUSES)},
                "items.serviceName": {"$in": service_names},
            }
            projection = {"_id": 1, "status": 1, "items": 1}
            order_docs = list(
                orders_col.find(query, projection)
                .sort([("created_at", 1), ("_id", 1)])
                .limit(max_orders_per_rule)
            )

            line_ids: List[str] = []
            service_names_lower = {s.lower() for s in service_names}
            for order in order_docs:
                line_ids.extend(_service_line_ids_for_auto_rule(order, service_names_lower, eligible_statuses))

            line_ids = list(dict.fromkeys(line_ids))
            matched_count = len(line_ids)
            matched_lines += matched_count

            updated_count = 0
            errors: List[str] = []
            if line_ids:
                updated_count, errors = _apply_line_status_change(
                    line_ids,
                    "delivered",
                    reason=f"auto_update_rule:{rule.get('_id')}",
                    actor_admin_id=rule.get("created_by"),
                    orders_collection=orders_col,
                    target_source="main",
                )
            updated_lines += updated_count

            auto_update_rules_col.update_one(
                {"_id": rule["_id"]},
                {
                    "$set": {
                        "last_run_at": now,
                        "last_match_count": matched_count,
                        "last_update_count": updated_count,
                        "last_error": "; ".join(errors[:5]) if errors else "",
                        "updated_at": now,
                    },
                    "$inc": {"run_count": 1},
                },
            )

        _auto_update_last_run_ts = time.time()
        return {
            "processed_rules": processed_rules,
            "matched_lines": matched_lines,
            "updated_lines": updated_lines,
        }
    finally:
        _auto_update_process_lock.release()

def _parse_dt_any(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _parse_time_hhmm(raw: str | None, default_time) -> datetime.time:
    if not raw:
        return default_time
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except Exception:
        return default_time

def _parse_export_timeframe(payload: dict) -> Tuple[datetime, datetime, str]:
    timeframe = (payload.get("timeframe") or "today").strip().lower()
    if timeframe not in {"today", "custom"}:
        timeframe = "today"

    if timeframe == "today":
        today = datetime.utcnow().date()
        start_t = _parse_time_hhmm(payload.get("today_start_time"), datetime.strptime("00:00", "%H:%M").time())
        end_t = _parse_time_hhmm(payload.get("today_end_time"), datetime.strptime("23:59", "%H:%M").time())
        start_dt = datetime.combine(today, start_t)
        end_dt = datetime.combine(today, end_t)
        if end_dt < start_dt:
            end_dt = end_dt + timedelta(days=1)
        return start_dt, end_dt, "today"

    date_from = _parse_dt_any(payload.get("date_from"))
    date_to = _parse_dt_any(payload.get("date_to"))
    if not date_from or not date_to:
        raise ValueError("Custom range requires date_from and date_to.")
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to, "custom"

def _build_export_source_filter(source: str | None) -> dict:
    s = (source or "").strip().lower()
    if not s or s in {"all", "any"}:
        return {}
    if s == "main":
        return {"$or": [{"source": "main"}, {"source": {"$exists": False}}]}
    return {"source": s}

def _resolve_item_network(item: dict, service_network_map: Dict[str, str]) -> str:
    for key in ("provider_network", "network", "network_name", "service_network"):
        val = item.get(key)
        if val not in (None, ""):
            return _normalize_network_name(str(val))

    nid = item.get("network_id")
    if isinstance(nid, (int, float)) and int(nid) in NETWORK_ID_TO_NAME:
        return NETWORK_ID_TO_NAME.get(int(nid)) or "UNKNOWN"

    svc = (item.get("serviceName") or "").strip()
    if svc and svc in service_network_map:
        return service_network_map.get(svc) or "UNKNOWN"

    return "UNKNOWN"

def _extract_offer(item: dict) -> str:
    val = item.get("value")
    if val not in (None, ""):
        return str(val)
    vo = item.get("value_obj") or {}
    for key in ("label", "name", "value"):
        if vo.get(key) not in (None, ""):
            return str(vo.get(key))
    return ""


def _export_offer_text(offer: Any) -> str:
    text = str(offer or "").strip()
    if not text:
        return ""
    return re.sub(r"^\s*(\d+(?:\.\d+)?)\s*GB\s*$", r"\1", text, flags=re.IGNORECASE)

def _is_exportable_line(item: dict) -> bool:
    raw = (item.get("line_status") or "").strip().lower()
    if raw in SKIP_LINE_STATUSES:
        return False
    if _normalize_line_status(raw) == "delivered":
        return False
    return True

def _collect_undelivered_export_rows(
    admin_oid: ObjectId | None,
    is_main_admin: bool,
    service_names: List[str],
    network_filter: str,
    source_filter: str,
    start_dt: datetime,
    end_dt: datetime,
    service_network_map: Dict[str, str],
) -> List[dict]:
    query = {"created_at": {"$gte": start_dt, "$lte": end_dt}}
    if not is_main_admin and admin_oid:
        query["admin_id"] = admin_oid

    src_clause = _build_export_source_filter(source_filter)
    if src_clause:
        query.update(src_clause)

    svc_set = {s.strip().lower() for s in service_names if s.strip()}
    if svc_set:
        query["items.serviceName"] = {"$in": service_names}

    projection = {"order_id": 1, "items": 1, "status": 1, "created_at": 1, "source": 1}
    rows = []
    for order in orders_col.find(query, projection).sort([("created_at", 1), ("_id", 1)]):
        order_source = (order.get("source") or "main").strip().lower()
        items = order.get("items") or []
        for idx, item in enumerate(items):
            if _is_boosting_item(item):
                continue
            if not _is_exportable_line(item):
                continue
            svc_name = (item.get("serviceName") or "").strip()
            if svc_set and svc_name.lower() not in svc_set:
                continue
            net = _resolve_item_network(item, service_network_map)
            if network_filter and network_filter not in {"ANY", "ALL"} and net != network_filter:
                continue
            line_status = _normalize_line_status(item.get("line_status"))
            rows.append({
                "source": order_source,
                "order_id": order.get("order_id"),
                "line_id": f"{order.get('_id')}:{idx}",
                "service": svc_name,
                "offer": _extract_offer(item),
                "network": net,
                "phone": (item.get("phone") or "").strip(),
                "line_status": line_status or "",
                "order_status": _normalize_status(order.get("status") or ""),
                "created_at": order.get("created_at"),
            })
    return rows

def _next_badge_no() -> int:
    last = order_export_batches_col.find_one({}, sort=[("badge_no", -1)], projection={"badge_no": 1})
    try:
        return int(last.get("badge_no") or 0) + 1 if last else 1
    except Exception:
        return 1

def _save_export_batch(
    service_names: List[str],
    network: str,
    source: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    count: int,
    fmt: str,
    rows: List[dict],
    admin_id: ObjectId | None,
    created_by: ObjectId | None,
) -> dict:
    badge_no = _next_badge_no()
    label = f"Badge {badge_no}"
    service_label = ", ".join(service_names) if service_names else "All Services"

    lines = []
    for r in rows:
        lines.append({
            "line_id": r.get("line_id"),
            "phone": r.get("phone"),
            "offer": r.get("offer"),
            "network": r.get("network"),
            "source": r.get("source"),
            "order_id": r.get("order_id"),
        })

    doc = {
        "badge_no": badge_no,
        "label": label,
        "service_name": service_label,
        "service_names": service_names,
        "network": network,
        "source": source,
        "timeframe": timeframe,
        "date_from": start_dt,
        "date_to": end_dt,
        "count": count,
        "format": fmt,
        "created_at": datetime.utcnow(),
        "created_by": created_by,
        "admin_id": admin_id,
        "lines": lines,
    }
    order_export_batches_col.insert_one(doc)
    return doc

def _serialize_export_batch_summary(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id")),
        "badge_no": doc.get("badge_no"),
        "label": doc.get("label"),
        "service_name": doc.get("service_name"),
        "network": doc.get("network"),
        "source": doc.get("source"),
        "timeframe": doc.get("timeframe"),
        "date_from": _format_dt(doc.get("date_from")),
        "date_to": _format_dt(doc.get("date_to")),
        "count": doc.get("count") or 0,
        "format": doc.get("format"),
        "created_at": _format_dt(doc.get("created_at")),
    }

def _load_batch_lines_with_status(batch: dict) -> List[dict]:
    lines = batch.get("lines") or []
    if not lines:
        return []

    by_source: Dict[str, Dict[ObjectId, dict]] = {}
    needed: Dict[str, set] = {}
    for line in lines:
        parsed = _parse_line_id(line.get("line_id"))
        if not parsed:
            continue
        source, oid, _ = parsed
        needed.setdefault(source, set()).add(oid)

    for source, ids in needed.items():
        col = _get_orders_collection(source)
        orders = list(col.find({"_id": {"$in": list(ids)}}, {"items": 1, "status": 1}))
        by_source[source] = {o["_id"]: o for o in orders}

    enriched = []
    for line in lines:
        line_id = line.get("line_id")
        parsed = _parse_line_id(line_id)
        line_status = "-"
        order_status = "-"
        if parsed:
            source, oid, idx = parsed
            order = by_source.get(source, {}).get(oid)
            if order:
                items = order.get("items") or []
                if 0 <= idx < len(items):
                    line_status = _normalize_line_status(items[idx].get("line_status")) or "-"
                order_status = _normalize_status(order.get("status") or "") or "-"
        enriched.append({
            **line,
            "line_status": line_status,
            "order_status": order_status,
        })
    return enriched

def _split_source_prefix(raw: str) -> Tuple[str, str]:
    raw = (raw or "").strip()
    if ":" in raw:
        left, right = raw.split(":", 1)
        if left in {"main"}:
            return left, right
    return "main", raw

def _parse_order_id_param(raw: str):
    source, oid_str = _split_source_prefix(raw)
    try:
        return source, ObjectId(oid_str.strip())
    except Exception:
        return None

def _to_object_id(val):
    if isinstance(val, ObjectId):
        return val
    if isinstance(val, str):
        try:
            return ObjectId(val)
        except Exception:
            return None
    return None

def _get_orders_collection(source: str):
    return orders_col

def _is_final_line_status(s: str | None) -> bool:
    return _normalize_line_status(s) in FINAL_STATUSES

def _parse_line_id(line_id: str):
    if not line_id:
        return None
    source, rest = _split_source_prefix(line_id)
    if ":" not in rest:
        return None
    left, right = rest.split(":", 1)
    try:
        oid = ObjectId(left.strip())
        idx = int(right.strip())
        if idx < 0:
            return None
        return source, oid, idx
    except Exception:
        return None

def _decorate_order_source(order: dict, source: str) -> None:
    order["source"] = "main"
    order["source_label"] = "Main"
    order["order_id_param"] = str(order.get("_id"))

def _sort_value(v):
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        return float(v)
    except Exception:
        return 0.0

def _sort_key_for_spec(order: dict, sort_spec: List[Tuple[str, int]]):
    key = []
    for field, direction in sort_spec:
        v = order.get(field)
        if field == "created_at" and not isinstance(v, datetime):
            v = None
        val = _sort_value(v)
        key.append(-val if direction < 0 else val)
    return tuple(key)

def _prepare_order(order: dict, source: str, order_lines: List[dict], orders_collection, user_map: dict | None = None, admin_map: dict | None = None, include_admin: bool = False, store_owner_map: dict | None = None):
    _decorate_order_source(order, source)
    uid = _order_agent_id(order, store_owner_map)
    if user_map is not None:
        order["user"] = user_map.get(uid) or {}
    else:
        order["user"] = users_col.find_one({"_id": uid}) if uid else {}

    if include_admin:
        aid = order.get("admin_id")
        if isinstance(aid, str):
            try:
                aid = ObjectId(aid)
            except Exception:
                aid = None
        if admin_map is not None:
            order["admin_user"] = admin_map.get(aid) or {}
        else:
            order["admin_user"] = users_col.find_one({"_id": aid}) if aid else {}

    items = order.get("items") or []
    now = datetime.utcnow()
    changed_indexes = []
    for idx, item in enumerate(items):
        updates = _extract_duplicate_delivered_updates(item)
        if updates:
            item.update(updates)
            changed_indexes.append((idx, updates))

    if changed_indexes:
        for idx, updates in changed_indexes:
            set_doc = {f"items.{idx}.{k}": v for k, v in updates.items()}
            set_doc["updated_at"] = now
            orders_collection.update_one({"_id": order["_id"]}, {"$set": set_doc})

        current_status = (order.get("status") or "").lower()
        if current_status not in FINAL_STATUSES:
            new_status = _compute_order_status_from_items(items, current_status=current_status)
            if new_status and new_status != current_status:
                set_doc = {"status": new_status, "updated_at": now}
                if new_status == "delivered" and not order.get("delivered_at"):
                    set_doc["delivered_at"] = now
                orders_collection.update_one({"_id": order["_id"]}, {"$set": set_doc})
                order["status"] = new_status

    _compute_api_fields(order)
    for idx, item in enumerate(items):
        if _is_boosting_item(item):
            continue
        line_id = f"{order.get('_id')}:{idx}"
        order_lines.append({
            "order_mongo_id": str(order.get("_id")),
            "order_mongo_id_param": order.get("order_id_param"),
            "order_id": order.get("order_id"),
            "user": order.get("user") or {},
            "admin_user": order.get("admin_user") or {},
            "paid_from": order.get("paid_from"),
            "created_at": order.get("created_at"),
            "status": order.get("status"),
            "order_total_amount": order.get("total_amount"),
            "profit_amount_total": item.get("admin_profit") if item.get("admin_profit") is not None else item.get("profit_amount") or 0,
            "item_index": idx,
            "item": item,
            "line_id": line_id,
            "source": source,
        })

def _apply_line_status_change(line_ids: List[str], new_status: str, api_status: str | None = None, reason: str = "manual", actor_admin_id=None, orders_collection=orders_col, target_source: str | None = None) -> Tuple[int, List[str]]:
    updated_lines = 0
    errors = []
    now = datetime.utcnow()
    if new_status not in {"pending", "processing", "delivered", "failed", "refunded"}:
        return 0, [f"invalid line status: {new_status}"]

    grouped = {}
    for lid in line_ids:
        parsed = _parse_line_id(lid)
        if not parsed:
            errors.append(f"{lid}: invalid line id")
            continue
        source, oid, idx = parsed
        if target_source and source != target_source:
            errors.append(f"{lid}: source mismatch")
            continue
        grouped.setdefault(oid, set()).add(idx)

    for oid, idxs in grouped.items():
        try:
            order = orders_collection.find_one({"_id": oid})
            if not order:
                errors.append(f"{oid}: not found")
                continue
            order_status = _normalize_status(order.get("status"))
            allow_delivered_refund = order_status == "delivered" and new_status == "refunded"
            if order_status in FINAL_STATUSES and not allow_delivered_refund:
                errors.append(f"{oid}: order is final and cannot be changed")
                continue

            items = order.get("items") or []
            set_doc = {"updated_at": now}
            any_changed = False

            for idx in sorted(idxs):
                if idx < 0 or idx >= len(items):
                    errors.append(f"{oid}:{idx}: item not found")
                    continue
                item = items[idx]
                current_line = _normalize_line_status(item.get("line_status"))
                allow_line_refund = current_line == "delivered" and new_status == "refunded"
                if _is_final_line_status(current_line) and _normalize_line_status(new_status) != current_line and not allow_line_refund:
                    errors.append(f"{oid}:{idx}: line is final and cannot be changed")
                    continue

                if new_status == "refunded" and current_line != "refunded":
                    refund_amount = _money(item.get("charged_amount"), _money(item.get("amount"), 0.0))
                    paid_from = (order.get("paid_from") or "").strip().lower()
                    refund_user_id = order.get("user_id")
                    if paid_from == "wallet":
                        refund_user_id = order.get("wallet_owner_user_id") or order.get("admin_id") or refund_user_id
                    refund_user_id = _to_object_id(refund_user_id)
                    refund_admin_id = order.get("admin_id") or current_admin_id_from_session(session)
                    if refund_amount > 0 and refund_user_id and not item.get("refunded_at"):
                        try:
                            balances_col.update_one(
                                {"user_id": refund_user_id, "admin_id": refund_admin_id},
                                {
                                    "$inc": {"amount": refund_amount},
                                    "$set": {"updated_at": now},
                                    "$setOnInsert": {"created_at": now, "currency": "GHS", "admin_id": refund_admin_id},
                                },
                                upsert=True,
                            )
                            transactions_col.insert_one({
                                "user_id": refund_user_id,
                                "admin_id": refund_admin_id,
                                "amount": refund_amount,
                                "reference": f"{order.get('order_id')}:{idx}",
                                "status": "success",
                                "type": "refund",
                                "gateway": "Wallet",
                                "currency": "GHS",
                                "created_at": now,
                                "verified_at": now,
                                "meta": {
                                    "note": f"{reason.capitalize()} line refund",
                                    "order_db_id": oid,
                                    "item_index": idx,
                                    "actor_admin_id": actor_admin_id,
                                    "refund_paid_from": paid_from or None,
                                    "wallet_owner_user_id": order.get("wallet_owner_user_id"),
                                },
                            })
                            item["refunded_at"] = now
                            item["refunded_amount"] = refund_amount
                            set_doc[f"items.{idx}.refunded_at"] = now
                            set_doc[f"items.{idx}.refunded_amount"] = refund_amount
                        except Exception as e:
                            errors.append(f"{oid}:{idx}: refund ledger err: {e}")
                            continue
                item["line_status"] = new_status
                set_doc[f"items.{idx}.line_status"] = new_status
                if api_status:
                    item["api_status"] = api_status
                    set_doc[f"items.{idx}.api_status"] = api_status
                set_doc[f"items.{idx}.provider_status_checked_at"] = now
                any_changed = True
                updated_lines += 1

            if not any_changed:
                continue

            current_status = (order.get("status") or "").lower()
            if current_status not in FINAL_STATUSES or allow_delivered_refund:
                new_order_status = _compute_order_status_from_items(
                    items,
                    current_status=current_status,
                    allow_final_recompute=allow_delivered_refund,
                )
                if new_order_status and new_order_status != current_status:
                    set_doc["status"] = new_order_status
                    if new_order_status == "delivered" and not order.get("delivered_at"):
                        set_doc["delivered_at"] = now
                    if new_order_status == "refunded" and not order.get("refunded_at"):
                        set_doc["refunded_at"] = now

            res = orders_collection.update_one({"_id": oid}, {"$set": set_doc})
            if getattr(res, "modified_count", 0):
                _clear_dashboard_cache_safely()
        except Exception as e:
            errors.append(f"{oid}: {e}")

    return updated_lines, errors

def _extract_duplicate_delivered_updates(item: dict) -> dict | None:
    api_resp = item.get("api_response")
    if not isinstance(api_resp, dict):
        return None
    if api_resp.get("http_status") != 409:
        return None
    dup = api_resp.get("duplicate_order") or {}
    dup_status = (dup.get("status") or "").strip().upper()
    if dup_status != "DELIVERED":
        return None

    updates = {}
    if _normalize_line_status(item.get("line_status")) != "delivered":
        updates["line_status"] = "delivered"
    if (item.get("api_status") or "").strip().lower() not in ("success", "duplicate_delivered"):
        updates["api_status"] = "success"
    if not item.get("provider_reference"):
        ref = dup.get("transaction_code") or dup.get("provider_reference") or dup.get("reference") or dup.get("id")
        if ref:
            updates["provider_reference"] = ref
    return updates or None

# ---------- CORE: apply status change (used by manual, bulk, scheduled) ----------
def _apply_status_change(order_ids: List[ObjectId], new_status: str, reason: str = "manual", actor_admin_id=None, orders_collection=orders_col, source: str = "main") -> Tuple[int, List[str]]:
    """
    Idempotent per-order updates, including wallet credit for refunds.
    Returns (updated_count, errors)
    """
    updated = 0
    errors  = []

    now = datetime.utcnow()
    for oid in order_ids:
        try:
            order = orders_collection.find_one({"_id": oid})
            if not order:
                errors.append(f"{oid}: not found")
                continue

            old_status = _normalize_status(order.get("status"))
            allow_delivered_refund = old_status == "delivered" and new_status == "refunded"
            if old_status in FINAL_STATUSES and new_status != old_status and not allow_delivered_refund:
                _log_status_blocked(order, new_status, "final_status", reason, source, actor_admin_id)
                errors.append(f"{oid}: order is final and cannot be changed")
                continue
            if not _can_transition(old_status, new_status):
                _log_status_blocked(order, new_status, "invalid_transition", reason, source, actor_admin_id)
                errors.append(f"{oid}: invalid transition {old_status} -> {new_status}")
                continue
            update_doc = {"status": new_status, "updated_at": now}
            # Delivered → set delivered_at if missing
            if new_status == "delivered" and not order.get("delivered_at"):
                update_doc["delivered_at"] = now

            # Refunded → single wallet credit based on charged_amount
            if new_status == "refunded":
                charged_amount = _money(order.get("charged_amount"), 0.0)
                line_refunded_amount = sum(
                    _money(item.get("refunded_amount"), 0.0)
                    for item in (order.get("items") or [])
                )
                charged_amount = max(0.0, charged_amount - line_refunded_amount)
                paid_from = (order.get("paid_from") or "").strip().lower()
                refund_user_id = order.get("user_id")
                if paid_from == "wallet":
                    refund_user_id = (
                        order.get("wallet_owner_user_id")
                        or order.get("admin_id")
                        or refund_user_id
                    )
                refund_user_id = _to_object_id(refund_user_id)
                refund_admin_id = order.get("admin_id") or current_admin_id_from_session(session)
                already_refunded = bool(order.get("refunded_at")) or (old_status == "refunded")

                if charged_amount > 0 and refund_user_id and not already_refunded:
                    try:
                        balances_col.update_one(
                            {"user_id": refund_user_id, "admin_id": refund_admin_id},
                            {
                                "$inc": {"amount": charged_amount},
                                "$set": {"updated_at": now},
                                "$setOnInsert": {"created_at": now, "currency": "GHS", "admin_id": refund_admin_id},
                            },
                            upsert=True
                        )
                        transactions_col.insert_one({
                            "user_id": refund_user_id,
                            "admin_id": refund_admin_id,
                            "amount": charged_amount,
                            "reference": order.get("order_id"),
                            "status": "success",
                            "type": "refund",
                            "gateway": "Wallet",
                            "currency": "GHS",
                            "created_at": now,
                            "verified_at": now,
                            "meta": {
                                "note": f"{reason.capitalize()} refund",
                                "order_db_id": oid,
                                "actor_admin_id": actor_admin_id,
                                "refund_paid_from": paid_from or None,
                                "wallet_owner_user_id": order.get("wallet_owner_user_id"),
                            }
                        })
                    except Exception as e:
                        errors.append(f"{oid}: refund ledger err: {e}")
                update_doc["refunded_at"] = now

            update_filter = {"_id": oid}
            if new_status not in FINAL_STATUSES:
                update_filter["status"] = {"$nin": list(FINAL_STATUSES)}
            res = orders_collection.update_one(update_filter, {"$set": update_doc})
            if res.modified_count:
                _clear_dashboard_cache_safely()
                # Flip line_status in items from processing -> delivered when marking delivered
                if new_status == "delivered":
                    try:
                        orders_collection.update_one(
                            {"_id": oid, "status": "delivered"},
                            {"$set": {"items.$[it].line_status": "delivered"}},
                            array_filters=[{"it.line_status": "processing"}]
                        )
                    except Exception:
                        pass
                updated += 1
            else:
                if new_status not in FINAL_STATUSES:
                    _log_status_blocked(order, new_status, "db_guard", reason, source, actor_admin_id)

        except Exception as e:
            errors.append(f"{oid}: {e}")

    return updated, errors

# ---------- DB-backed scheduler utilities ----------
def _enqueue_status_job(order_id_strs: List[str], new_status: str, run_time: datetime, admin_id: str | None, note: str | None, line_ids: List[str] | None = None):
    """
    Persist a job document that can be executed later (Render-safe).
    """
    now = datetime.utcnow()
    doc = {
        "job_key": str(uuid.uuid4()),
        "order_ids": order_id_strs,     # strings
        "line_ids": line_ids or [],     # strings "orderId:itemIndex"
        "status": new_status,
        "note": note or "",
        "admin_id": admin_id,
        "state": "scheduled",           # scheduled | running | done | error | cancelled
        "attempts": 0,
        "max_attempts": 3,
        "created_at": now,
        "run_at": run_time,             # UTC datetime
        "started_at": None,
        "finished_at": None,
        "result": None,                 # {updated, errors:[], ...}
        "lock_token": None,             # for cooperative locking
        "locked_at": None
    }
    schedules_col.insert_one(doc)
    return doc

def _process_due_jobs(max_batch: int = 25):
    """
    Cooperatively process due jobs. Safe to call at the top of admin routes
    and/or from a Render Cron ping.
    """
    now = datetime.utcnow()
    # pick up to max_batch jobs that are due and not locked/running/cancelled
    cursor = schedules_col.find({
        "state": {"$in": ["scheduled", "error"]},
        "run_at": {"$lte": now},
        "$or": [{"lock_token": None}, {"locked_at": {"$lt": now - timedelta(minutes=5)}}]
    }).sort([("run_at", 1)]).limit(max_batch)

    for job in cursor:
        lock_token = str(uuid.uuid4())
        # try to acquire lock
        claimed = schedules_col.update_one(
            {"_id": job["_id"], "lock_token": job.get("lock_token")},
            {"$set": {"lock_token": lock_token, "locked_at": now, "state": "running", "started_at": now}}
        )
        if not claimed.modified_count:
            continue

        # Execute
        try:
            updated = 0
            errors = []

            line_ids = [s for s in (job.get("line_ids") or []) if s]
            if line_ids:
                by_source = {"main": []}
                for lid in line_ids:
                    parsed = _parse_line_id(lid)
                    if not parsed:
                        errors.append(f"{lid}: invalid line id")
                        continue
                    source, _, _ = parsed
                    by_source[source].append(lid)
                for source, ids in by_source.items():
                    if not ids:
                        continue
                    line_updated, line_errors = _apply_line_status_change(
                        ids,
                        job.get("status"),
                        reason="scheduled",
                        actor_admin_id=job.get("admin_id"),
                        orders_collection=_get_orders_collection(source),
                        target_source=source,
                    )
                    updated += line_updated
                    errors += line_errors

            by_source = {"main": []}
            for s in (job.get("order_ids") or []):
                parsed = _parse_order_id_param(s)
                if not parsed:
                    continue
                source, oid = parsed
                by_source[source].append(oid)
            for source, ids in by_source.items():
                if not ids:
                    continue
                order_updated, order_errors = _apply_status_change(
                    ids,
                    job.get("status"),
                    reason="scheduled",
                    actor_admin_id=job.get("admin_id"),
                    orders_collection=_get_orders_collection(source),
                    source=source,
                )
                updated += order_updated
                errors += order_errors
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "done" if not errors else "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": updated, "error_count": len(errors), "errors": errors}
                }}
            )
        except Exception as e:
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": 0, "error_count": 1, "errors": [str(e)]}
                }}
            )


def process_order_automation_tick(*, max_schedule_batch: int = 50, max_auto_rules: int = 20, max_orders_per_rule: int = 500) -> Dict[str, Any]:
    schedule_error = None
    try:
        _process_due_jobs(max_batch=max_schedule_batch)
    except Exception as exc:
        schedule_error = str(exc)

    auto_result = _process_auto_update_rules(
        force=True,
        max_rules=max_auto_rules,
        max_orders_per_rule=max_orders_per_rule,
    )
    return {
        "ok": schedule_error is None,
        "schedule_error": schedule_error,
        "auto_update": auto_result,
    }

# =========================================================
#                       ROUTES
# =========================================================
@admin_orders_bp.route("/admin/orders/data")
def admin_orders_data():
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if _require_main_admin():
        try:
            _process_auto_update_rules()
        except Exception:
            pass

    cache_key = _build_orders_cache_key(request.args)
    cached = get_cached_json(cache_key)
    if cached and isinstance(cached, dict):
        payload = cached.get("payload")
        etag = cached.get("etag")
        inm = request.headers.get("If-None-Match")
        if inm and etag and inm == etag:
            resp = make_response("", 304)
            resp.headers["Cache-Control"] = "private, max-age=30"
            resp.headers["ETag"] = etag
            return resp
        if payload is not None:
            return _json_with_cache(payload, etag)

    payload = _build_orders_data_payload(request.args)
    etag = _etag_for_payload(payload)
    set_cached_json(cache_key, {"payload": payload, "etag": etag}, _ORDERS_CACHE_TTL_SECONDS)
    return _json_with_cache(payload, etag)

@admin_orders_bp.route("/admin/orders")
def admin_view_orders():
    if not _require_admin():
        return redirect(url_for("login.login"))
    if _require_main_admin():
        try:
            _process_auto_update_rules()
        except Exception:
            pass

    view_mode = (request.args.get("view") or "lines").strip().lower()
    if view_mode not in {"lines", "orders"}:
        view_mode = "lines"

    sort = (request.args.get("sort") or "newest").strip().lower()
    if sort not in ALLOWED_SORTS:
        sort = "newest"

    try:
        per_page = int(request.args.get("per_page", DEFAULT_PER_PAGE))
        per_page = max(1, min(per_page, 100))
    except Exception:
        per_page = DEFAULT_PER_PAGE

    try:
        page = int(request.args.get("page", 1))
        page = max(1, page)
    except Exception:
        page = 1

    view_line_url = url_for("admin_orders.admin_view_orders")
    view_order_url = url_for("admin_orders.admin_view_orders")
    try:
        view_line_query = _build_preserved_query({**request.args.to_dict(flat=True), "view": "lines"})
        view_order_query = _build_preserved_query({**request.args.to_dict(flat=True), "view": "orders"})
        if view_line_query:
            view_line_url = f"{view_line_url}?{view_line_query}"
        if view_order_query:
            view_order_url = f"{view_order_url}?{view_order_query}"
    except Exception:
        pass

    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    afa_pending = 0
    main_admin_complaint_pending = 0
    if is_main_admin:
        try:
            afa_pending = int(afa_col.count_documents({"status": "pending"}))
        except Exception:
            afa_pending = 0
        try:
            main_admin_complaint_pending = int(complaints_col.count_documents({
                "sent_to_main_admin": True,
                "status": "pending",
            }))
        except Exception:
            main_admin_complaint_pending = 0

    return render_template(
        "admin_orders.html",
        orders=[],
        order_lines=[],
        order_lines_count=0,
        page=page, total_pages=1, total_orders=0,
        status_filter=(request.args.get("status") or "").strip().lower(),
        order_id_q=(request.args.get("order_id") or "").strip(),
        customer_q=(request.args.get("customer") or "").strip(),
        paid_from=(request.args.get("paid_from") or "").strip().lower(),
        min_total=(request.args.get("min_total") or "").strip(),
        max_total=(request.args.get("max_total") or "").strip(),
        date_from=(request.args.get("date_from") or "").strip(),
        date_to=(request.args.get("date_to") or "").strip(),
        time_from=(request.args.get("time_from") or "").strip(),
        time_to=(request.args.get("time_to") or "").strip(),
        sort=sort, per_page=per_page,
        item_service=(request.args.get("item_service") or "").strip(),
        item_offer=(request.args.get("item_offer") or "").strip(),
        item_phone=(request.args.get("item_phone") or "").strip(),
        agent_q=(request.args.get("agent") or "").strip(),
        api_filter=(request.args.get("api_filter") or "").strip().lower(),
        source="main",
        filters_query=_build_preserved_query(request.args),
        view_mode=view_mode,
        is_main_admin=is_main_admin,
        afa_pending=afa_pending,
        main_admin_complaint_pending=main_admin_complaint_pending,
        view_line_url=view_line_url,
        view_order_url=view_order_url,
    )

@admin_orders_bp.route("/admin/orders/complaint-orders")
def main_admin_complaint_orders():
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if (session.get("role") or "").strip().lower() != "main_admin":
        return jsonify({"ok": False, "error": "main admin only"}), 403

    mode = (request.args.get("mode") or "active").strip().lower()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        per_page = max(1, min(int(request.args.get("per_page", 8)), 50))
    except Exception:
        per_page = 8

    status_filter = (request.args.get("status") or "").strip().lower()
    open_statuses = ["pending", PAYMENT_CONFIRMED_STATUS, "refund"]
    history_statuses = ["resolved", "false", "rejected"]
    all_statuses = set(open_statuses + history_statuses)

    query = {"sent_to_main_admin": True}
    if mode == "history":
        if status_filter in all_statuses:
            query["status"] = status_filter
        else:
            query["status"] = {"$in": history_statuses}
    else:
        if status_filter in all_statuses:
            query["status"] = status_filter
        elif status_filter == "all_open":
            query["status"] = {"$in": open_statuses}
        else:
            query["status"] = "pending"

    total = complaints_col.count_documents(query)
    docs = list(
        complaints_col.find(query)
        .sort("submitted_at", -1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )
    admin_ids = list({d.get("admin_id") for d in docs if d.get("admin_id")})
    admins = {u["_id"]: u for u in users_col.find({"_id": {"$in": admin_ids}}, {"first_name": 1, "last_name": 1, "business_name": 1, "username": 1, "email": 1, "phone": 1})} if admin_ids else {}
    items = [_serialize_main_admin_complaint(d, admins.get(d.get("admin_id"))) for d in docs]
    total_pages = max(1, (total + per_page - 1) // per_page)
    return jsonify({
        "ok": True,
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status_filter or ("all_history" if mode == "history" else "pending"),
    })

@admin_orders_bp.route("/admin/orders/complaint-orders/<complaint_id>/action", methods=["POST"])
def main_admin_complaint_order_action(complaint_id):
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if (session.get("role") or "").strip().lower() != "main_admin":
        return jsonify({"ok": False, "error": "main admin only"}), 403

    try:
        cid = ObjectId(complaint_id)
    except Exception:
        return jsonify({"ok": False, "error": "invalid complaint id"}), 400

    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    reply = (payload.get("reply") or "").strip()
    if action not in {"reply", "resolve", "payment_confirmed", "false_complaint"}:
        return jsonify({"ok": False, "error": "invalid action"}), 400
    if action == "payment_confirmed" and not reply:
        reply = PAYMENT_CONFIRMED_REPLY
    if action == "false_complaint" and not reply:
        reply = FALSE_COMPLAINT_REPLY
    if action == "reply" and not reply:
        return jsonify({"ok": False, "error": "reply message is required"}), 400

    query = {"_id": cid, "sent_to_main_admin": True}
    complaint = complaints_col.find_one(query, {"_id": 1})
    if not complaint:
        return jsonify({"ok": False, "error": "complaint not found"}), 404

    now = datetime.utcnow()
    update = {
        "main_admin_last_action_at": now,
        "main_admin_last_action_by": {
            "user_id": session.get("user_id"),
            "username": session.get("username") or session.get("email") or "main_admin",
        },
        "updated_at": now,
    }
    if reply:
        update.update({
            "main_admin_reply": reply,
            "main_admin_replied_at": now,
            "main_admin_reply_seen": False,
        })
    if action == "resolve":
        update.update({
            "status": "resolved",
            "main_admin_resolved": True,
            "main_admin_resolved_at": now,
        })
    elif action == "payment_confirmed":
        update.update({
            "status": PAYMENT_CONFIRMED_STATUS,
            "main_admin_decision": PAYMENT_CONFIRMED_STATUS,
            "payment_confirmed": True,
            "payment_confirmed_at": now,
        })
    elif action == "false_complaint":
        update.update({
            "status": "false",
            "main_admin_decision": "false_complaint",
            "main_admin_resolved": True,
            "main_admin_resolved_at": now,
        })

    complaints_col.update_one(query, {"$set": update})
    updated_doc = complaints_col.find_one(query) or {}
    messages = {
        "resolve": "Complaint resolved.",
        "payment_confirmed": "Payment confirmed. Admin can process the store order.",
        "false_complaint": "Complaint marked as false.",
    }
    return jsonify({
        "ok": True,
        "message": messages.get(action, "Reply sent."),
        "status": updated_doc.get("status") or update.get("status") or "",
        "payment_confirmed": bool(updated_doc.get("payment_confirmed")),
        "can_process_store_order": bool(updated_doc.get("store_slug") and updated_doc.get("cart_snapshot") and updated_doc.get("payment_confirmed")),
    })

@admin_orders_bp.route("/admin/orders/export-catalog")
def export_catalog():
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    catalog = _collect_export_catalog(admin_oid, is_main_admin)
    return jsonify({"ok": True, **catalog})


@admin_orders_bp.route("/admin/orders/auto-update-rules", methods=["GET"])
def list_auto_update_rules():
    if not _require_main_admin():
        return jsonify({"ok": False, "error": "main admin only"}), 403
    rules = [
        _serialize_auto_update_rule(doc)
        for doc in auto_update_rules_col.find({}).sort([("state", 1), ("created_at", -1)]).limit(100)
    ]
    return jsonify({"ok": True, "rules": rules})


@admin_orders_bp.route("/admin/orders/auto-update-rules", methods=["POST"])
def create_auto_update_rule():
    if not _require_main_admin():
        return jsonify({"ok": False, "error": "main admin only"}), 403

    service_names = _clean_service_names(
        request.form.getlist("service_name") + request.form.getlist("service_name[]")
    )
    if not service_names:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("service_name") or payload.get("service_names") or []
        if isinstance(raw, str):
            service_names = _clean_service_names(raw.split(","))
        elif isinstance(raw, list):
            service_names = _clean_service_names([str(v) for v in raw])

    try:
        threshold_minutes = int((request.form.get("threshold_minutes") or (request.get_json(silent=True) or {}).get("threshold_minutes") or 0))
    except Exception:
        threshold_minutes = 0

    statuses = _parse_auto_rule_statuses(
        request.form.getlist("eligible_statuses") + request.form.getlist("eligible_statuses[]")
    )
    if not statuses:
        payload = request.get_json(silent=True) or {}
        raw_statuses = payload.get("eligible_statuses") or []
        if isinstance(raw_statuses, str):
            statuses = _parse_auto_rule_statuses(raw_statuses.split(","))
        elif isinstance(raw_statuses, list):
            statuses = _parse_auto_rule_statuses([str(v) for v in raw_statuses])

    note = (request.form.get("note") or ((request.get_json(silent=True) or {}).get("note")) or "").strip()
    name = (request.form.get("name") or ((request.get_json(silent=True) or {}).get("name")) or "").strip()

    if not service_names:
        return jsonify({"ok": False, "error": "Select at least one service."}), 400
    if threshold_minutes < 1 or threshold_minutes > 10080:
        return jsonify({"ok": False, "error": "Threshold must be between 1 and 10080 minutes."}), 400

    now = datetime.utcnow()
    if not name:
        preview_names = ", ".join(service_names[:2])
        more = "" if len(service_names) <= 2 else f" +{len(service_names) - 2}"
        name = f"Auto Deliver after {threshold_minutes} min: {preview_names}{more}"

    doc = {
        "name": name,
        "service_names": service_names,
        "service_names_lc": [s.lower() for s in service_names],
        "threshold_minutes": threshold_minutes,
        "eligible_statuses": statuses,
        "target_status": "delivered",
        "state": "active",
        "note": note,
        "created_by": _to_objectid(session.get("user_id")),
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_match_count": 0,
        "last_update_count": 0,
        "run_count": 0,
        "last_error": "",
    }
    inserted = auto_update_rules_col.insert_one(doc)
    doc["_id"] = inserted.inserted_id
    return jsonify({"ok": True, "rule": _serialize_auto_update_rule(doc)})


@admin_orders_bp.route("/admin/orders/auto-update-rules/<rule_id>/toggle", methods=["POST"])
def toggle_auto_update_rule(rule_id):
    if not _require_main_admin():
        return jsonify({"ok": False, "error": "main admin only"}), 403
    oid = _to_objectid(rule_id)
    if not oid:
        return jsonify({"ok": False, "error": "Invalid rule id."}), 400
    rule = auto_update_rules_col.find_one({"_id": oid})
    if not rule:
        return jsonify({"ok": False, "error": "Rule not found."}), 404
    new_state = "paused" if (rule.get("state") or "active") == "active" else "active"
    auto_update_rules_col.update_one({"_id": oid}, {"$set": {"state": new_state, "updated_at": datetime.utcnow()}})
    updated = auto_update_rules_col.find_one({"_id": oid})
    return jsonify({"ok": True, "rule": _serialize_auto_update_rule(updated or rule)})


@admin_orders_bp.route("/admin/orders/auto-update-rules/<rule_id>/run", methods=["POST"])
def run_auto_update_rule(rule_id):
    if not _require_main_admin():
        return jsonify({"ok": False, "error": "main admin only"}), 403
    oid = _to_objectid(rule_id)
    if not oid:
        return jsonify({"ok": False, "error": "Invalid rule id."}), 400
    auto_update_rules_col.update_one({"_id": oid}, {"$set": {"state": "active", "updated_at": datetime.utcnow()}})
    result = _process_auto_update_rules(force=True, max_rules=50, max_orders_per_rule=1000, specific_rule_ids=[oid])
    rule = auto_update_rules_col.find_one({"_id": oid})
    return jsonify({"ok": True, "result": result, "rule": _serialize_auto_update_rule(rule or {"_id": oid})})


@admin_orders_bp.route("/admin/orders/auto-update-rules/<rule_id>/delete", methods=["POST"])
def delete_auto_update_rule(rule_id):
    if not _require_main_admin():
        return jsonify({"ok": False, "error": "main admin only"}), 403
    oid = _to_objectid(rule_id)
    if not oid:
        return jsonify({"ok": False, "error": "Invalid rule id."}), 400
    res = auto_update_rules_col.delete_one({"_id": oid})
    if not res.deleted_count:
        return jsonify({"ok": False, "error": "Rule not found."}), 404
    return jsonify({"ok": True})

@admin_orders_bp.route("/admin/orders/export-undelivered", methods=["POST"])
def export_undelivered():
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = {}
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}
    form_payload = request.form.to_dict(flat=True)
    merged = {**payload, **form_payload}

    service_names = []
    service_names += request.form.getlist("service_name")
    service_names += request.form.getlist("service_name[]")
    if not service_names:
        raw = payload.get("service_name")
        if isinstance(raw, list):
            service_names = [str(s).strip() for s in raw if str(s).strip()]
        elif isinstance(raw, str):
            service_names = [s.strip() for s in raw.split(",") if s.strip()]
    service_names = [s for s in service_names if s]

    network = (merged.get("network") or "ANY").strip().upper()
    source = (merged.get("source") or "all").strip().lower()
    fmt = (merged.get("format") or "txt").strip().lower()
    if fmt not in {"txt", "excel", "pdf"}:
        return jsonify({"ok": False, "error": "Invalid format."}), 400

    try:
        start_dt, end_dt, timeframe = _parse_export_timeframe(merged)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    catalog = _collect_export_catalog(admin_oid, is_main_admin)
    service_network_map = catalog.get("service_network_map") or {}

    rows = _collect_undelivered_export_rows(
        admin_oid,
        is_main_admin,
        service_names,
        network,
        source,
        start_dt,
        end_dt,
        service_network_map,
    )

    if not rows:
        return jsonify({"ok": False, "error": "No matching undelivered lines found for the selected filters."}), 404

    networks = sorted({r.get("network") for r in rows if r.get("network")})
    export_network = network
    if export_network in {"ANY", "ALL"}:
        export_network = networks[0] if len(networks) == 1 else "ANY"

    created_by = _to_objectid(session.get("user_id"))
    batch_doc = _save_export_batch(
        service_names=service_names,
        network=export_network,
        source=source or "all",
        timeframe=timeframe,
        start_dt=start_dt,
        end_dt=end_dt,
        count=len(rows),
        fmt=fmt,
        rows=rows,
        admin_id=admin_oid,
        created_by=created_by,
    )

    filename_base = f"undelivered_badge_{batch_doc.get('badge_no')}"
    if fmt == "txt":
        lines = [export_network, ""]
        for r in rows:
            phone = r.get("phone") or ""
            offer = _export_offer_text(r.get("offer") or "")
            row = f"{phone} {offer}".strip()
            lines.append(row)
        content = "\n".join(lines)
        resp = make_response(content)
        resp.headers["Content-Type"] = "text/plain; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename=\"{filename_base}.txt\""
        return resp

    if fmt == "excel":
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Line"])
        for r in rows:
            phone = r.get("phone") or ""
            offer = _export_offer_text(r.get("offer") or "")
            writer.writerow([f"{phone} {offer}".strip()])
        data = output.getvalue()
        resp = make_response(data)
        resp.headers["Content-Type"] = "application/vnd.ms-excel; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename=\"{filename_base}.xls\""
        return resp

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 50
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, f"Exported Undelivered Lines - {export_network}")
    y -= 25
    c.setFont("Helvetica", 10)
    for r in rows:
        phone = r.get("phone") or ""
        offer = _export_offer_text(r.get("offer") or "")
        row = f"{phone} {offer}".strip()
        if y < 40:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)
        c.drawString(40, y, row)
        y -= 16
    c.save()
    pdf_data = buf.getvalue()
    resp = make_response(pdf_data)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"attachment; filename=\"{filename_base}.pdf\""
    return resp

@admin_orders_bp.route("/admin/orders/export-batches")
def export_batches():
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    query = {} if is_main_admin else {"admin_id": admin_oid}
    docs = list(order_export_batches_col.find(query).sort("created_at", -1).limit(15))
    return jsonify({"ok": True, "batches": [_serialize_export_batch_summary(d) for d in docs]})

@admin_orders_bp.route("/admin/orders/export-batches/<batch_id>")
def export_batch_detail(batch_id):
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    try:
        doc = order_export_batches_col.find_one({"_id": ObjectId(batch_id)})
    except Exception:
        doc = None
    if not doc:
        return jsonify({"ok": False, "error": "Batch not found."}), 404
    if not is_main_admin and doc.get("admin_id") != admin_oid:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    lines = _load_batch_lines_with_status(doc)
    return jsonify({
        "ok": True,
        "batch": _serialize_export_batch_summary(doc),
        "lines": lines,
    })

def _mark_batch_lines_delivered(line_ids: List[str]) -> Tuple[int, List[str]]:
    updated_total = 0
    errors: List[str] = []
    by_source: Dict[str, List[str]] = {}
    for lid in line_ids:
        parsed = _parse_line_id(lid)
        if not parsed:
            errors.append(f"{lid}: invalid line id")
            continue
        source, _, _ = parsed
        by_source.setdefault(source, []).append(lid)

    for source, ids in by_source.items():
        if not ids:
            continue
        updated, errs = _apply_line_status_change(
            ids,
            "delivered",
            reason="export_batch",
            actor_admin_id=session.get("user_id"),
            orders_collection=_get_orders_collection(source),
            target_source=source,
        )
        updated_total += updated
        errors += errs
    return updated_total, errors

@admin_orders_bp.route("/admin/orders/export-batches/<batch_id>/mark-delivered", methods=["POST"])
def export_batch_mark_delivered(batch_id):
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    try:
        doc = order_export_batches_col.find_one({"_id": ObjectId(batch_id)})
    except Exception:
        doc = None
    if not doc:
        return jsonify({"ok": False, "error": "Batch not found."}), 404
    if not is_main_admin and doc.get("admin_id") != admin_oid:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    line_ids = [l.get("line_id") for l in (doc.get("lines") or []) if l.get("line_id")]
    updated, errors = _mark_batch_lines_delivered(line_ids)
    return jsonify({"ok": True, "updated": updated, "errors": errors})

@admin_orders_bp.route("/admin/orders/export-batches/<batch_id>/mark-delivered-selected", methods=["POST"])
def export_batch_mark_delivered_selected(batch_id):
    if not _require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    try:
        doc = order_export_batches_col.find_one({"_id": ObjectId(batch_id)})
    except Exception:
        doc = None
    if not doc:
        return jsonify({"ok": False, "error": "Batch not found."}), 404
    if not is_main_admin and doc.get("admin_id") != admin_oid:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    line_ids = []
    raw = payload.get("line_ids") or payload.get("selected_line_ids")
    if isinstance(raw, list):
        line_ids = [str(s).strip() for s in raw if str(s).strip()]
    elif isinstance(raw, str):
        line_ids = [s.strip() for s in raw.split(",") if s.strip()]

    if not line_ids:
        return jsonify({"ok": False, "error": "No line ids provided."}), 400

    batch_line_ids = {l.get("line_id") for l in (doc.get("lines") or []) if l.get("line_id")}
    line_ids = [lid for lid in line_ids if lid in batch_line_ids]
    if not line_ids:
        return jsonify({"ok": False, "error": "No valid batch lines selected."}), 400

    updated, errors = _mark_batch_lines_delivered(line_ids)
    return jsonify({"ok": True, "updated": updated, "errors": errors})

@admin_orders_bp.route("/admin/orders/<order_id>/items/<int:item_index>/status", methods=["POST"])
def update_order_line_status(order_id, item_index):
    if not _require_admin():
        return redirect(url_for("login.login"))

    payload = {}
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    new_status = (request.form.get("line_status") or payload.get("line_status") or "").strip().lower()
    api_status = (request.form.get("api_status") or payload.get("api_status") or "").strip().lower()

    if new_status not in {"delivered", "processing", "failed", "pending", "refunded"}:
        flash("Invalid line status.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    parsed = _parse_order_id_param(order_id)
    if not parsed:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))
    source, oid = parsed
    orders_collection = _get_orders_collection(source)

    order = orders_collection.find_one({"_id": oid})
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))
    order_status = _normalize_status(order.get("status"))
    if order_status in FINAL_STATUSES and not (order_status == "delivered" and new_status == "refunded"):
        flash("This order is final and cannot be changed.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    items = order.get("items") or []
    if item_index < 0 or item_index >= len(items):
        flash("Line item not found.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    item = items[item_index]
    current_line = _normalize_line_status(item.get("line_status"))
    if _is_final_line_status(current_line) and _normalize_line_status(new_status) != current_line and not (current_line == "delivered" and new_status == "refunded"):
        flash("This line is final and cannot be changed.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    line_id = f"{source}:{oid}:{item_index}" if source != "main" else f"{oid}:{item_index}"
    updated, errors = _apply_line_status_change(
        [line_id], new_status, api_status=api_status or None, reason="manual",
        actor_admin_id=session.get("user_id"), orders_collection=orders_collection,
        target_source=source,
    )
    if not updated:
        flash(errors[0] if errors else "Line status was not updated.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    flash("✅ Line status updated.", "success")
    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/<order_id>/update", methods=["POST"])
def update_order_status(order_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in ALLOWED_STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    parsed = _parse_order_id_param(order_id)
    if not parsed:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))
    source, oid = parsed
    orders_collection = _get_orders_collection(source)

    updated, errors = _apply_status_change([oid], new_status, reason="manual", actor_admin_id=session.get("user_id"), orders_collection=orders_collection, source=source)
    if updated:
        msg = {
            "processing": "✅ Order marked as Processing.",
            "delivered": "✅ Order marked as Delivered.",
            "failed": "✅ Order marked as Failed.",
            "refunded": "✅ Order marked as Refunded (wallet credited if not already).",
            "cancelled": "✅ Order marked as Cancelled.",
            "canceled": "✅ Order marked as Canceled.",
            "pending": "✅ Order marked as Pending.",
            "completed": "✅ Order marked as Completed.",
        }.get(new_status, "✅ Order updated.")
        flash(msg, "success")
    else:
        if errors:
            flash(" | ".join(errors[:3]), "warning")
        else:
            flash("ℹ️ No change to order.", "warning")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/<order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    parsed = _parse_order_id_param(order_id)
    if not parsed:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    source, oid = parsed
    orders_collection = _get_orders_collection(source)

    admin_oid = current_admin_id_from_session(session)
    is_main_admin = (session.get("role") or "").strip().lower() == "main_admin"
    q = {"_id": oid}
    if not is_main_admin and admin_oid:
        q["admin_id"] = admin_oid

    order = orders_collection.find_one(q, {"created_at": 1, "status": 1})
    if not order:
        flash("Order not found or not permitted.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    if not _cancel_allowed(order.get("created_at"), order.get("status")):
        if _cancel_seconds_left(order.get("created_at")) <= 0:
            flash("Cancel window expired. Orders can only be cancelled within 1 minute.", "warning")
        else:
            flash("Order cannot be cancelled in its current status.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    updated, errors = _apply_status_change(
        [oid],
        "refunded",
        reason="admin_cancel",
        actor_admin_id=session.get("user_id"),
        orders_collection=orders_collection,
        source=source,
    )
    if updated:
        flash("✅ Order cancelled and refunded.", "success")
    else:
        if errors:
            flash(" | ".join(errors[:3]), "warning")
        else:
            flash("ℹ️ No change to order.", "warning")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/bulk-deliver", methods=["POST"])
def bulk_deliver_orders():
    """
    Existing behavior: mark all orders that match CURRENT FILTERS and are processing -> delivered.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))
    args = request.args.to_dict(flat=True)
    args["status"] = "processing"
    query = _build_query_from_params(args)
    try:
        updated_total = 0
        errors = []
        ids_main = [o["_id"] for o in orders_col.find(query, {"_id": 1})]
        updated, errs = _apply_status_change(
            ids_main,
            "delivered",
            reason="bulk_deliver",
            actor_admin_id=session.get("user_id"),
            orders_collection=orders_col,
            source="main",
        )
        updated_total += updated
        errors += errs

        if updated_total:
            flash(f"Marked {updated_total} processing order(s) as Delivered.", "success")
        else:
            flash("No eligible processing orders to deliver.", "warning")
        if errors:
            flash(" | ".join(errors[:3]), "warning")
    except Exception:
        flash("Bulk update failed.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# NEW: mark SELECTED ids as delivered (from checkboxes / floating bar)
@admin_orders_bp.route("/admin/orders/bulk-deliver-selected", methods=["POST"])
def bulk_deliver_selected():
    if not _require_admin():
        return redirect(url_for("login.login"))

    line_ids = []
    if "line_ids" in request.form:
        line_ids += [request.form.get("line_ids") or ""]
    line_ids += request.form.getlist("line_ids[]")
    line_ids = ",".join([s for s in line_ids if s]).split(",")
    line_ids = [s.strip() for s in line_ids if s.strip()]

    if line_ids:
        try:
            updated_total = 0
            errors = []
            by_source = {"main": []}
            for lid in line_ids:
                parsed = _parse_line_id(lid)
                if not parsed:
                    errors.append(f"{lid}: invalid line id")
                    continue
                source, _, _ = parsed
                by_source[source].append(lid)

            for source, ids in by_source.items():
                if not ids:
                    continue
                updated, errs = _apply_line_status_change(
                    ids,
                    "delivered",
                    reason="bulk_deliver_selected",
                    actor_admin_id=session.get("user_id"),
                    orders_collection=_get_orders_collection(source),
                    target_source=source,
                )
                updated_total += updated
                errors += errs

            if updated_total:
                flash(f"Marked {updated_total} selected line(s) as Delivered.", "success")
            else:
                flash("No eligible lines to deliver.", "warning")
            if errors:
                flash(" | ".join(errors[:3]), "warning")
        except Exception:
            flash("Failed to bulk deliver selected lines.", "danger")

        back_to = url_for("admin_orders.admin_view_orders")
        qs = _build_preserved_query(request.args)
        return redirect(f"{back_to}?{qs}" if qs else back_to)

    # Accept: order_ids (comma string) OR order_ids[] OR order_id[]
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    by_source = {"main": []}
    for s in raw_list:
        parsed = _parse_order_id_param((s or "").strip())
        if not parsed:
            continue
        source, oid = parsed
        by_source[source].append(oid)

    if not by_source["main"]:
        flash("Please select at least one order.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        updated_total = 0
        errors = []
        for source, ids in by_source.items():
            if not ids:
                continue
            updated, errs = _apply_status_change(
                ids,
                "delivered",
                reason="bulk_deliver_selected",
                actor_admin_id=session.get("user_id"),
                orders_collection=_get_orders_collection(source),
                source=source,
            )
            updated_total += updated
            errors += errs

        if updated_total:
            flash(f"Marked {updated_total} selected order(s) as Delivered.", "success")
        else:
            flash("No eligible orders to deliver.", "warning")
        if errors:
            flash(" | ".join(errors[:3]), "warning")
    except Exception:
        flash("Failed to bulk deliver selected.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# =========================================================

# =========================================================
#            DB-BACKED SCHEDULING ENDPOINTS (Admin)
# =========================================================
@admin_orders_bp.route("/admin/orders/schedule-status", methods=["POST"])
def schedule_status():
    """
    Form fields:
      - order_ids: comma-separated string OR multiple order_ids[] fields OR order_id[]
      - status: one of ALLOWED_STATUSES
      - delay_minutes: int (optional)
      - run_at: "YYYY-MM-DD HH:MM" (UTC, optional)
      - note: optional
    One of delay_minutes or run_at is required.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))

    status = (request.form.get("status") or "").strip().lower()
    if status not in ALLOWED_STATUSES:
        flash("Invalid status for scheduling.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # collect order ids
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    order_id_strs = []
    bad_ids = []
    for s in raw_list:
        s2 = (s or "").strip()
        if not s2:
            continue
        parsed = _parse_order_id_param(s2)
        if not parsed:
            bad_ids.append(s2)
            continue
        source, oid = parsed
        if source == "main":
            order_id_strs.append(str(oid))

    # collect line ids
    line_ids = []
    if "line_ids" in request.form:
        line_ids += [request.form.get("line_ids") or ""]
    line_ids += request.form.getlist("line_ids[]")
    line_ids = ",".join([s for s in line_ids if s]).split(",")
    line_ids = [s.strip() for s in line_ids if s.strip()]

    valid_line_ids = []
    for lid in line_ids:
        parsed = _parse_line_id(lid)
        if parsed:
            source, oid, idx = parsed
            if source == "main":
                valid_line_ids.append(f"{oid}:{idx}")

    if not order_id_strs and not valid_line_ids:
        flash("Please select at least one valid order or line.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))
    if valid_line_ids and status == "completed" and not order_id_strs:
        flash("Completed is an order-only status. Choose another status for lines.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # compute run time
    delay_str  = (request.form.get("delay_minutes") or "").strip()
    run_at_str = (request.form.get("run_at") or "").strip()
    run_time   = None

    if delay_str:
        try:
            mins = int(delay_str)
            run_time = datetime.utcnow() + timedelta(minutes=max(0, mins))
        except Exception:
            flash("Invalid delay minutes.", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
    elif run_at_str:
        dt = _parse_date(run_at_str)
        if not dt:
            flash("Invalid run_at datetime. Use 'YYYY-MM-DD HH:MM' (UTC).", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
        run_time = dt
        if run_time < datetime.utcnow():
            flash("Run time must be in the future.", "warning")
            return redirect(url_for("admin_orders.admin_view_orders"))
    else:
        flash("Provide either delay_minutes or run_at.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    note = (request.form.get("note") or "").strip()
    admin_id = (session.get("user_id") or None)
    job = _enqueue_status_job(order_id_strs, status, run_time, str(admin_id) if admin_id else None, note, line_ids=valid_line_ids)

    target_count = len(order_id_strs) or len(valid_line_ids)
    target_label = "order(s)" if order_id_strs else "line(s)"
    flash(f"⏱️ Scheduled {target_count} {target_label} → {status} at {run_time.strftime('%Y-%m-%d %H:%M')} UTC.", "success")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/schedules", methods=["GET"])
def list_schedules():
    """Returns JSON of recent schedules (for the offcanvas in the UI)."""
    if not _require_admin():
        return redirect(url_for("login.login"))
    # Also opportunistically process due jobs when viewing the list
    try:
        _process_due_jobs(max_batch=25)
    except Exception:
        pass
    if _require_main_admin():
        try:
            _process_auto_update_rules()
        except Exception:
            pass

    jobs = []
    for j in schedules_col.find({}).sort([("created_at", -1)]).limit(100):
        jobs.append({
            "id": str(j.get("_id")),
            "job_key": j.get("job_key"),
            "next_run_time": j.get("run_at").strftime("%Y-%m-%d %H:%M:%S UTC") if j.get("run_at") else None,
            "state": j.get("state"),
            "status": j.get("status"),
            "args": [j.get("order_ids"), j.get("status")],
            "result": j.get("result"),
            "attempts": j.get("attempts", 0),
        })
    return jsonify({"jobs": jobs})

@admin_orders_bp.route("/admin/orders/schedules/<job_id>/cancel", methods=["POST"])
def cancel_schedule(job_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    try:
        res = schedules_col.update_one({"_id": ObjectId(job_id)}, {"$set": {"state": "cancelled"}})
        if res.modified_count:
            flash("🗑️ Schedule cancelled.", "success")
        else:
            flash("Schedule not found.", "warning")
    except Exception as e:
        flash(f"Failed to cancel schedule: {e}", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# Optional: endpoint you can ping from Render Cron every minute
@admin_orders_bp.route("/admin/orders/schedules/run-due", methods=["POST", "GET"])
def run_due_schedules():
    is_admin = _require_admin()
    is_cron = _cron_runner_authorized()
    if not is_admin and not is_cron:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        _process_due_jobs(max_batch=50)
        auto_result = _process_auto_update_rules(force=True) if (is_cron or _require_main_admin()) else {"processed_rules": 0, "matched_lines": 0, "updated_lines": 0}
        return jsonify({"ok": True, "auto_update": auto_result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
