from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, Request
from db import db
from datetime import datetime
from bson import ObjectId
from werkzeug.utils import secure_filename
import os
import json
import uuid
import re
from ast import literal_eval
from afa_settings_utils import (
    DEFAULT_AFA_PRICE,
    SETTINGS_ID,
    load_afa_admin_base_price,
    load_afa_settings,
)
from tenant import current_admin_id_from_session
from service_admin_pricing import admin_stage_price_from_offer, reprice_admin_services_for_base
from social_boosting_pricing import (
    SOCIAL_BOOSTING_IMAGE_URL,
    SOCIAL_BOOSTING_NAME,
    SOCIAL_BOOSTING_SERVICE_ID,
    admin_profit_percent,
    admin_rate_per_1000,
    agent_profit_percent,
    apply_default_offer_fields,
    customer_rate_per_1000,
    is_social_boosting_service,
    normalize_admin_level,
    offer_service_id,
    percent_value,
    service_rate_per_1000,
    usd_to_ghs_rate,
)

admin_services_bp = Blueprint("admin_services", __name__)
services_col = db["services"]
users_col = db["users"]                     # customers live here
service_profits_col = db["service_profits"] # {service_id, customer_id, profit_percent, created_at, updated_at}
service_offer_prices_col = db["service_offer_prices"]  # {service_id, customer_id, offer_id, customer_price, created_at, updated_at}
afa_settings_col = db["afa_settings"]

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
MTN_NORMAL_SERVICE_ID = "68b8b6a7eb0ced45901c68d2"
BULK_SMS_SERVICE_ID = "69e36c82a8e6c7a322926fc8"

SMS_ADMIN_PRICE_KEYS = ("admin", "super_admin", "super_professional")
SMS_AGENT_PRICE_KEYS = ("normal_agent", "elite_agent", "premium")

def _ensure_upload_folder():
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _require_admin():
    return (session.get("role") or "").strip().lower() in {
        "admin",
        "main_admin",
        "super_admin",
        "professional_admin",
        "superadmin",
    }

def _admin_oid():
    return current_admin_id_from_session(session)

def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"

def _base_services_query() -> dict:
    return {"$or": [{"admin_id": {"$exists": False}}, {"admin_id": None}]}

def _parse_admin_scope_from_request(req):
    """
    Returns (admin_oid, is_base, scope_key).
    - For main_admin: scope can be 'base' (global) or a specific admin_id.
    - For normal admin: scope is always their own admin_id.
    """
    if not _is_main_admin():
        admin_oid = _admin_oid()
        return admin_oid, False, str(admin_oid) if admin_oid else ""

    scope = ""
    if req is not None:
        scope = (req.args.get("admin_scope") or "").strip()
        if not scope:
            scope = (req.form.get("admin_scope") or "").strip()
        if not scope and req.is_json:
            payload = req.get_json(silent=True) or {}
            scope = (payload.get("admin_scope") or "").strip()

    if not scope or scope.lower() in {"base", "global", "default"}:
        return None, True, "base"

    try:
        admin_oid = ObjectId(scope)
        return admin_oid, False, str(admin_oid)
    except Exception:
        return None, True, "base"

def _apply_admin_scope(base_query: dict, admin_oid, is_base: bool) -> dict:
    if is_base:
        return {"$and": [base_query, _base_services_query()]}
    if admin_oid:
        if is_social_boosting_service(base_query.get("_id")):
            return {"_id": SOCIAL_BOOSTING_SERVICE_ID}
        q = dict(base_query)
        q["admin_id"] = admin_oid
        return q
    return dict(base_query)

def _agent_visibility_for_admin(service: dict, admin_oid=None) -> bool:
    if not isinstance(service, dict):
        return True
    if admin_oid:
        visibility_map = service.get("agent_visibility_by_admin") or {}
        key = str(admin_oid)
        if isinstance(visibility_map, dict) and key in visibility_map:
            return visibility_map.get(key) is not False
    return service.get("agent_visible", True) is not False


def _service_display_enabled(service: dict) -> bool:
    if not isinstance(service, dict):
        return True
    return service.get("display_enabled", True) is not False

def _propagate_to_admin_copies(base_id: ObjectId | None, update_doc: dict) -> int:
    """
    When a base service changes (main admin/global), mirror key fields to
    all admin-cloned services with base_service_id.
    """
    if not isinstance(base_id, ObjectId):
        return 0
    try:
        res = services_col.update_many({"base_service_id": base_id}, {"$set": update_doc})
        return int(res.modified_count or 0)
    except Exception:
        return 0

