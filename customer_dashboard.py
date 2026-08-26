from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from bson import ObjectId
from db import db
import json, ast, re
from copy import deepcopy
from datetime import datetime, date, timedelta
from threading import RLock
from time import time
from typing import Optional, Any, Dict, List, Tuple  # add Tuple for 3.8/3.9
from urllib.parse import urlsplit
from afa_settings_utils import load_afa_settings
from agent_code_utils import get_or_create_agent_code_for_user, set_agent_code_status_for_user
from order_display import build_order_display_items
from tenant import resolve_admin_id_for_user_id
from announcements import get_popup_announcement
from bulk_sms import bulk_sms_context_for_customer
from social_boosting_pricing import (
    SOCIAL_BOOSTING_IMAGE_URL,
    SOCIAL_BOOSTING_NAME,
    SOCIAL_BOOSTING_SERVICE_ID,
    admin_rate_per_1000,
    apply_default_offer_fields,
    custom_comments_text,
    customer_rate_per_1000,
    is_social_boosting_service,
    normalize_admin_level,
    normalize_custom_comments,
    offer_requires_custom_comments,
    offer_service_id,
    service_rate_per_1000,
    usd_to_ghs_rate,
)

customer_dashboard_bp = Blueprint("customer_dashboard", __name__)

# --- Collections ---
services_col         = db["services"]
balances_col         = db["balances"]
orders_col           = db["orders"]
service_profits_col  = db["service_profits"]   # per-customer overrides
users_col            = db["users"]             # for display name
stores_col           = db["stores"]
store_accounts_col   = db["store_accounts"]
afa_col              = db["afa_registrations"] # AFA registrations
balance_logs_col     = db["balance_logs"]      # wallet logs
transactions_col     = db["transactions"]

_DASHBOARD_CACHE: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
_DASHBOARD_CACHE_LOCK = RLock()

# ---------- helpers ----------
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)
_mapping_like = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)

def _now() -> datetime:
    return datetime.utcnow()

def _to_float(x: Any) -> Optional[float]:
    """
    Safely convert numbers, Mongo Extended JSON (e.g. {'$numberDouble':'15.0'}),
    strings like '15', etc. to float.
    """
    try:
        # Handle {"$numberDouble": "..."} or {"$numberInt": "..."}
        if isinstance(x, dict):
            for k in ("$numberDouble", "$numberInt", "$numberDecimal", "$numberLong"):
                if k in x:
                    return float(x[k])
        return float(x)
    except Exception:
        return None


def _cached_copy(key: Tuple[Any, ...], ttl_seconds: float, loader):
    now = time()
    with _DASHBOARD_CACHE_LOCK:
        entry = _DASHBOARD_CACHE.get(key)
        if entry and float(entry.get("expires_at") or 0) > now:
            return deepcopy(entry.get("value"))
        if entry:
            _DASHBOARD_CACHE.pop(key, None)
    value = loader()
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE[key] = {
            "expires_at": now + max(1.0, float(ttl_seconds or 1)),
            "value": deepcopy(value),
        }
    return deepcopy(value)


def _dashboard_cache_key(*parts: Any) -> Tuple[Any, ...]:
    role = (session.get("role") or "").strip().lower()
    user_id = session.get("user_id") or ""
    return ("customer_dashboard", role, str(user_id), *parts)


def _safe_redirect_target(target: Any) -> str | None:
    raw = str(target or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return None
    if not raw.startswith("/"):
        return None
    return raw

# ---- unit helpers ------------------------------------------------------------

def _service_unit(svc: Dict[str, Any]) -> str:
    """
    Returns the unit for a service:
      - 'minutes' for AFA talktime (by name or optional svc['unit']=='minutes')
      - 'data' (MB/GB) for everything else
    """
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes"):
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    # default 'data': MB
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _parse_value_field(value: Any) -> Any:
    """
    Accepts:
      - dict like {"id": 50, "volume": 20000}
      - Python-like string "{'id': 50, 'volume': 20000}"
      - raw string like "1GB" or "1000MB" or "250 MIN"
      - display string like "GHS 160 — 1GB (Pkg 2)"
    Returns either dict (preferred) or the original string.
    """
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            # try JSON first
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                # then tolerant Python-literal
                try:
                    if _mapping_like.match(vt):
                        data = ast.literal_eval(vt)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    """Return numeric volume for sorting (MB for data, minutes for talktime)."""
    if isinstance(value, dict):
        vol = value.get("volume")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            return float(vol)
        # textual volume
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)  # assume MB
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m:
                return float(m.group(1))
            if _NUM.match(s):
                return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m:
                return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m:
                return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m:
                return float(m.group(1))
            if _NUM.match(s2):
                return float(s2)  # assume MB
            return None
    return None

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"

def _offer_id_from_value(value: Any, idx: int) -> int:
    if isinstance(value, dict) and value.get("id") is not None:
        try:
            return int(value.get("id"))
        except Exception:
            pass
    return idx

def _stage_key(stage_label: Optional[str]) -> str:
    s = (stage_label or "").strip().lower().replace("-", " ").replace("_", " ")
    if s in {"elite", "elite agent"}:
        return "elite_agent"
    if s in {"premium", "premium agent"}:
        return "premium"
    return "normal_agent"

def _stage_price_for_offer(offer: Dict[str, Any], stage_label: Optional[str]) -> Optional[float]:
    sp = offer.get("stage_prices")
    if not isinstance(sp, dict):
        return None

    key = _stage_key(stage_label)
    if key in sp:
        return _to_float(sp.get(key))

    aliases = {
        "normal_agent": ("normal", "normal_agent", "normal agent"),
        "elite_agent": ("elite", "elite_agent", "elite agent"),
        "premium": ("premium", "premium_agent"),
    }.get(key, ())
    lowered = {str(k).strip().lower(): v for k, v in sp.items()}
    for a in aliases:
        if a in lowered:
            return _to_float(lowered.get(a))
    return None

def _customer_price_for_offer(
    service_doc: Dict[str, Any],
    offer: Dict[str, Any],
    offer_id: int,
    customer_id_obj: Optional[ObjectId] = None,
    stage_label: Optional[str] = None,
) -> Optional[float]:
    stage_price = _stage_price_for_offer(offer, stage_label)
    if stage_price is not None:
        return round(stage_price, 2)
    return None

# ---- service ordering ----
PREFERRED_ORDER: List[str] = [
    "MTN",
    "AT - iShare",
    "AT - BigTime",
    "AFA TALKTIME",
]

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _name_rank(name: str) -> Optional[int]:
    n = _norm(name)
    for i, want in enumerate(PREFERRED_ORDER):
        if _norm(want) == n:
            return i
    n2 = " ".join(n.split())
    for i, want in enumerate(PREFERRED_ORDER):
        if " ".join(_norm(want).split()) == n2:
            return i
    return None

def _created_ts(service_doc: Dict[str, Any]) -> float:
    ca = service_doc.get("created_at")
    if isinstance(ca, datetime):
        return ca.timestamp()
    try:
        val = float(ca)
        if val > 1e12:
            return val / 1000.0
        return val
    except Exception:
        return 0.0

def _service_priority_tuple(svc: Dict[str, Any]):
    prio = _to_float(svc.get("priority"))
    prio = prio if prio is not None else float("inf")
    name = svc.get("name") or ""
    nrank = _name_rank(name)
    nrank = nrank if nrank is not None else 10_000
    display_order = _to_float(svc.get("display_order"))
    display_order = display_order if display_order is not None else float("inf")
    ts = -_created_ts(svc)
    alpha = _norm(name)
    return (prio, nrank, display_order, ts, alpha)