_ALLOWED_TYPES = {"API", "OFF"}
def _norm_type(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().upper()
    return t if t in _ALLOWED_TYPES else None

def _to_float(s):
    try:
        return float(s)
    except Exception:
        return None

def _is_bulk_sms_service(service: dict | None = None, service_id: ObjectId | str | None = None) -> bool:
    if service and service.get("_id") and str(service.get("_id")) == BULK_SMS_SERVICE_ID:
        return True
    if service_id and str(service_id) == BULK_SMS_SERVICE_ID:
        return True
    name = (service or {}).get("name") if service else ""
    return (name or "").strip().lower() == "bulk sms"

def _sms_price_from_map(prices: dict | None, key: str) -> float | None:
    if not isinstance(prices, dict):
        return None
    val = prices.get(key)
    if val in (None, ""):
        return None
    return _to_float(val)

def _sms_admin_cost_for_level(service: dict | None, admin_level: str) -> float | None:
    if not service:
        return None
    level = normalize_admin_level(admin_level)
    prices = service.get("sms_admin_stage_prices")
    price = _sms_price_from_map(prices, level)
    if price is None and level != "admin":
        price = _sms_price_from_map(prices, "admin")
    if price is None:
        price = _to_float(service.get("sms_price_per_number"))
    return price

def _sms_main_base_price(service: dict | None) -> float | None:
    price = _to_float((service or {}).get("sms_base_price_per_number"))
    if price is not None:
        return price
    return _to_float((service or {}).get("sms_provider_base_price_per_number"))

def _sms_agent_price_for_stage(service: dict | None, stage: str) -> float | None:
    return _sms_price_from_map((service or {}).get("sms_agent_stage_prices"), stage)

def _ensure_bulk_sms_copy_for_admin(admin_oid: ObjectId | None) -> None:
    if not isinstance(admin_oid, ObjectId):
        return
    existing = services_col.find_one(
        {
            "admin_id": admin_oid,
            "$or": [
                {"base_service_id": ObjectId(BULK_SMS_SERVICE_ID)},
                {"name": {"$regex": r"^Bulk SMS$", "$options": "i"}},
            ],
        },
        {"_id": 1},
    )
    if existing:
        services_col.update_one(
            {"_id": existing["_id"]},
            {"$set": {"base_service_id": ObjectId(BULK_SMS_SERVICE_ID), "updated_at": datetime.utcnow()}},
        )
        return
    base = services_col.find_one({"_id": ObjectId(BULK_SMS_SERVICE_ID)})
    if not base:
        return
    now = datetime.utcnow()
    copy_doc = dict(base)
    copy_doc.pop("_id", None)
    copy_doc["admin_id"] = admin_oid
    copy_doc["base_service_id"] = base["_id"]
    copy_doc["cloned_at"] = now
    copy_doc["created_at"] = now
    copy_doc["updated_at"] = now
    copy_doc.setdefault("agent_visible", True)
    services_col.insert_one(copy_doc)

def _stage_price_from_offer(offer: dict, stage: str) -> float | None:
    prices = offer.get("stage_prices")
    if not isinstance(prices, dict):
        return None

    if stage in prices:
        return _to_float(prices.get(stage))

    aliases = {
        "normal_agent": ("normal", "normal_agent", "normal agent"),
        "elite_agent": ("elite", "elite_agent", "elite agent"),
        "premium": ("premium", "premium_agent"),
    }.get(stage, ())

    lowered = {str(k).strip().lower(): v for k, v in prices.items()}
    for a in aliases:
        if a in lowered:
            return _to_float(lowered.get(a))
    return None

def _admin_stage_price_from_offer(offer: dict, level: str) -> float | None:
    return admin_stage_price_from_offer(offer, level)

def _to_int(s):
    try:
        if isinstance(s, str):
            s = s.replace(",", "").strip()
        return int(float(s))
    except Exception:
        return None

_MB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*MB\s*$", re.I)
_GB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*G(?:B|IG)?\s*$", re.I)
_INT_RE = re.compile(r"^\s*[\d,]+\s*$")

def _parse_volume_to_mb(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    txt = str(v).strip()

    m = _MB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val))

    m = _GB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val * 1000))

    if _INT_RE.match(txt):
        return int(txt.replace(",", ""))

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "volume" in as_json:
                return _to_int(as_json["volume"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "volume" in d:
            return _to_int(d["volume"])
    except Exception:
        pass

    return None

def _format_volume(vol_mb):
    if vol_mb is None:
        return "-"
    try:
        vol_mb = float(vol_mb)
    except Exception:
        return "-"
    if vol_mb >= 1000:
        gb = vol_mb / 1000.0
        return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(vol_mb)}MB"

def _extract_pkg_id(value_raw):
    if value_raw is None:
        return None
    if isinstance(value_raw, (int, float)):
        return _to_int(value_raw)

    txt = str(value_raw).strip()
    if _INT_RE.match(txt):
        return _to_int(txt)

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "id" in as_json:
                return _to_int(as_json["id"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "id" in d:
            return _to_int(d["id"])
    except Exception:
        pass

    return None

def _merge_offer_extras(existing_offers: list, new_offers: list) -> list:
    """
    Preserve stage/admin pricing blocks when editing offers.
    """
    if not existing_offers or not new_offers:
        return new_offers
    existing_map = {}
    for idx, of in enumerate(existing_offers, start=1):
        key = _extract_pkg_id(of.get("value")) or idx
        existing_map[str(key)] = of

    for idx, of in enumerate(new_offers, start=1):
        key = _extract_pkg_id(of.get("value")) or idx
        old = existing_map.get(str(key))
        if not old:
            continue
        for k in ("stage_prices", "admin_stage_prices"):
            if old.get(k) and not of.get(k):
                of[k] = old.get(k)
    return new_offers

def _offer_doc_id(of: dict, idx: int) -> str:
    return str(of.get("offer_id") or _extract_pkg_id(of.get("value")) or idx)

def _drop_offer_by_id(offers: list, offer_id: str) -> tuple[list, bool]:
    if not isinstance(offers, list):
        return [], False
    kept = []
    removed = False
    for idx, of in enumerate(offers, start=1):
        if isinstance(of, dict) and _offer_doc_id(of, idx) == str(offer_id):
            removed = True
            continue
        kept.append(of)
    return kept, removed

def _to_mtn_value_string(pkg_id: int | None, volume_mb: int | None, fallback_value_raw: str | None):
    if volume_mb is None:
        volume_mb = _parse_volume_to_mb(fallback_value_raw)
    volume_mb = _to_int(volume_mb) if volume_mb is not None else None
    pkg_id = _to_int(pkg_id) if pkg_id is not None else None
    if pkg_id is None or volume_mb is None:
        return None
    return f"{{'id': {pkg_id}, 'volume': {volume_mb}}}"

def _compute_value_text_from_mtn_string(value_str: str):
    if not isinstance(value_str, str):
        return "-"
    try:
        d = literal_eval(value_str)
        if not isinstance(d, dict):
            return value_str
        vol_mb = _to_int(d.get("volume"))
        pid = _to_int(d.get("id"))
        label = _format_volume(vol_mb)
        return f"{label} (Pkg {pid})" if pid else label
    except Exception:
        vol_mb = _parse_volume_to_mb(value_str)
        if vol_mb is not None:
            return _format_volume(vol_mb)
        return value_str or "-"

# ===========================
# OFFERS PARSER (WITH PREFIX)
# ===========================
def _parse_offers(req: Request, prefix: str = "offers"):
    """
    prefix='offers'         -> uses offers_amount[], offers_value[]
    prefix='store_offers'   -> uses store_offers_amount[], store_offers_value[], store_offers_customer_price[]
    """
    amount_key = f"{prefix}_amount[]"
    value_key  = f"{prefix}_value[]"
    price_key  = f"{prefix}_customer_price[]"

    amounts = req.form.getlist(amount_key)
    values_freetext = req.form.getlist(value_key)
    customer_prices = req.form.getlist(price_key) if prefix == "store_offers" else []

    n = max(len(amounts), len(values_freetext))
    offers = []
    auto_id_seed = 1

    for i in range(n):
        amount = (amounts[i] if i < len(amounts) else "").strip()
        value_txt = (values_freetext[i] if i < len(values_freetext) else "").strip()
        customer_price_raw = (customer_prices[i] if i < len(customer_prices) else "").strip()

        if not amount and not value_txt:
            continue

        base_amount = _to_float(amount)
        customer_price = _to_float(customer_price_raw) if prefix == "store_offers" else None

        pkg_id = _extract_pkg_id(value_txt)
        vol_mb = _parse_volume_to_mb(value_txt)

        if pkg_id is None:
            pkg_id = auto_id_seed
            auto_id_seed += 1

        value_str = _to_mtn_value_string(pkg_id, vol_mb, value_txt)
        if value_str is None and (pkg_id is not None and vol_mb is not None):
            value_str = f"{{'id': {int(pkg_id)}, 'volume': {int(vol_mb)}}}"

        offer_doc = {
            "amount": base_amount,
            "value": value_str,
            "profit": None,
        }
        if prefix == "store_offers":
            offer_doc["customer_price"] = customer_price
        offers.append(offer_doc)

    return offers

# =======================
#  PROFIT LOOKUP/QUOTES
# =======================
def _get_service_default_profit(service_doc) -> float:
    p = service_doc.get("default_profit_percent")
    try:
        return float(p)
    except Exception:
        return 0.0

def _get_customer_profit_percent(service_id: ObjectId, customer_id: ObjectId):
    sp = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id})
    if not sp:
        return None
    try:
        return float(sp.get("profit_percent"))
    except Exception:
        return None

def _effective_profit_percent(service_doc, customer_id: ObjectId | None) -> float:
    if customer_id:
        cp = _get_customer_profit_percent(service_doc["_id"], customer_id)
        if cp is not None:
            return cp
    return _get_service_default_profit(service_doc)

def _quote_total(amount: float, profit_percent: float) -> dict:
    if amount is None:
        return {"amount": None, "profit": None, "total": None}
    pp = max(0.0, float(profit_percent or 0.0))
    profit_amt = round(amount * (pp / 100.0), 2)
    total = round(amount + profit_amt, 2)
    return {"amount": round(amount, 2), "profit": profit_amt, "total": total, "profit_percent": pp}

def _display_name(user_doc):
    nm = (user_doc.get("business_name") or "").strip()
    if nm:
        return nm
    fn = (user_doc.get("first_name") or "").strip()
    ln = (user_doc.get("last_name") or "").strip()
    full = (" ".join([fn, ln])).strip()
    return full or (user_doc.get("username") or user_doc.get("phone") or str(user_doc.get("_id")))

def _is_mtn_normal_service(service: dict | None, service_id: ObjectId | None = None) -> bool:
    if service and (service.get("name") or "").strip().lower() == "mtn normal":
        return True
    if service_id and str(service_id) == MTN_NORMAL_SERVICE_ID:
        return True
    if service and service.get("_id") and str(service.get("_id")) == MTN_NORMAL_SERVICE_ID:
        return True
    return False

def _is_mtn_express_service(service: dict | None) -> bool:
    if not service:
        return False
    for key in ("name", "service_network", "network"):
        val = (service.get(key) or "").strip().lower()
        if val == "mtn express":
            return True
    return False

def _is_telecel_service(service: dict | None) -> bool:
    if not service:
        return False
    name = " ".join(
        str(x)
        for x in (
            service.get("name"),
            service.get("service_network"),
            service.get("network"),
        )
        if x
    ).lower()
    return ("telecel" in name) or ("vodafone" in name)

def _is_afa_service(service: dict | None) -> bool:
    if not service:
        return False
    return (service.get("name") or "").strip().lower() == "afa registration"

def _supports_provider_switch(service: dict | None, service_id: ObjectId | None = None) -> bool:
    if not service:
        return False
    if is_social_boosting_service(service_id or service.get("_id")):
        return False
    if _is_bulk_sms_service(service, service_id):
        return False
    if _is_afa_service(service):
        return False
    return True

# =======================
#      PAGE ROUTES
# =======================
@admin_services_bp.route("/admin/services", methods=["GET"])
def manage_services():
    if not _require_admin():
        return redirect(url_for("login.login"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    if not is_base and admin_oid:
        try:
            _ensure_bulk_sms_copy_for_admin(admin_oid)
        except Exception:
            pass
    if is_base:
        query = _base_services_query()
    elif admin_oid:
        query = {"$or": [
            {
                "admin_id": admin_oid,
                "_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "base_service_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "name": {"$ne": SOCIAL_BOOSTING_NAME},
            },
            {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        ]}
    else:
        query = {"_id": {"$exists": False}}

    selected_admin_level = "admin"
    if admin_oid:
        admin_ctx = users_col.find_one({"_id": admin_oid}, {"admin_level": 1}) or {}
        selected_admin_level = normalize_admin_level(admin_ctx.get("admin_level"))

    services = list(services_col.find(query, {
        "name": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,
        "services_offers": 1,
        "services_offers_count": 1,
        "services_offers_provider": 1,
        "services_offers_synced_at": 1,
        "created_at": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "provider": 1,
        "admin_id": 1,
        "agent_visible": 1,
        "display_enabled": 1,
        "agent_visibility_by_admin": 1,
        "base_service_id": 1,
        "sms_admin_stage_prices": 1,
        "sms_agent_stage_prices": 1,
        "sms_price_per_number": 1,
        "sms_base_price_per_number": 1,
    }).sort([("_id", -1)]))

    for s in services:
        s["_id_str"] = str(s["_id"])
        s["agent_visible"] = _agent_visibility_for_admin(s, admin_oid)
        s["display_enabled"] = _service_display_enabled(s)
        if _is_bulk_sms_service(s):
            s["is_bulk_sms"] = True
            sms_admin_prices = s.get("sms_admin_stage_prices") if isinstance(s.get("sms_admin_stage_prices"), dict) else {}
            sms_agent_prices = s.get("sms_agent_stage_prices") if isinstance(s.get("sms_agent_stage_prices"), dict) else {}
            s["sms_price_admin"] = _sms_price_from_map(sms_admin_prices, "admin")
            s["sms_price_super"] = _sms_price_from_map(sms_admin_prices, "super_admin")
            s["sms_price_pro"] = _sms_price_from_map(sms_admin_prices, "super_professional")
            s["sms_base_price_per_number"] = _sms_main_base_price(s)
            s["sms_agent_price_normal"] = _sms_price_from_map(sms_agent_prices, "normal_agent")
            s["sms_agent_price_elite"] = _sms_price_from_map(sms_agent_prices, "elite_agent")
            s["sms_agent_price_premium"] = _sms_price_from_map(sms_agent_prices, "premium")
            s["sms_admin_base_price"] = _sms_admin_cost_for_level(s, selected_admin_level)
            if s["sms_admin_base_price"] is None and s.get("base_service_id"):
                base_sms = services_col.find_one(
                    {"_id": s.get("base_service_id")},
                    {"sms_admin_stage_prices": 1, "sms_price_per_number": 1, "sms_base_price_per_number": 1, "name": 1},
                )
                s["sms_admin_base_price"] = _sms_admin_cost_for_level(base_sms, selected_admin_level)
                if s["sms_base_price_per_number"] is None:
                    s["sms_base_price_per_number"] = _sms_main_base_price(base_sms)
        if is_social_boosting_service(s):
            s["is_social_boosting"] = True
            s["image_url"] = SOCIAL_BOOSTING_IMAGE_URL
            social_offers = s.get("services_offers") or []
            for idx, of in enumerate(social_offers, start=1):
                if not isinstance(of, dict):
                    continue
                apply_default_offer_fields(of)
                sid = offer_service_id(of) or idx
                of["offer_id"] = sid
                of["value_text"] = of.get("name") or f"Service {sid}"
                of["rate_base_usd"] = float(service_rate_per_1000(of))
                of["rate_base_ghs"] = usd_to_ghs_rate(of["rate_base_usd"])
                of["rate_base"] = of["rate_base_usd"]
                of["admin_percent_admin"] = admin_profit_percent(of, "admin")
                of["admin_percent_super"] = admin_profit_percent(of, "super_admin")
                of["admin_percent_pro"] = admin_profit_percent(of, "super_professional")
                of["admin_rate_admin_usd"] = admin_rate_per_1000(of, "admin")
                of["admin_rate_super_usd"] = admin_rate_per_1000(of, "super_admin")
                of["admin_rate_pro_usd"] = admin_rate_per_1000(of, "super_professional")
                of["selected_admin_rate_usd"] = admin_rate_per_1000(of, selected_admin_level)
                of["admin_rate_admin_ghs"] = usd_to_ghs_rate(of["admin_rate_admin_usd"])
                of["admin_rate_super_ghs"] = usd_to_ghs_rate(of["admin_rate_super_usd"])
                of["admin_rate_pro_ghs"] = usd_to_ghs_rate(of["admin_rate_pro_usd"])
                of["selected_admin_rate_ghs"] = usd_to_ghs_rate(of["selected_admin_rate_usd"])
                of["admin_rate_admin"] = of["admin_rate_admin_usd"]
                of["admin_rate_super"] = of["admin_rate_super_usd"]
                of["admin_rate_pro"] = of["admin_rate_pro_usd"]
                of["selected_admin_rate"] = of["selected_admin_rate_usd"]
                if admin_oid:
                    of["agent_percent_normal"] = agent_profit_percent(of, admin_oid, "normal_agent")
                    of["agent_percent_elite"] = agent_profit_percent(of, admin_oid, "elite_agent")
                    of["agent_percent_premium"] = agent_profit_percent(of, admin_oid, "premium")
                    of["customer_rate_normal_usd"] = customer_rate_per_1000(of, selected_admin_level, admin_oid, "Normal Agent")
                    of["customer_rate_elite_usd"] = customer_rate_per_1000(of, selected_admin_level, admin_oid, "Elite Agent")
                    of["customer_rate_premium_usd"] = customer_rate_per_1000(of, selected_admin_level, admin_oid, "Premium")
                    of["customer_rate_normal_ghs"] = usd_to_ghs_rate(of["customer_rate_normal_usd"])
                    of["customer_rate_elite_ghs"] = usd_to_ghs_rate(of["customer_rate_elite_usd"])
                    of["customer_rate_premium_ghs"] = usd_to_ghs_rate(of["customer_rate_premium_usd"])
                    of["customer_rate_normal"] = of["customer_rate_normal_usd"]
                    of["customer_rate_elite"] = of["customer_rate_elite_usd"]
                    of["customer_rate_premium"] = of["customer_rate_premium_usd"]
            s["services_offers"] = social_offers
            s["services_offers_count"] = len(social_offers)

        # compute value_text for default + store
        for key in ("offers", "store_offers"):
            if isinstance(s.get(key), list):
                for idx, of in enumerate(s[key], start=1):
                    v = of.get("value")
                    of["value_text"] = _compute_value_text_from_mtn_string(v) if isinstance(v, str) else "-"
                    of["offer_id"] = _extract_pkg_id(v) or idx

    for s in services:
        for of in (s.get("offers") or []):
            of["stage_price_normal"] = _stage_price_from_offer(of, "normal_agent")
            of["stage_price_elite"] = _stage_price_from_offer(of, "elite_agent")
            of["stage_price_premium"] = _stage_price_from_offer(of, "premium")
            of["admin_price_admin"] = _admin_stage_price_from_offer(of, "admin")
            of["admin_price_super"] = _admin_stage_price_from_offer(of, "super_admin")
            of["admin_price_pro"] = _admin_stage_price_from_offer(of, "super_professional")

        s["offer_options"] = [
            {"offer_id": str(of.get("offer_id")), "label": of.get("value_text") or of.get("value") or "-"}
            for of in (s.get("offers") or [])
        ]
        s["can_switch_provider"] = _supports_provider_switch(s, s.get("_id"))

    admin_options = []
    admin_scope_label = None
    afa_service_card = None
    if is_base and _is_main_admin():
        main_afa_settings = load_afa_settings(default_price=DEFAULT_AFA_PRICE)
        afa_service_card = {
            "mode": "main",
            "base_price": round(float(main_afa_settings.get("price") or DEFAULT_AFA_PRICE), 2),
            "price": round(float(main_afa_settings.get("price") or DEFAULT_AFA_PRICE), 2),
            "is_open": bool(main_afa_settings.get("is_open", True)),
            "in_stock": bool(main_afa_settings.get("in_stock", True)),
        }
    elif admin_oid and not is_base and not _is_main_admin():
        admin_level_afa_price = load_afa_admin_base_price(admin_oid, users_col, default=DEFAULT_AFA_PRICE)
        admin_afa_settings = load_afa_settings(admin_oid, default_price=admin_level_afa_price or DEFAULT_AFA_PRICE)
        afa_service_card = {
            "mode": "admin",
            "base_price": round(float(admin_level_afa_price or DEFAULT_AFA_PRICE), 2),
            "price": round(float(admin_afa_settings.get("price") or 0.0), 2),
            "is_open": bool(admin_afa_settings.get("is_open", True)),
            "in_stock": bool(admin_afa_settings.get("in_stock", True)),
        }
    if _is_main_admin():
        admin_docs = list(users_col.find(
            {"role": {"$in": ["admin", "main_admin"]}},
            {"_id": 1, "first_name": 1, "last_name": 1, "business_name": 1, "username": 1, "phone": 1, "email": 1, "role": 1},
        ).sort([("role", -1), ("_id", -1)]))
        admin_label_by_id = {}
        for u in admin_docs:
            oid = u["_id"]
            role = (u.get("role") or "").strip().lower()
            label = _display_name(u)
            if role == "main_admin":
                label = f"{label} (Main Admin)"
            admin_options.append({
                "id": str(oid),
                "label": label,
                "role": role,
            })
            admin_label_by_id[str(oid)] = label
        admin_scope_label = "Base services (no admin)" if is_base else (admin_label_by_id.get(str(admin_oid)) or "Selected admin")

    return render_template(
        "admin_services.html",
        services=services,
        is_main_admin=_is_main_admin(),
        admin_scope=scope_key if _is_main_admin() else "",
        admin_scope_label=admin_scope_label,
        admin_options=admin_options,
        afa_service_card=afa_service_card,
    )

@admin_services_bp.route("/admin/services/create", methods=["POST"])
def create_service():
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base and not admin_oid:
        flash("Admin context missing.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    service_name = (request.form.get("service_name") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    service_type = _norm_type(request.form.get("service_type")) or "API"

    if not service_name:
        flash("Service name is required.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if not image_url:
        flash("Please upload/select an image for the service.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = _parse_offers(request, "offers")

    # NEW: optionally copy default to store on create
    copy_default_to_store = (request.form.get("copy_default_to_store") or "").strip()
    store_offers = offers if copy_default_to_store else []

    doc = {
        "name": service_name,
        "image_url": image_url,
        "offers": offers,
        "store_offers": store_offers,  # NEW
        "type": service_type,
        "status": "OPEN",
        "availability": "AVAILABLE",
        "agent_visible": True,
        "display_enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    if not is_base and admin_oid:
        doc["admin_id"] = admin_oid

    services_col.insert_one(doc)
    flash("Service added successfully.", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/afa-registration/base", methods=["POST"])
def update_afa_base_settings():
    if not _require_admin():
        return redirect(url_for("login.login"))
    if not _is_main_admin():
        flash("Only main admin can update the AFA base price.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    price_raw = (request.form.get("price") or "").strip()
    is_open = (request.form.get("is_open") or "").strip().lower() in {"1", "true", "on", "yes", "open"}
    in_stock = (request.form.get("in_stock") or "").strip().lower() in {"1", "true", "on", "yes", "available"}
    try:
        price = round(max(0.0, float(price_raw)), 2)
    except Exception:
        flash("Enter a valid AFA base price.", "warning")
        return redirect(url_for("admin_services.manage_services", admin_scope="base"))

    now = datetime.utcnow()
    afa_settings_col.update_one(
        {"_id": SETTINGS_ID},
        {
            "$set": {
                "price": price,
                "is_open": bool(is_open),
                "in_stock": bool(in_stock),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    flash("AFA Registration base price updated.", "success")
    return redirect(url_for("admin_services.manage_services", admin_scope="base"))


@admin_services_bp.route("/admin/services/<service_id>/update", methods=["POST"])
def update_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base and not admin_oid:
        flash("Admin context missing.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    service = services_col.find_one(svc_query)
    if not service:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if not _is_main_admin():
        flash("Service details are locked. Use Customer Pricing to update default, stage, or store prices.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if is_social_boosting_service(_id) and not (_is_main_admin() and is_base):
        flash("Social Media Boosting base details can only be edited from Main Admin base services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    service_name = (request.form.get("service_name") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    service_type = _norm_type(request.form.get("service_type"))

    if not service_name:
        flash("Service name is required.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if not image_url:
        flash("Please upload/select an image for the service.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    # NEW: parse both sets
    offers = _parse_offers(request, "offers")
    offers = _merge_offer_extras(service.get("offers") or [], offers)
    store_offers = _parse_offers(request, "store_offers")

    update_doc = {
        "name": service_name,
        "image_url": image_url,
        "offers": offers,
        "store_offers": store_offers,  # NEW
        "updated_at": datetime.utcnow()
    }
    if service_type:
        update_doc["type"] = service_type

    services_col.update_one(svc_query, {"$set": update_doc})
    if is_base and _is_main_admin():
        try:
            refreshed = services_col.find_one({"_id": _id})
            if refreshed:
                reprice_admin_services_for_base(refreshed)
        except Exception:
            pass
    flash("Service updated successfully.", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

@admin_services_bp.route("/admin/services/<service_id>/delete", methods=["POST"])
def delete_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base and not admin_oid:
        flash("Admin context missing.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    if is_social_boosting_service(_id):
        flash("Social Media Boosting is a shared API service and cannot be deleted from here.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    svc = services_col.find_one(svc_query)
    res = services_col.delete_one(svc_query)

    if res.deleted_count:
        try:
            if svc and isinstance(svc.get("image_url"), str) and svc["image_url"].startswith("/uploads/"):
                _ensure_upload_folder()
                fname = svc["image_url"].replace("/uploads/", "")
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
        except Exception:
            pass
        service_profits_col.delete_many({"service_id": _id})
        flash("Service deleted.", "info")
    else:
        flash("Service not found or already deleted.", "warning")

    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

@admin_services_bp.route("/upload_service_image", methods=["POST"])
def upload_service_image():
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No file part 'image'"}), 400

    file = request.files["image"]
    if not file or file.filename.strip() == "":
        return jsonify({"success": False, "error": "No selected file"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"success": False, "error": "Invalid file type"}), 400

    _ensure_upload_folder()

    base, ext = os.path.splitext(secure_filename(file.filename))
    filename = f"{base}_{uuid.uuid4().hex[:8]}{ext.lower()}"
    target_path = os.path.join(UPLOAD_FOLDER, filename)

    file.save(target_path)
    file_url = f"/uploads/{filename}"
    return jsonify({"success": True, "url": file_url}), 200

# =======================
#   CUSTOMER PRICE OVERRIDES
# =======================
@admin_services_bp.route("/admin/services/<service_id>/price/customer", methods=["POST"])
def set_customer_price_for_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to edit customer pricing.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_owner = services_col.find_one(_apply_admin_scope({"_id": s_id}, admin_oid, is_base), {"_id": 1})
    if not svc_owner:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    customer_ids = request.form.getlist("customer_id[]")
    if not customer_ids:
        single = (request.form.get("customer_id") or "").strip()
        if single:
            customer_ids = [single]

    cust_objs: list[ObjectId] = []
    for cid in customer_ids:
        try:
            cust_objs.append(ObjectId(str(cid)))
        except Exception:
            continue
    if not cust_objs:
        flash("Invalid customer id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    valid_customers = list(
        users_col.find(
            {"_id": {"$in": cust_objs}, "role": {"$in": ["customer", "agent"]}, "admin_id": admin_oid},
            {"_id": 1},
        )
    )
    if not valid_customers:
        flash("Customer not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_id_raw = (request.form.get("offer_id") or "").strip()
    try:
        offer_id = int(float(offer_id_raw))
    except Exception:
        flash("Invalid offer id.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    price = _to_float(request.form.get("customer_price"))
    if price is None or price < 0:
        flash("Customer price must be a non-negative number.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    now = datetime.utcnow()
    updated = 0
    for c in valid_customers:
        c_id = c["_id"]
        service_offer_prices_col.update_one(
            {"service_id": s_id, "customer_id": c_id, "offer_id": offer_id},
            {"$set": {"customer_price": float(price), "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True
        )
        updated += 1

    flash(f"Customer price override updated ({updated} customer(s)).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

@admin_services_bp.route("/admin/services/<service_id>/price/customer/<customer_id>/<offer_id>/delete", methods=["POST"])
def delete_customer_price_for_service(service_id, customer_id, offer_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to edit customer pricing.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
        c_id = ObjectId(customer_id)
        offer_id_i = int(float(offer_id))
    except Exception:
        flash("Invalid id(s).", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_owner = services_col.find_one(_apply_admin_scope({"_id": s_id}, admin_oid, is_base), {"_id": 1})
    if not svc_owner:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    res = service_offer_prices_col.delete_one(
        {"service_id": s_id, "customer_id": c_id, "offer_id": offer_id_i}
    )
    if res.deleted_count:
        flash("Customer price override removed.", "info")
    else:
        flash("Override not found.", "warning")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/default/bulk", methods=["POST"])
def set_default_prices_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    flash("Default pricing has been removed. Use Stage Pricing and Store Pricing instead.", "warning")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if is_base or not admin_oid:
        flash("Select an admin scope to edit pricing.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    if is_social_boosting_service(s_id) or _is_bulk_sms_service(service_id=s_id):
        flash("Default pricing is only available for data services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if _is_afa_service(svc):
        flash("Default pricing is only available for data services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    prices = request.form.getlist("default_price[]")
    if not offer_ids:
        flash("No offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = svc.get("offers") or []
    offers_by_id = {}
    for idx, of in enumerate(offers, start=1):
        vid = _extract_pkg_id(of.get("value")) or idx
        offers_by_id[str(vid)] = of

    updated = 0
    for i in range(len(offer_ids)):
        oid_raw = (offer_ids[i] or "").strip()
        if not oid_raw:
            continue
        of = offers_by_id.get(oid_raw)
        if not of:
            continue
        price_raw = (prices[i] if i < len(prices) else "").strip()
        if price_raw == "":
            continue
        price = _to_float(price_raw)
        if price is None or price < 0:
            continue
        of["amount"] = float(price)
        updated += 1

    services_col.update_one(
        svc_query,
        {"$set": {"offers": offers, "updated_at": datetime.utcnow()}},
    )
    flash(f"Default prices updated ({updated}).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/customer/bulk", methods=["POST"])
def set_customer_prices_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to edit customer pricing.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_owner = services_col.find_one(_apply_admin_scope({"_id": s_id}, admin_oid, is_base), {"_id": 1})
    if not svc_owner:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    customer_ids = request.form.getlist("customer_id[]")
    if not customer_ids:
        single = (request.form.get("customer_id") or "").strip()
        if single:
            customer_ids = [single]

    cust_objs: list[ObjectId] = []
    for cid in customer_ids:
        try:
            cust_objs.append(ObjectId(str(cid)))
        except Exception:
            continue
    if not cust_objs:
        flash("Invalid customer id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    valid_customers = list(
        users_col.find(
            {"_id": {"$in": cust_objs}, "role": {"$in": ["customer", "agent"]}, "admin_id": admin_oid},
            {"_id": 1},
        )
    )
    if not valid_customers:
        flash("Customer not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    prices = request.form.getlist("customer_price[]")
    if not offer_ids:
        flash("No offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    now = datetime.utcnow()
    upserts = 0
    deletes = 0
    for c in valid_customers:
        c_id = c["_id"]
        for i in range(len(offer_ids)):
            oid_raw = (offer_ids[i] or "").strip()
            price_raw = (prices[i] or "").strip()
            if not oid_raw:
                continue
            try:
                offer_id = int(float(oid_raw))
            except Exception:
                continue

            if price_raw == "":
                res = service_offer_prices_col.delete_one(
                    {"service_id": s_id, "customer_id": c_id, "offer_id": offer_id}
                )
                if res.deleted_count:
                    deletes += 1
                continue

            price = _to_float(price_raw)
            if price is None or price < 0:
                continue

            service_offer_prices_col.update_one(
                {"service_id": s_id, "customer_id": c_id, "offer_id": offer_id},
                {"$set": {"customer_price": float(price), "updated_at": now},
                 "$setOnInsert": {"created_at": now}},
                upsert=True
            )
            upserts += 1

    flash(
        f"Customer prices updated for {len(valid_customers)} customer(s) "
        f"(saved {upserts}, removed {deletes}).",
        "success"
    )
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/stage/bulk", methods=["POST"])
def set_stage_prices_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base and not admin_oid:
        flash("Admin context missing.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    normal_prices = request.form.getlist("normal_price[]")
    elite_prices = request.form.getlist("elite_price[]")
    premium_prices = request.form.getlist("premium_price[]")
    if not offer_ids:
        flash("No offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = svc.get("offers") or []
    offers_by_id = {}
    for idx, of in enumerate(offers, start=1):
        vid = _extract_pkg_id(of.get("value")) or idx
        offers_by_id[str(vid)] = of

    updated = 0
    cleared = 0
    for i in range(len(offer_ids)):
        oid_raw = (offer_ids[i] or "").strip()
        if not oid_raw:
            continue

        of = offers_by_id.get(oid_raw)
        if not of:
            continue

        normal_raw = (normal_prices[i] if i < len(normal_prices) else "").strip()
        elite_raw = (elite_prices[i] if i < len(elite_prices) else "").strip()
        premium_raw = (premium_prices[i] if i < len(premium_prices) else "").strip()

        stage_prices = of.get("stage_prices") if isinstance(of.get("stage_prices"), dict) else {}
        stage_prices = dict(stage_prices)

        for key, raw_val in (
            ("normal_agent", normal_raw),
            ("elite_agent", elite_raw),
            ("premium", premium_raw),
        ):
            if raw_val == "":
                if key in stage_prices:
                    stage_prices.pop(key, None)
                    cleared += 1
                continue
            price = _to_float(raw_val)
            if price is None or price < 0:
                continue
            stage_prices[key] = float(price)
            updated += 1

        if stage_prices:
            of["stage_prices"] = stage_prices
        else:
            of.pop("stage_prices", None)

    services_col.update_one(
        svc_query,
        {"$set": {"offers": offers, "updated_at": datetime.utcnow()}},
    )
    flash(f"Stage prices updated (saved {updated}, cleared {cleared}).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/store/bulk", methods=["POST"])
def set_store_prices_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to edit store pricing.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    if is_social_boosting_service(s_id) or _is_bulk_sms_service(service_id=s_id):
        flash("Store offer pricing is only available for data services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
    if _is_afa_service(svc):
        flash("Store offer pricing is only available for data services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    amounts = request.form.getlist("amount[]")
    values = request.form.getlist("value[]")
    prices = request.form.getlist("store_customer_price[]")
    if not offer_ids:
        flash("No offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    default_offers = svc.get("offers") or []
    default_by_id = {}
    for idx, of in enumerate(default_offers, start=1):
        vid = _extract_pkg_id(of.get("value")) or idx
        default_by_id[str(vid)] = of

    store_offers = []
    saved = 0
    removed = 0
    max_rows = max(len(offer_ids), len(prices), len(amounts), len(values))
    for i in range(max_rows):
        oid_raw = (offer_ids[i] if i < len(offer_ids) else "").strip()
        if not oid_raw:
            continue

        base_offer = default_by_id.get(oid_raw) or {}
        amount_raw = (amounts[i] if i < len(amounts) else "")
        value_raw = (values[i] if i < len(values) else "") or (base_offer.get("value") or "")
        price_raw = (prices[i] if i < len(prices) else "").strip()

        amount = _to_float(amount_raw)
        if amount is None:
            amount = _to_float(base_offer.get("amount"))
        value_str = (value_raw or "").strip() or (base_offer.get("value") or "")

        store_offer_row = {
            "offer_id": oid_raw,
            "amount": amount,
            "value": value_str,
            "profit": None,
        }

        if price_raw == "":
            store_offer_row["customer_price"] = None
            store_offers.append(store_offer_row)
            removed += 1
            continue
        price = _to_float(price_raw)
        if price is None or price < 0:
            continue

        store_offer_row["customer_price"] = float(price)
        store_offers.append(store_offer_row)
        saved += 1

    services_col.update_one(
        svc_query,
        {"$set": {"store_offers": store_offers, "updated_at": datetime.utcnow()}},
    )
    flash(f"Store prices updated (saved {saved}, cleared {removed}).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/base-amount/bulk", methods=["POST"])
def set_base_amounts_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    if not _is_main_admin():
        flash("Only main admin can edit base amounts.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base:
        flash("Base amounts can only be edited from base services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    if is_social_boosting_service(s_id) or _is_bulk_sms_service(service_id=s_id):
        flash("Base amount editing is only available for data services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    delete_offer_id = (request.form.get("delete_offer_id") or "").strip()
    if delete_offer_id:
        offers, removed = _drop_offer_by_id(svc.get("offers") or [], delete_offer_id)
        store_offers, _ = _drop_offer_by_id(svc.get("store_offers") or [], delete_offer_id)
        if not removed:
            flash("Offer not found.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

        now = datetime.utcnow()
        services_col.update_one(
            svc_query,
            {"$set": {"offers": offers, "store_offers": store_offers, "updated_at": now}},
        )

        if isinstance(s_id, ObjectId):
            for child in services_col.find({"base_service_id": s_id}, {"_id": 1, "offers": 1, "store_offers": 1}):
                child_offers, child_removed = _drop_offer_by_id(child.get("offers") or [], delete_offer_id)
                child_store_offers, child_store_removed = _drop_offer_by_id(child.get("store_offers") or [], delete_offer_id)
                if child_removed or child_store_removed:
                    services_col.update_one(
                        {"_id": child["_id"]},
                        {"$set": {"offers": child_offers, "store_offers": child_store_offers, "updated_at": now}},
                    )

        try:
            service_offer_prices_col.delete_many({"service_id": s_id, "offer_id": int(float(delete_offer_id))})
        except Exception:
            pass

        flash("Offer deleted successfully.", "success")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    amounts = request.form.getlist("base_amount[]")
    if not offer_ids or not amounts:
        flash("No base amounts provided.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = svc.get("offers") or []
    offers_by_id = {}
    for idx, of in enumerate(offers, start=1):
        vid = _extract_pkg_id(of.get("value")) or idx
        offers_by_id[str(vid)] = of

    updated = 0
    for i in range(min(len(offer_ids), len(amounts))):
        oid_raw = (offer_ids[i] or "").strip()
        amount_raw = (amounts[i] or "").strip()
        if not oid_raw or amount_raw == "":
            continue
        amount = _to_float(amount_raw)
        if amount is None or amount < 0:
            continue
        of = offers_by_id.get(oid_raw)
        if not of:
            continue
        of["amount"] = float(amount)
        updated += 1

    services_col.update_one(
        svc_query,
        {"$set": {"offers": offers, "updated_at": datetime.utcnow()}},
    )
    try:
        refreshed = services_col.find_one({"_id": s_id})
        if refreshed:
            reprice_admin_services_for_base(refreshed)
    except Exception:
        pass

    flash(f"Base amounts updated ({updated} offers).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/admin-stage/bulk", methods=["POST"])
def set_admin_stage_prices_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    if not _is_main_admin():
        flash("Only main admin can set admin stage prices.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base:
        flash("Admin stage prices can only be set on base services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc:
        flash("Service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_id[]")
    admin_prices = request.form.getlist("admin_price[]")
    super_prices = request.form.getlist("super_admin_price[]")
    pro_prices = request.form.getlist("super_professional_price[]")
    if not offer_ids:
        flash("No offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = svc.get("offers") or []
    offers_by_id = {}
    for idx, of in enumerate(offers, start=1):
        vid = _extract_pkg_id(of.get("value")) or idx
        offers_by_id[str(vid)] = of

    updated = 0
    cleared = 0
    for i in range(len(offer_ids)):
        oid_raw = (offer_ids[i] or "").strip()
        if not oid_raw:
            continue

        of = offers_by_id.get(oid_raw)
        if not of:
            continue

        admin_raw = (admin_prices[i] if i < len(admin_prices) else "").strip()
        super_raw = (super_prices[i] if i < len(super_prices) else "").strip()
        pro_raw = (pro_prices[i] if i < len(pro_prices) else "").strip()

        stage_prices = of.get("admin_stage_prices") if isinstance(of.get("admin_stage_prices"), dict) else {}
        stage_prices = dict(stage_prices)

        for key, raw_val in (
            ("admin", admin_raw),
            ("super_admin", super_raw),
            ("super_professional", pro_raw),
        ):
            if raw_val == "":
                if key in stage_prices:
                    stage_prices.pop(key, None)
                    cleared += 1
                continue
            price = _to_float(raw_val)
            if price is None or price < 0:
                continue
            stage_prices[key] = float(price)
            updated += 1

        if stage_prices:
            of["admin_stage_prices"] = stage_prices
        else:
            of.pop("admin_stage_prices", None)

    services_col.update_one(
        svc_query,
        {"$set": {"offers": offers, "updated_at": datetime.utcnow()}},
    )

    try:
        refreshed = services_col.find_one({"_id": s_id})
        if refreshed:
            reprice_admin_services_for_base(refreshed)
    except Exception:
        pass

    flash(f"Admin stage prices updated (saved {updated}, cleared {cleared}).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/bulk-sms/admin-stage", methods=["POST"])
def set_bulk_sms_admin_stage_prices(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    if not _is_main_admin():
        flash("Only main admin can set Bulk SMS admin prices.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base:
        flash("Bulk SMS admin prices must be set from Base services.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query, {"_id": 1, "name": 1})
    if not svc or not _is_bulk_sms_service(svc, s_id):
        flash("Bulk SMS service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    raw_values = {
        "admin": (request.form.get("admin_price") or "").strip(),
        "super_admin": (request.form.get("super_admin_price") or "").strip(),
        "super_professional": (request.form.get("super_professional_price") or "").strip(),
    }
    raw_base_price = (request.form.get("base_price_per_number") or "").strip()
    base_price = None
    if raw_base_price != "":
        base_price = _to_float(raw_base_price)
        if base_price is None or base_price < 0:
            flash("SMS base price per number must be a non-negative number.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    prices = {}
    for key, raw in raw_values.items():
        if raw == "":
            continue
        price = _to_float(raw)
        if price is None or price < 0:
            flash("SMS price per number must be a non-negative number.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
        if base_price is not None and price < base_price:
            flash("SMS admin prices cannot be below the base price per number.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
        prices[key] = float(price)

    now = datetime.utcnow()
    update_doc = {
        "sms_admin_stage_prices": prices,
        "sms_price_per_number": prices.get("admin"),
        "updated_at": now,
    }
    if base_price is not None:
        update_doc["sms_base_price_per_number"] = float(base_price)
    services_col.update_one(svc_query, {"$set": update_doc})
    _propagate_to_admin_copies(s_id, update_doc)

    flash("Bulk SMS admin prices updated.", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/price/bulk-sms/agent-stage", methods=["POST"])
def set_bulk_sms_agent_stage_prices(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to set Bulk SMS agent prices.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    try:
        s_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc_query = _apply_admin_scope({"_id": s_id}, admin_oid, is_base)
    svc = services_col.find_one(svc_query)
    if not svc or not _is_bulk_sms_service(svc, s_id):
        flash("Bulk SMS service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    admin_doc = users_col.find_one({"_id": admin_oid}, {"admin_level": 1}) or {}
    admin_level = normalize_admin_level(admin_doc.get("admin_level"))
    admin_cost = _sms_admin_cost_for_level(svc, admin_level)
    if admin_cost is None and svc.get("base_service_id"):
        base_sms = services_col.find_one(
            {"_id": svc.get("base_service_id")},
            {"sms_admin_stage_prices": 1, "sms_price_per_number": 1, "name": 1},
        )
        admin_cost = _sms_admin_cost_for_level(base_sms, admin_level)

    raw_values = {
        "normal_agent": (request.form.get("normal_price") or "").strip(),
        "elite_agent": (request.form.get("elite_price") or "").strip(),
        "premium": (request.form.get("premium_price") or "").strip(),
    }
    prices = {}
    for key, raw in raw_values.items():
        if raw == "":
            continue
        price = _to_float(raw)
        if price is None or price < 0:
            flash("SMS price per number must be a non-negative number.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
        if admin_cost is not None and price < admin_cost:
            flash("Agent SMS prices cannot be below your admin SMS cost.", "warning")
            return redirect(url_for("admin_services.manage_services", **redirect_kwargs))
        prices[key] = float(price)

    services_col.update_one(
        svc_query,
        {"$set": {"sms_agent_stage_prices": prices, "updated_at": datetime.utcnow()}},
    )

    flash("Bulk SMS agent prices updated.", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/boosting/admin-profit/bulk", methods=["POST"])
def set_boosting_admin_profit_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    if not _is_main_admin():
        flash("Only main admin can set Social Media Boosting admin percentages.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if not is_base:
        flash("Select Base services to edit admin profit percentages.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    if not is_social_boosting_service(service_id):
        flash("This pricing screen is only for Social Media Boosting.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc = services_col.find_one({"_id": SOCIAL_BOOSTING_SERVICE_ID}, {"services_offers": 1})
    if not svc:
        flash("Social Media Boosting service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_service_id[]")
    admin_pcts = request.form.getlist("admin_percent[]")
    super_pcts = request.form.getlist("super_admin_percent[]")
    pro_pcts = request.form.getlist("super_professional_percent[]")
    if not offer_ids:
        flash("No Social Media Boosting offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offers = svc.get("services_offers") or []
    offers_by_id = {
        str(offer_service_id(of)): of
        for of in offers
        if isinstance(of, dict) and offer_service_id(of) is not None
    }

    updated = 0
    for i, raw_offer_id in enumerate(offer_ids):
        of = offers_by_id.get(str(raw_offer_id).strip())
        if not of:
            continue
        apply_default_offer_fields(of)
        of["admin_profit_percent"] = percent_value(admin_pcts[i] if i < len(admin_pcts) else 0)
        of["super_admin_profit_percent"] = percent_value(super_pcts[i] if i < len(super_pcts) else 0)
        of["super_professional_profit_percent"] = percent_value(pro_pcts[i] if i < len(pro_pcts) else 0)
        updated += 1

    now = datetime.utcnow()
    services_col.update_one(
        {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        {
            "$set": {
                "services_offers": offers,
                "services_offers_count": len(offers),
                "image_url": SOCIAL_BOOSTING_IMAGE_URL,
                "updated_at": now,
            }
        },
    )
    flash(f"Social Media Boosting admin percentages updated ({updated} offers).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/boosting/agent-profit/bulk", methods=["POST"])
def set_boosting_agent_profit_bulk(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    admin_oid, is_base, scope_key = _parse_admin_scope_from_request(request)
    redirect_kwargs = {"admin_scope": scope_key} if _is_main_admin() else {}
    if is_base or not admin_oid:
        flash("Select an admin scope to edit agent percentages.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    if not is_social_boosting_service(service_id):
        flash("This pricing screen is only for Social Media Boosting.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    svc = services_col.find_one({"_id": SOCIAL_BOOSTING_SERVICE_ID}, {"services_offers": 1})
    if not svc:
        flash("Social Media Boosting service not found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    offer_ids = request.form.getlist("offer_service_id[]")
    normal_pcts = request.form.getlist("normal_percent[]")
    elite_pcts = request.form.getlist("elite_percent[]")
    premium_pcts = request.form.getlist("premium_percent[]")
    if not offer_ids:
        flash("No Social Media Boosting offers found.", "warning")
        return redirect(url_for("admin_services.manage_services", **redirect_kwargs))

    admin_key = str(admin_oid)
    offers = svc.get("services_offers") or []
    offers_by_id = {
        str(offer_service_id(of)): of
        for of in offers
        if isinstance(of, dict) and offer_service_id(of) is not None
    }

    updated = 0
    for i, raw_offer_id in enumerate(offer_ids):
        of = offers_by_id.get(str(raw_offer_id).strip())
        if not of:
            continue
        apply_default_offer_fields(of)
        by_admin = of.get("agent_profit_percentages_by_admin")
        if not isinstance(by_admin, dict):
            by_admin = {}
        row = by_admin.get(admin_key)
        if not isinstance(row, dict):
            row = {}
        row["normal_agent_profit_percent"] = percent_value(normal_pcts[i] if i < len(normal_pcts) else 0)
        row["elite_agent_profit_percent"] = percent_value(elite_pcts[i] if i < len(elite_pcts) else 0)
        row["premium_profit_percent"] = percent_value(premium_pcts[i] if i < len(premium_pcts) else 0)
        by_admin[admin_key] = row
        of["agent_profit_percentages_by_admin"] = by_admin
        updated += 1

    now = datetime.utcnow()
    services_col.update_one(
        {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        {
            "$set": {
                "services_offers": offers,
                "services_offers_count": len(offers),
                "image_url": SOCIAL_BOOSTING_IMAGE_URL,
                "updated_at": now,
            }
        },
    )
    flash(f"Social Media Boosting agent percentages updated ({updated} offers).", "success")
    return redirect(url_for("admin_services.manage_services", **redirect_kwargs))


@admin_services_bp.route("/admin/services/<service_id>/type", methods=["POST"])
def set_service_type(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not _is_main_admin():
        return jsonify({"success": False, "error": "Only main admin can change service type."}), 403
    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if not is_base and not admin_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400
    if is_social_boosting_service(_id) and not (_is_main_admin() and is_base):
        return jsonify({"success": False, "error": "Only main admin can update Social Media Boosting globally."}), 403

    desired_raw = request.form.get("type")
    if desired_raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        desired_raw = payload.get("type")
    desired = _norm_type(desired_raw)

    if not desired:
        return jsonify({"success": False, "error": "type must be 'API' or 'OFF'"}), 400

    scope_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    now = datetime.utcnow()
    update_doc = {"type": desired, "updated_at": now}
    res = services_col.update_one(scope_query, {"$set": update_doc})
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if is_base:
        _propagate_to_admin_copies(_id, update_doc)

    return jsonify({
        "success": True,
        "service_id": str(_id),
        "type": desired
    })

@admin_services_bp.route("/admin/services/<service_id>/visibility", methods=["POST"])
def set_service_agent_visibility(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if _is_main_admin():
        return jsonify({"success": False, "error": "Only sub admins can change agent visibility."}), 403

    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if is_base or not admin_oid:
        return jsonify({"success": False, "error": "Admin context missing."}), 401

    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("visible")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("visible")

    if isinstance(raw, bool):
        visible = raw
    else:
        visible = str(raw).strip().lower() in {"1", "true", "on", "yes", "visible"}

    service = services_col.find_one(
        _apply_admin_scope({"_id": _id}, admin_oid, False),
        {"_id": 1, "admin_id": 1},
    )
    if not service:
        return jsonify({"success": False, "error": "Service not found"}), 404

    now = datetime.utcnow()
    update_doc = {
        f"agent_visibility_by_admin.{str(admin_oid)}": visible,
        "updated_at": now,
    }
    owner = service.get("admin_id")
    if owner == admin_oid or str(owner) == str(admin_oid):
        update_doc["agent_visible"] = visible

    services_col.update_one({"_id": _id}, {"$set": update_doc})

    return jsonify({
        "success": True,
        "service_id": str(_id),
        "visible": visible,
    })


@admin_services_bp.route("/admin/services/<service_id>/display", methods=["POST"])
def set_service_display(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not _is_main_admin():
        return jsonify({"success": False, "error": "Only main admin can change display."}), 403

    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if not is_base and not admin_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("display")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("display")

    if isinstance(raw, bool):
        display_enabled = raw
    else:
        display_enabled = str(raw).strip().lower() in {"1", "true", "on", "yes", "display", "enabled"}

    scope_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    now = datetime.utcnow()
    update_doc = {"display_enabled": display_enabled, "updated_at": now}
    res = services_col.update_one(scope_query, {"$set": update_doc})
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if is_base:
        _propagate_to_admin_copies(_id, update_doc)

    return jsonify({
        "success": True,
        "service_id": str(_id),
        "display_enabled": display_enabled,
    })

def _norm_status_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"open", "1", "true", "on", "yes"}:
        return "OPEN"
    if s in {"closed", "0", "false", "off", "no"}:
        return "CLOSED"
    return None

def _norm_availability_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"available", "in_stock", "instock", "1", "true", "on", "yes"}:
        return "AVAILABLE"
    if s in {"out_of_stock", "outofstock", "oos", "unavailable", "0", "false", "off", "no"}:
        return "OUT_OF_STOCK"
    return None

@admin_services_bp.route("/admin/services/<service_id>/status", methods=["POST"])
def set_service_status(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if not is_base and not admin_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400
    if is_social_boosting_service(_id) and not (_is_main_admin() and is_base):
        return jsonify({"success": False, "error": "Only main admin can update Social Media Boosting globally."}), 403

    raw = request.form.get("status")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("status")

    status_val = _norm_status_flag(raw)
    if not status_val:
        return jsonify({"success": False, "error": "status must be 'OPEN' or 'CLOSED'"}), 400

    scope_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    now = datetime.utcnow()
    update_doc = {"status": status_val, "updated_at": now}
    res = services_col.update_one(scope_query, {"$set": update_doc})
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if is_base:
        _propagate_to_admin_copies(_id, update_doc)

    return jsonify({
        "success": True,
        "service_id": str(_id),
        "status": status_val
    })

@admin_services_bp.route("/admin/services/<service_id>/availability", methods=["POST"])
def set_service_availability(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if not is_base and not admin_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400
    if is_social_boosting_service(_id) and not (_is_main_admin() and is_base):
        return jsonify({"success": False, "error": "Only main admin can update Social Media Boosting globally."}), 403

    raw = request.form.get("availability")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("availability")

    avail_val = _norm_availability_flag(raw)
    if not avail_val:
        return jsonify({"success": False, "error": "availability must be 'AVAILABLE' or 'OUT_OF_STOCK'"}), 400

    scope_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    now = datetime.utcnow()
    update_doc = {"availability": avail_val, "updated_at": now}
    res = services_col.update_one(scope_query, {"$set": update_doc})
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if is_base:
        _propagate_to_admin_copies(_id, update_doc)

    return jsonify({
        "success": True,
        "service_id": str(_id),
        "availability": avail_val
    })


@admin_services_bp.route("/admin/services/<service_id>/provider", methods=["POST"])
def set_service_provider(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not _is_main_admin():
        return jsonify({"success": False, "error": "Only main admin can change provider."}), 403
    admin_oid, is_base, _ = _parse_admin_scope_from_request(request)
    if not is_base and not admin_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or "").strip().lower()
    if provider not in {"portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"}:
        return jsonify(
            {
                "success": False,
                "error": "provider must be 'portal02', 'dataconnect', 'codecraft', 'datakazina', 'skplug', or 'bundleportal'",
            }
        ), 400

    scope_query = _apply_admin_scope({"_id": _id}, admin_oid, is_base)
    service = services_col.find_one(
        scope_query,
        {"name": 1, "type": 1, "service_network": 1, "network": 1},
    )
    if not service:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if not _supports_provider_switch(service, _id):
        return jsonify({"success": False, "error": "Provider switch is only available for data services."}), 400

    svc_type = (service.get("type") or "").strip().upper()
    if svc_type not in {"ON", "API"}:
        return jsonify(
            {
                "success": False,
                "error": "Service type must be 'ON' or 'API' to enable provider routing",
            }
        ), 400

    now = datetime.utcnow()
    update_doc = {
        "provider": provider,
        "updated_at": now,
        "mtn_normal_use_portal02": True if provider == "portal02" else False,
        "mtn_express_use_portal02": True if provider == "portal02" else False,
    }

    services_col.update_one(scope_query, {"$set": update_doc})

    if is_base:
        _propagate_to_admin_copies(_id, update_doc)

    return jsonify({
        "success": True,
        "provider": provider
    })