def _display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Customer"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    first = (user_doc.get("first_name") or "").strip()
    last  = (user_doc.get("last_name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "Customer"

# ---- service-state helper ----------------------------------------------------

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize flags + derive if the service can be ordered.
    """
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()               # OPEN | CLOSED
    availability = (svc.get("availability") or "AVAILABLE").upper()  # AVAILABLE | OUT_OF_STOCK

    # optional custom messages stored on the service doc
    closed_msg = (svc.get("closed_message") or "This service is temporarily closed.")
    oos_msg = (svc.get("out_of_stock_message") or "This service is currently out of stock.")

    can_order = (t == "API" and status == "OPEN" and availability == "AVAILABLE")

    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        elif t != "API":
            disabled_reason = "This service is currently unavailable."

    return {
        "type": t,
        "status": status,
        "availability": availability,
        "closed_message": closed_msg,
        "out_of_stock_message": oos_msg,
        "can_order": can_order,
        "disabled_reason": disabled_reason
    }

# ---------- AFA settings loader (price / open / stock) ----------

def _load_afa_settings(admin_oid: ObjectId | None = None) -> Dict[str, Any]:
    return load_afa_settings(admin_oid, default_price=2.00)

# ---------- NEW: customer daily sales (today + last 5) ----------

def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end

def compute_user_daily_sales(user_oid: ObjectId, days_back: int = 6) -> Dict[str, Any]:
    """
    Customer 'sales' = sum of their order totals (customer-facing price).
    Uses orders.total_amount per day.
    If you prefer charged-only, switch "$total_amount" to "$charged_amount" below.
    Returns labels, values, today_sales, yesterday_sales, change_pct, trend, statement.
    """
    today = datetime.utcnow().date()
    # previous 5 days + today, in chronological order
    days = [today - timedelta(days=i) for i in range(days_back)][::-1]

    window_start, _ = _day_range(days[0])
    _, window_end = _day_range(days[-1])  # end-of-today

    # include store-page sales for stores owned by this user
    store_slugs = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1}
            )
            if s.get("slug")
        ]
    except Exception:
        store_slugs = []

    match_or = [{"user_id": user_oid}]
    if store_slugs:
        match_or.append({"store_slug": {"$in": store_slugs}})

    pipeline = [
        {"$match": {
            "$or": match_or,
            "created_at": {"$gte": window_start, "$lt": window_end},
            # If needed: "status": "completed",
        }},
        {"$project": {
            "d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}},
            "amt": {"$ifNull": ["$total_amount", 0]},
        }},
        {"$group": {"_id": "$d", "sales": {"$sum": "$amt"}}},
    ]

    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    by_day: Dict[date, float] = {}
    for row in agg:
        dt = row.get("_id")
        if isinstance(dt, datetime):
            by_day[dt.date()] = float(row.get("sales", 0) or 0)

    labels: List[str] = []
    values: List[float] = []
    for d in days:
        labels.append("Today" if d == today else d.strftime("%b %d"))
        values.append(round(by_day.get(d, 0.0), 2))

    today_sales = values[-1] if values else 0.0
    yesterday_sales = values[-2] if len(values) >= 2 else 0.0

    if yesterday_sales == 0:
        change_pct = 100.0 if today_sales > 0 else 0.0
    else:
        change_pct = ((today_sales - yesterday_sales) / abs(yesterday_sales)) * 100.0

    if abs(today_sales - yesterday_sales) < 1e-9:
        trend = "flat"
        statement = "Today’s purchases are the same as yesterday."
    elif today_sales > yesterday_sales:
        trend = "up"
        diff = round(today_sales - yesterday_sales, 2)
        pct = round(change_pct, 2)
        statement = f"Today’s purchases have risen by {pct}% compared to yesterday (up GHS {diff:,.2f})."
    else:
        trend = "down"
        diff = round(yesterday_sales - today_sales, 2)
        pct = round(abs(change_pct), 2)
        statement = f"Today’s purchases have fallen by {pct}% compared to yesterday (down GHS {diff:,.2f})."

    return {
        "labels": labels,
        "values": values,
        "today_sales": round(today_sales, 2),
        "yesterday_sales": round(yesterday_sales, 2),
        "change_pct": round(change_pct, 2),
        "trend": trend,
        "statement": statement,
    }

# ---------- globals ----------
@customer_dashboard_bp.app_context_processor
def inject_customer_globals():
    bal = 0.0
    uname = session.get("username")
    stage_label = None
    try:
        if session.get("role") in {"customer", "agent"} and session.get("user_id"):
            uid = ObjectId(session["user_id"])
            bal_doc = balances_col.find_one({"user_id": uid})
            if bal_doc and bal_doc.get("amount") is not None:
                bal = float(bal_doc["amount"])
            user_doc = users_col.find_one({"_id": uid}, {
                "full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1, "stage_label": 1
            })
            uname = _display_name(user_doc)
            if user_doc:
                stage_label = user_doc.get("stage_label")
    except Exception:
        pass
    return {
        "customer_balance": bal,
        "customer_username": uname or "Customer",
        "customer_stage_label": stage_label or "Normal Agent",
    }

# ---------- API: Customer AFA Registration (charge immediately) ----------
@customer_dashboard_bp.route("/api/afa/register", methods=["POST"])
def api_afa_register():
    # Auth: customers and agents
    if session.get("role") not in {"customer", "agent"} or not session.get("user_id"):
        return jsonify(success=False, error="Unauthorized"), 401

    user_oid = ObjectId(session["user_id"])
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    if not admin_id:
        return jsonify(success=False, error="Account is not mapped to an admin"), 400

    payload = request.get_json(silent=True) or {}
    name       = (payload.get("name") or "").strip()
    phone      = (payload.get("phone") or "").strip()
    dob        = (payload.get("dob") or None)
    location   = (payload.get("location") or None)
    ghana_card = (payload.get("ghana_card") or None)

    # Basic validation
    if not name:
        return jsonify(success=False, error="Name is required"), 400
    if not re.match(r"^0\d{9}$", phone):
        return jsonify(success=False, error="Phone must be 0xxxxxxxxx"), 400

    # Load AFA settings (single source of truth)
    afa = _load_afa_settings(admin_id)
    if not afa["is_open"]:
        return jsonify(success=False, error="Service closed"), 400
    if not afa["in_stock"]:
        return jsonify(success=False, error="Out of stock"), 400

    price = _to_float(afa.get("price")) or 0.0
    if price < 0:
        price = 0.0

    now = _now()

    # Atomic charge: guard against insufficient funds
    upd = balances_col.update_one(
        {"user_id": user_oid, "amount": {"$gte": price}},
        {"$inc": {"amount": -price}, "$set": {"updated_at": now}, "$setOnInsert": {"admin_id": admin_id}},
        upsert=False
    )
    if upd.matched_count == 0:
        return jsonify(success=False, error="Insufficient funds"), 400

    # Fetch new balance (best effort)
    bal_doc = balances_col.find_one({"user_id": user_oid}) or {}
    new_balance = float(bal_doc.get("amount", 0.0) or 0.0)

    # Log balance change
    actor_name = session.get("username") or session.get("email") or "customer"
    log_doc = {
        "user_id": user_oid,
        "admin_id": admin_id,
        "action": "withdraw",
        "delta": -price,
        "amount_before": None,  # Optional: keep None or compute preimage with find_one_and_update if required
        "amount_after": new_balance,
        "currency": bal_doc.get("currency", "GHS"),
        "note": "AFA registration (customer self-charge)",
        "actor_id": user_oid,
        "actor_name": actor_name,
        "created_at": now,
    }
    log_res = balance_logs_col.insert_one(log_doc)

    # Create registration (already charged)
    reg_doc = {
        "customer_id": user_oid,
        "admin_id": admin_id,
        "name": name,
        "phone": phone,
        "dob": dob or None,
        "location": location or None,
        "ghana_card": ghana_card or None,

        "status": "pending",
        "charged": True,
        "amount": price,                 # normalize UI amount to settings price used
        "charged_amount": price,
        "charged_at": now,
        "charged_by": actor_name,
        "charge_log_id": log_res.inserted_id,

        "created_at": now,
        "updated_at": now,
    }
    reg_id = afa_col.insert_one(reg_doc).inserted_id

    return jsonify(
        success=True,
        message="Registration submitted and charged.",
        registration_id=str(reg_id),
        balance=new_balance,
        price=price
    ), 200


@customer_dashboard_bp.route("/api/store-notifications", methods=["GET"])
def api_store_notifications():
    if session.get("role") not in {"customer", "agent"} or not session.get("user_id"):
        return jsonify(success=False, error="Unauthorized"), 401

    user_oid = ObjectId(session["user_id"])
    store_slugs: List[str] = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1}
            )
            if s.get("slug")
        ]
    except Exception:
        store_slugs = []

    if not store_slugs:
        return jsonify(success=True, orders=[], payments=[])

    orders_view: List[Dict[str, Any]] = []
    try:
        cur = (
            orders_col.find(
                {"store_slug": {"$in": store_slugs}},
                {
                    "order_id": 1,
                    "store_slug": 1,
                    "items": 1,
                    "total_amount": 1,
                    "status": 1,
                    "created_at": 1,
                    "paystack_reference": 1,
                }
            )
            .sort("created_at", -1)
            .limit(30)
        )
        for od in cur:
            created_at = od.get("created_at")
            created_iso = created_at.isoformat() if isinstance(created_at, datetime) else ""
            created_fmt = created_at.strftime("%d %b %Y, %I:%M %p") if isinstance(created_at, datetime) else ""
            items = od.get("items") or []
            phone = ""
            if items and isinstance(items[0], dict):
                phone = items[0].get("phone") or ""
            orders_view.append({
                "order_id": od.get("order_id"),
                "store_slug": od.get("store_slug"),
                "phone": phone,
                "total_amount": _to_float(od.get("total_amount")) or 0.0,
                "status": od.get("status") or "",
                "paystack_reference": (od.get("paystack_reference") or "").strip(),
                "created_at_iso": created_iso,
                "created_at_fmt": created_fmt,
            })
    except Exception:
        orders_view = []

    payments_view: List[Dict[str, Any]] = []
    try:
        q = {
            "status": "success",
            "meta.store_slug": {"$in": store_slugs},
            "$or": [
                {"source": {"$in": ["paystack_inline", "paystack_transfer", "paystack"]}},
                {"gateway": {"$regex": "paystack", "$options": "i"}},
            ],
        }
        cur = (
            transactions_col.find(
                q,
                {
                    "amount": 1,
                    "reference": 1,
                    "paystack_reference": 1,
                    "verified_at": 1,
                    "created_at": 1,
                    "meta.store_slug": 1,
                }
            )
            .sort([("verified_at", -1), ("created_at", -1)])
            .limit(30)
        )
        for tx in cur:
            ts = tx.get("verified_at") or tx.get("created_at")
            created_iso = ts.isoformat() if isinstance(ts, datetime) else ""
            created_fmt = ts.strftime("%d %b %Y, %I:%M %p") if isinstance(ts, datetime) else ""
            payments_view.append({
                "amount": _to_float(tx.get("amount")) or 0.0,
                "reference": (tx.get("reference") or "").strip(),
                "paystack_reference": (tx.get("paystack_reference") or "").strip(),
                "created_at_iso": created_iso,
                "created_at_fmt": created_fmt,
            })
    except Exception:
        payments_view = []

    return jsonify(success=True, orders=orders_view, payments=payments_view)


def _build_dashboard_services(
    admin_id: Optional[ObjectId],
    user_oid: ObjectId,
    user_stage_label: str,
    admin_level: str,
) -> Dict[str, List[Dict[str, Any]]]:
    raw_services = list(services_col.find({
        "$or": [
            {
                "admin_id": admin_id,
                "_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "base_service_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "name": {"$ne": SOCIAL_BOOSTING_NAME},
            },
            {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        ],
        "agent_visible": {"$ne": False},
        "display_enabled": {"$ne": False},
        f"agent_visibility_by_admin.{str(admin_id)}": {"$ne": False},
    })) if admin_id else []
    raw_services.sort(key=_service_priority_tuple)

    services: List[Dict[str, Any]] = []
    for s in raw_services:
        s["_id_str"] = str(s["_id"])
        st = _service_state(s)
        s.update(st)

        if is_social_boosting_service(s):
            s["is_social_boosting"] = True
            s["image_url"] = SOCIAL_BOOSTING_IMAGE_URL
            s["display_name"] = "Boosting"
            normalized_social_offers: List[Dict[str, Any]] = []
            for idx, of in enumerate(s.get("services_offers") or [], start=1):
                if not isinstance(of, dict):
                    continue
                apply_default_offer_fields(of)
                provider_service_id = offer_service_id(of) or idx
                customer_rate_usd = customer_rate_per_1000(of, admin_level, admin_id, user_stage_label)
                admin_rate_usd = admin_rate_per_1000(of, admin_level)
                base_rate_usd = float(service_rate_per_1000(of))
                customer_rate_ghs = usd_to_ghs_rate(customer_rate_usd)
                admin_rate_ghs = usd_to_ghs_rate(admin_rate_usd)
                base_rate_ghs = usd_to_ghs_rate(base_rate_usd)
                normalized_social_offers.append({
                    "is_social_boosting": True,
                    "amount": admin_rate_ghs,
                    "amount_usd": admin_rate_usd,
                    "total": customer_rate_ghs,
                    "total_usd": customer_rate_usd,
                    "customer_price": customer_rate_ghs,
                    "customer_price_usd": customer_rate_usd,
                    "rate_per_1000": customer_rate_ghs,
                    "rate_per_1000_ghs": customer_rate_ghs,
                    "rate_per_1000_usd": customer_rate_usd,
                    "admin_rate_per_1000": admin_rate_ghs,
                    "admin_rate_per_1000_ghs": admin_rate_ghs,
                    "admin_rate_per_1000_usd": admin_rate_usd,
                    "base_rate_per_1000": base_rate_ghs,
                    "base_rate_per_1000_ghs": base_rate_ghs,
                    "base_rate_per_1000_usd": base_rate_usd,
                    "usd_to_ghs_rate": 11.01,
                    "currency": "USD",
                    "display_currency": "GHS",
                    "provider": "exosupplier",
                    "provider_service_id": provider_service_id,
                    "offer_id": provider_service_id,
                    "offer_type": of.get("type") or "",
                    "requires_custom_comments": offer_requires_custom_comments(of),
                    "value": {
                        "social_boosting": True,
                        "provider_service_id": provider_service_id,
                        "quantity_min": of.get("min"),
                        "quantity_max": of.get("max"),
                        "offer_type": of.get("type") or "",
                        "requires_custom_comments": offer_requires_custom_comments(of),
                        "comments": normalize_custom_comments(of),
                        "comments_text": custom_comments_text(of),
                    },
                    "value_text": of.get("name") or f"Service {provider_service_id}",
                    "name": of.get("name") or "",
                    "social_media": of.get("social_media") or "",
                    "category": of.get("category") or "",
                    "min": of.get("min"),
                    "max": of.get("max"),
                    "_sort_platform": of.get("social_media") or "",
                    "_sort_name": of.get("name") or "",
                })
            normalized_social_offers.sort(key=lambda x: (x["_sort_platform"], x["_sort_name"]))
            s["offers"] = [{k: v for k, v in o.items() if not k.startswith("_sort_")} for o in normalized_social_offers]
            s["unit"] = "social"
            services.append(s)
            continue

        unit = _service_unit(s)
        normalized_offers: List[Dict[str, Any]] = []
        for idx, of in enumerate(s.get("offers") or [], start=1):
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)
            value_text = _value_text_for_display(parsed_value, unit)
            amount = _to_float(of.get("amount"))
            offer_id = _offer_id_from_value(parsed_value, idx)
            total = _customer_price_for_offer(s, of, offer_id, user_oid, user_stage_label)
            normalized_offers.append({
                "amount": amount,
                "value": parsed_value,
                "value_text": value_text,
                "legacy_profit": _to_float(of.get("profit")),
                "total": total,
                "offer_id": offer_id,
                "customer_price": total,
                "_sort_vol": vol_num if vol_num is not None else float("inf"),
                "_sort_amt": amount if amount is not None else float("inf"),
            })
        normalized_offers.sort(key=lambda x: (x["_sort_vol"], x["_sort_amt"]))
        s["offers"] = [{k: v for k, v in o.items() if not k.startswith("_sort_")} for o in normalized_offers]
        s["unit"] = unit
        services.append(s)

    def _is_express(svc: Dict[str, Any]) -> bool:
        cat = (svc.get("service_category") or "").strip().lower()
        cat2 = (svc.get("category") or "").strip().lower()
        return cat == "express services" or cat2 == "express"

    return {
        "regular": [s for s in services if not _is_express(s)],
        "express": [s for s in services if _is_express(s)],
    }


def _load_recent_orders_view(user_oid: ObjectId) -> List[Dict[str, Any]]:
    recent_orders = list(
        orders_col.find(
            {
                "user_id": user_oid,
                "items": {"$elemMatch": {"provider": {"$ne": "exosupplier"}}},
            }
        )
        .sort("created_at", -1)
        .limit(5)
    )
    for order in recent_orders:
        normal_items = [
            item for item in (order.get("items") or [])
            if (item.get("provider") or "").strip().lower() != "exosupplier"
        ]
        order["items"] = normal_items
        order["display_items"] = build_order_display_items(normal_items)
        try:
            order["total_amount"] = round(sum(float(item.get("amount") or 0) for item in normal_items), 2)
        except Exception:
            order["total_amount"] = 0.0
    return recent_orders


def _load_store_snapshot(user_oid: ObjectId) -> Dict[str, Any]:
    outstanding_payouts = 0.0
    store_slugs: List[str] = []
    store_recent_orders: List[Dict[str, Any]] = []
    store_recent_orders_view: List[Dict[str, Any]] = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1}
            )
            if s.get("slug")
        ]
        if store_slugs:
            pipeline = [
                {"$match": {"store_slug": {"$in": store_slugs}}},
                {"$group": {
                    "_id": None,
                    "total": {"$sum": {"$toDouble": {"$ifNull": ["$total_profit_balance", 0]}}},
                }},
            ]
            agg = list(store_accounts_col.aggregate(pipeline))
            if agg:
                outstanding_payouts = _to_float(agg[0].get("total")) or 0.0

            store_recent_orders = list(
                orders_col.find(
                    {"store_slug": {"$in": store_slugs}},
                    {
                        "order_id": 1,
                        "store_slug": 1,
                        "items": 1,
                        "total_amount": 1,
                        "status": 1,
                        "created_at": 1,
                        "paystack_reference": 1,
                    }
                )
                .sort("created_at", -1)
            )
            for od in store_recent_orders:
                created_at = od.get("created_at")
                created_iso = created_at.isoformat() if isinstance(created_at, datetime) else ""
                created_fmt = created_at.strftime("%d %b %Y, %I:%M %p") if isinstance(created_at, datetime) else ""
                items = od.get("items") or []
                phone = ""
                if items and isinstance(items[0], dict):
                    phone = items[0].get("phone") or ""
                store_recent_orders_view.append({
                    "order_id": od.get("order_id"),
                    "store_slug": od.get("store_slug"),
                    "phone": phone,
                    "total_amount": _to_float(od.get("total_amount")) or 0.0,
                    "status": od.get("status") or "",
                    "paystack_reference": (od.get("paystack_reference") or "").strip(),
                    "created_at_iso": created_iso,
                    "created_at_fmt": created_fmt,
                })
    except Exception:
        pass
    return {
        "outstanding_payouts": outstanding_payouts,
        "store_slugs": store_slugs,
        "store_recent_orders": store_recent_orders,
        "store_recent_orders_view": store_recent_orders_view,
    }


def _safe_popup_announcement(role: Any, admin_id: Any):
    try:
        return get_popup_announcement(role, admin_id, session.get("user_id"))
    except Exception:
        return None


def _safe_bulk_sms_context(user_oid: ObjectId) -> Dict[str, Any]:
    try:
        return bulk_sms_context_for_customer(user_oid)
    except Exception:
        return {
            "available": False,
            "price_per_number": None,
            "service_id": "",
            "disabled_reason": "Bulk SMS is not available right now.",
            "disclaimer": "",
        }


def _load_customer_dashboard_snapshot(user_oid: ObjectId, role: str) -> Dict[str, Any]:
    user_doc = users_col.find_one(
        {"_id": user_oid},
        {"full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1, "stage_label": 1},
    ) or {}
    customer_name = _display_name(user_doc)
    user_stage_label = (user_doc or {}).get("stage_label") or "Normal Agent"

    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    admin_doc = (users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}) or {}
    admin_level = normalize_admin_level((admin_doc or {}).get("admin_level"))

    services_bundle = _build_dashboard_services(admin_id, user_oid, user_stage_label, admin_level)
    recent_orders = _load_recent_orders_view(user_oid)
    store_snapshot = _load_store_snapshot(user_oid)
    afa = _load_afa_settings(admin_id)
    ds = compute_user_daily_sales(user_oid, days_back=6)
    bulk_sms = _safe_bulk_sms_context(user_oid)
    store_slugs = store_snapshot.get("store_slugs") or []
    agent_code_doc = get_or_create_agent_code_for_user(user_oid, admin_id=admin_id)
    agent_code = {
        "agent_code": (agent_code_doc or {}).get("agent_code") or "",
        "status": ((agent_code_doc or {}).get("status") or "active").strip().lower(),
        "id": str((agent_code_doc or {}).get("_id") or ""),
    }

    return {
        "customer_name": customer_name,
        "services_bundle": services_bundle,
        "recent_orders": recent_orders,
        "store_snapshot": store_snapshot,
        "afa": afa,
        "daily_sales": ds,
        "bulk_sms": bulk_sms,
        "store_notify_key": ",".join(sorted(store_slugs)) if store_slugs else "none",
        "agent_code": agent_code,
    }

# ---------- route ----------
@customer_dashboard_bp.route("/customer/dashboard")
def customer_dashboard():
    if session.get("role") not in {"customer", "agent"}:
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))
    user_oid = ObjectId(user_id)
    role = (session.get("role") or "").strip().lower()
    snapshot = _cached_copy(
        _dashboard_cache_key("snapshot"),
        25,
        lambda: _load_customer_dashboard_snapshot(user_oid, role),
    )

    customer_name = snapshot.get("customer_name") or ""
    services_bundle = snapshot.get("services_bundle") or {}

    # Balance
    balance_doc = balances_col.find_one({"user_id": user_oid})
    balance = float(balance_doc["amount"]) if (balance_doc and balance_doc.get("amount") is not None) else 0.00

    recent_orders = snapshot.get("recent_orders") or []
    store_snapshot = snapshot.get("store_snapshot") or {}
    outstanding_payouts = float(store_snapshot.get("outstanding_payouts") or 0.0)
    store_slugs = store_snapshot.get("store_slugs") or []
    store_recent_orders = store_snapshot.get("store_recent_orders") or []
    store_recent_orders_view = store_snapshot.get("store_recent_orders_view") or []

    express_services = services_bundle.get("express") or []
    regular_services = services_bundle.get("regular") or []

    # AFA settings (price / open / stock) — decoupled from services
    afa = snapshot.get("afa") or {}

    # Affordability for AFA button state on the page
    can_buy_afa = bool(afa["is_open"] and afa["in_stock"] and balance >= float(afa["price"] or 0.0))

    # NEW: the customer’s own sales trend (today + last 5 days)
    ds = snapshot.get("daily_sales") or {}
    store_notify_key = snapshot.get("store_notify_key") or ("none" if not store_slugs else ",".join(sorted(store_slugs)))

    # Popup announcements are checked outside the cached dashboard snapshot so
    # newly published popups can appear for agents/customers immediately.
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    announcement_popup = _safe_popup_announcement(role, admin_id)
    bulk_sms = snapshot.get("bulk_sms") or {}
    agent_code = snapshot.get("agent_code") or {}

    return render_template(
        "customer_dashboard.html",
        services=regular_services,         # keep old variable working for existing section
        express_services=express_services, # NEW
        balance=balance,
        recent_orders=recent_orders,
        customer_name=customer_name,
        afa=afa,                           # pass settings for the AFA block in your HTML
        can_buy_afa=can_buy_afa,           # NEW: enable/disable Buy button for AFA

        # sales KPIs for the hero section
        today_sales=ds["today_sales"],
        yesterday_sales=ds["yesterday_sales"],
        sales_change_pct=ds["change_pct"],
        sales_trend=ds["trend"],
        sales_statement=ds["statement"],
        daily_sales_labels=ds["labels"],
        daily_sales_values=ds["values"],
        outstanding_payouts=outstanding_payouts,
        store_recent_orders=store_recent_orders,
        store_recent_orders_view=store_recent_orders_view,
        has_store=bool(store_slugs),
        store_notify_key=store_notify_key,
        announcement_popup=announcement_popup,
        bulk_sms=bulk_sms,
        agent_code=agent_code,
        defer_internal_chat=True,
    )


@customer_dashboard_bp.route("/agent-code/status", methods=["POST"])
def update_my_agent_code_status():
    if session.get("role") not in {"customer", "agent"} or not session.get("user_id"):
        return redirect(url_for("login.login"))

    try:
        user_oid = ObjectId(session["user_id"])
    except Exception:
        return redirect(url_for("login.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    doc = set_agent_code_status_for_user(
        user_oid,
        new_status,
        admin_id=admin_id,
        actor_user_id=user_oid,
    )
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE.pop(_dashboard_cache_key("snapshot"), None)

    if request.is_json:
        if not doc:
            return jsonify(success=False, message="Invalid agent code status."), 400
        return jsonify(
            success=True,
            agent_code=(doc.get("agent_code") or ""),
            status=(doc.get("status") or "active"),
        )

    target = _safe_redirect_target(request.form.get("next")) or _safe_redirect_target(request.referrer)
    return redirect(target or url_for("customer_dashboard.customer_dashboard"))
