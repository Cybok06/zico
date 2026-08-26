# routes/store_page.py
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
import os, json, re, ast, traceback, uuid

import requests
from bson import ObjectId
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
    abort,
)

from agent_code_utils import get_or_create_agent_code_for_user
from db import db
from activity_log import log_activity
from complaint_admin_override import verify_admin_override_token
import gridfs
from order_display import build_order_display_items
from tenant import resolve_admin_id_for_user_id, current_admin_id_from_session, is_admin_role
from paystack_keys import get_paystack_key_pair
from checker_pricing import admin_stage_price, checker_base_cost, customer_stage_price, get_checker_pricing_doc, normalize_checker_type
from sms_sender import normalize_ghana_sms_phone, resolve_admin_sender_name, send_sms
from admin_paystack_ledger import evaluate_admin_wallet_low_balance, record_admin_paystack_credit
from profit_ledger import apply_profit_split, money as ledger_money, normalize_profit_line, profit_totals
from wallet_ledger import WALLET_OVERDRAFT_LIMIT_MESSAGE, debit_wallets_for_order
from order_sms_notifications import send_mtn_mashup_order_sms
from bulk_sms import (
    BULK_SMS_SERVICE_ID,
    SMS_DISCLAIMER_TEXT,
    bulk_sms_deliveries_col,
    dispatch_bulk_sms_delivery,
    find_bulk_sms_service_for_admin,
    sms_price_for_user,
    validate_sms_message_body,
)
from afa_settings_utils import load_afa_admin_base_price, load_afa_base_price, load_afa_price
from social_boosting_pricing import (
    SOCIAL_BOOSTING_IMAGE_URL,
    SOCIAL_BOOSTING_NAME,
    SOCIAL_BOOSTING_PROVIDER,
    SOCIAL_BOOSTING_SERVICE_ID,
    admin_rate_per_1000,
    apply_default_offer_fields,
    custom_comments_text,
    customer_rate_per_1000,
    find_offer as find_social_offer,
    is_social_boosting_service,
    normalize_admin_level,
    normalize_custom_comments,
    offer_requires_custom_comments,
    offer_service_id,
    rate_money,
    service_rate_per_1000,
    total_for_quantity,
    total_for_quantity_ghs,
    usd_to_ghs_rate,
)


# ---------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------
services_col = db["services"]
stores_col = db["stores"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
orders_col = db["orders"]
transactions_col = db["transactions"]
users_col = db["users"]
store_accounts_col = db["store_accounts"]
complaints_col = db["complaints"]
audit_paystack = db["audit_paystack"]
afa_col = db["afa_registrations"]
afa_settings_col = db["afa_settings"]
checker_stock_col = db["wassce_checker"]
purchase_history_col = db["purchase_history"]
auth_pages_col = db["auth_pages"]
COMPLAINT_CLOSED_STATUSES = {"resolved", "refund", "false", "rejected"}

# ✅ PRIMARY: Store products collection used by /api/store-products/*
store_products_col = db["store_products"]

# ✅ Legacy products collection (optional fallback)
products_col = db.get_collection("products")

# --- GridFS bucket ---
fs = gridfs.GridFS(db)

stores_bp = Blueprint("stores", __name__)
AFA_SETTINGS_ID = "AFA_SETTINGS"
PAYSTACK_INLINE_FEE_RATE = 0.005


# ---------------------------------------------------------------------
# Import helpers from checkout.py (keep compatibility)
# ---------------------------------------------------------------------
_checkout_helpers: Dict[str, Any] = {}
try:
    from checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _extract_ported_fields,
        _to_float,
        _money,
        generate_order_id,
        _split_order_documents,
        _service_unavailability_reason,
        _resolve_network_id,
        _resolve_dataconnect_network,
        _resolve_codecraft_network_name,
        _resolve_bundleportal_network_name,
        _normalize_bundleportal_phone,
        _resolve_skplug_network_name,
        _resolve_package_size_gb,
        _resolve_datakazina_shared_bundle,
        _is_mtn_normal_service,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _codecraft_get_packages_cached,
        _codecraft_submit_regular,
        _background_process_providers,
        PORTAL02_OFFER_SLUG_MTN_NORMAL,
        jlog,
    )
    try:
        from checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass
except Exception:  # pragma: no cover
    from .checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _extract_ported_fields,
        _to_float,
        _money,
        generate_order_id,
        _split_order_documents,
        _service_unavailability_reason,
        _resolve_network_id,
        _resolve_dataconnect_network,
        _resolve_codecraft_network_name,
        _resolve_bundleportal_network_name,
        _normalize_bundleportal_phone,
        _resolve_skplug_network_name,
        _resolve_package_size_gb,
        _resolve_datakazina_shared_bundle,
        _is_mtn_normal_service,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _codecraft_get_packages_cached,
        _codecraft_submit_regular,
        _background_process_providers,
        PORTAL02_OFFER_SLUG_MTN_NORMAL,
        jlog,
    )
    try:
        from .checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from .checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass


def _clear_dashboard_cache_safely():
    try:
        from admin_dashboard import clear_dashboard_cache

        clear_dashboard_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------
# Config (ENV)
# ---------------------------------------------------------------------
def _clean_key(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""

def _is_pk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("pk_")

def _is_sk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("sk_")

def _load_store_paystack_keys(admin_id: ObjectId | None = None) -> Tuple[str, str]:
    pk, sk = get_paystack_key_pair("store", admin_id=admin_id)
    pk = _clean_key(pk)
    sk = _clean_key(sk)
    if _is_sk(pk) and _is_pk(sk):
        pk, sk = sk, pk
    if not _is_pk(pk) and _is_pk(sk):
        pk = sk
    if not _is_sk(sk) and _is_sk(pk):
        sk = pk
    return pk, sk


TARGET_STORE_HOST: str = os.getenv("STORE_PUBLIC_HOST", "nagmart.store")
STORE_PATH_PREFIXES: Tuple[str, ...] = ("/s/",)

NETWORK_ID_FALLBACK: Dict[str, int] = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}

PORTED_PREFIXES: Dict[str, List[str]] = {
    "mtn": ["025", "024", "059", "055", "054", "053"],
    "telecel": ["020", "050"],
    "airteltigo": ["057", "056", "027", "026"],
}


# ---------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _normalize_complaint_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if digits.startswith("0") and len(digits) == 10:
        return "233" + digits[1:]
    if digits.startswith("233") and len(digits) == 12:
        return digits
    return digits or str(raw or "").strip().lower()

def _complaint_phone_variants(raw: Any) -> List[str]:
    raw_s = str(raw or "").strip()
    norm = _normalize_complaint_phone(raw_s)
    variants = {raw_s, norm}
    if norm.startswith("233") and len(norm) == 12:
        variants.add("0" + norm[3:])
        variants.add("+" + norm)
    return [v for v in variants if v]

def _active_store_complaint_query(slug: str, phone: str, paystack_ref: str) -> Dict[str, Any]:
    duplicate_terms: List[Dict[str, Any]] = []
    for phone_variant in _complaint_phone_variants(phone):
        duplicate_terms.append({"customer_phone": phone_variant})
        duplicate_terms.append({"customer_phone_norm": phone_variant})
    if paystack_ref:
        duplicate_terms.append({"paystack_reference": paystack_ref})
        duplicate_terms.append({"paystack_reference_norm": paystack_ref.lower()})

    return {
        "store_slug": slug,
        "status": {"$nin": list(COMPLAINT_CLOSED_STATUSES)},
        "$or": duplicate_terms or [{"customer_phone": phone}],
    }

def _host_is_store_domain(host: str) -> bool:
    host_only = (host or "").split(":", 1)[0].strip().lower()
    base = (TARGET_STORE_HOST or "").strip().lower()
    if not base:
        return False
    return host_only in (base, f"www.{base}")

def _store_admin_id(store_doc: Dict[str, Any]) -> Optional[ObjectId]:
    if not store_doc:
        return None
    return store_doc.get("admin_id") or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id"))

def _slugify(s: str) -> str:
    s2 = (s or "").lower().strip()
    s2 = re.sub(r"[^a-z0-9]+", "-", s2).strip("-")
    return s2 or "store"

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = svc.get("closed_message") or "This service is temporarily closed."
    oos_msg = svc.get("out_of_stock_message") or "This service is currently out of stock."
    can_order = t in {"API", "OFF", "MANUAL"} and status == "OPEN" and availability == "AVAILABLE"
    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        else:
            disabled_reason = "This service is currently unavailable."
    return {
        "type": t,
        "status": status,
        "availability": availability,
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _sorted_services(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def prio_tuple(s: Dict[str, Any]) -> Tuple[float, float, float, str]:
        prio = _to_float(s.get("priority")) or float("inf")
        display_order = _to_float(s.get("display_order")) or float("inf")
        created = s.get("created_at")
        ts = 0.0
        if isinstance(created, datetime):
            ts = -created.timestamp()
        else:
            try:
                v = float(created)
                ts = -(v / 1000.0 if v > 1e12 else v)
            except Exception:
                ts = 0.0
        alpha = _norm(s.get("name") or "")
        return (prio, display_order, ts, alpha)

    raw.sort(key=prio_tuple)
    return raw


# ---------------------------------------------------------------------
# ✅ WhatsApp helpers
# ---------------------------------------------------------------------
def _wa_digits(v: Any) -> str:
    d = re.sub(r"\D+", "", str(v or ""))
    if d.startswith("0") and len(d) == 10:
        return "233" + d[1:]
    if d.startswith("233") and len(d) == 12:
        return d
    return d

def _wa_link_from_number(raw: Any, text: str = "") -> str:
    d = _wa_digits(raw)
    if not d:
        return ""
    msg = (text or "").strip()
    if msg:
        try:
            from urllib.parse import quote
            return f"https://wa.me/{d}?text={quote(msg)}"
        except Exception:
            return f"https://wa.me/{d}"
    return f"https://wa.me/{d}"

def _extract_store_whatsapp(store_doc: Dict[str, Any]) -> Dict[str, str]:
    def pick(*paths) -> Any:
        for p in paths:
            cur = store_doc
            ok = True
            for key in p:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur.get(key)
            if ok and cur not in (None, "", [], {}):
                return cur
        return ""

    wa_number = pick(
        ("whatsapp_number",),
        ("contact", "whatsapp_number"),
        ("hero", "whatsapp_number"),
        ("theme", "whatsapp_number"),
        ("whatsapp", "number"),
    )
    wa_group = pick(
        ("whatsapp_group",),
        ("contact", "whatsapp_group"),
        ("hero", "whatsapp_group"),
        ("theme", "whatsapp_group"),
        ("whatsapp", "group"),
        ("whatsapp_group_link",),
        ("contact", "whatsapp_group_link"),
    )

    wa_number_str = str(wa_number or "").strip()
    wa_group_str = str(wa_group or "").strip()

    return {
        "number_raw": wa_number_str,
        "number_digits": _wa_digits(wa_number_str),
        "number_link": _wa_link_from_number(
            wa_number_str, f"Hello {store_doc.get('name','')}, I want to order."
        ),
        "group_link": wa_group_str,
    }


# =====================================================================
# ✅ Offers source:
# - Page pricing: merge store_offers with default offers
# =====================================================================
def _offer_merge_key(of: Dict[str, Any], idx: int) -> str:
    parsed = _parse_value_field((of or {}).get("value"))
    if isinstance(parsed, dict) and parsed.get("volume") not in (None, ""):
        return f"volume:{parsed.get('volume')}"
    if isinstance(parsed, dict) and parsed.get("id") not in (None, ""):
        return f"id:{parsed.get('id')}"
    raw_value = (of or {}).get("value")
    if raw_value not in (None, ""):
        return f"value:{str(raw_value).strip()}"
    return f"idx:{idx}"


def _svc_offers_list(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    default_offers = svc.get("offers") if isinstance(svc.get("offers"), list) else []
    store_offers = svc.get("store_offers") if isinstance(svc.get("store_offers"), list) else []
    if not store_offers:
        return default_offers or []
    if not default_offers:
        return store_offers or []

    store_map: Dict[str, Dict[str, Any]] = {}
    for idx, of in enumerate(store_offers, start=1):
        if isinstance(of, dict):
            store_map[_offer_merge_key(of, idx)] = of

    merged: List[Dict[str, Any]] = []
    used_keys = set()
    for idx, of in enumerate(default_offers, start=1):
        key = _offer_merge_key(of, idx)
        override = store_map.get(key)
        row = dict(of or {})
        if isinstance(override, dict):
            for field in ("customer_price", "store_amount", "value_text", "value"):
                if field in override:
                    row[field] = override.get(field)
            if "amount" in override and "store_amount" not in override:
                row["store_amount"] = override.get("amount")
            used_keys.add(key)
        merged.append(row)

    for idx, of in enumerate(store_offers, start=1):
        key = _offer_merge_key(of, idx)
        if key in used_keys:
            continue
        if isinstance(of, dict):
            merged.append(dict(of))

    return merged

def _offer_base_amount(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    base = _to_float(of.get("customer_price"))
    if base is not None:
        return base
    v = of.get("store_amount")
    base = _to_float(v)
    if base is not None:
        return base
    return _to_float(of.get("amount"))


def _is_social_boosting_cart_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if is_social_boosting_service(item.get("serviceId") or item.get("service_id")):
        return True
    value_obj = item.get("value_obj")
    if value_obj in (None, "", [], {}):
        value_obj = item.get("valueObj")
    return (
        str(item.get("provider") or "").strip().lower() == SOCIAL_BOOSTING_PROVIDER
        or (isinstance(value_obj, dict) and bool(value_obj.get("social_boosting")))
        or bool(str(item.get("target_link") or "").strip())
    )


def _social_boosting_actor_context(actor_user_id: Any, admin_oid: Optional[ObjectId]) -> Tuple[str, str]:
    stage_label = ((_lookup_user_any_status(actor_user_id) or {}).get("stage_label") or "Normal Agent").strip() or "Normal Agent"
    admin_level = "admin"
    if admin_oid:
        try:
            admin_doc = users_col.find_one({"_id": admin_oid}, {"admin_level": 1}) or {}
            admin_level = normalize_admin_level(admin_doc.get("admin_level"))
        except Exception:
            admin_level = "admin"
    return admin_level, stage_label


def _social_boosting_owner_rate_per_1000(
    offer: Dict[str, Any],
    admin_level: str,
    admin_oid: Optional[ObjectId],
    actor_user_id: Any,
    stage_label: str,
) -> float:
    actor = _lookup_user_any_status(actor_user_id)
    if is_admin_role((actor or {}).get("role")):
        return admin_rate_per_1000(offer, admin_level)
    return customer_rate_per_1000(offer, admin_level, admin_oid, stage_label)


def _pricing_percent_for_service(
    service_id: Any,
    percent_default: float,
    per_service_map: Dict[str, Dict[str, Any]],
) -> float:
    svc_id_str = str(service_id or "")
    per_entry = per_service_map.get(svc_id_str, {}) if svc_id_str else {}
    svc_percent = per_entry.get("percent")
    if svc_percent is None:
        return 0.0
    try:
        return float(svc_percent)
    except Exception:
        return 0.0


def _resolve_social_boosting_request(
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    item: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[int], int, Optional[Dict[str, Any]], Dict[str, Any]]:
    social_value = value_obj if isinstance(value_obj, dict) else {}
    item = item or {}
    provider_service_id = (
        social_value.get("provider_service_id")
        or social_value.get("service")
        or item.get("provider_service_id")
        or item.get("offer_id")
    )
    quantity_raw = social_value.get("quantity") or item.get("quantity")
    try:
        provider_service_id_int = int(float(provider_service_id))
        quantity = int(float(quantity_raw))
    except Exception:
        provider_service_id_int = None
        quantity = 0
    social_offer = None
    if provider_service_id_int is not None:
        social_offer = find_social_offer((svc_doc or {}).get("services_offers") or [], provider_service_id_int)
    return provider_service_id_int, quantity, social_offer, social_value


def _normalize_social_boosting_service_for_store(
    svc: Dict[str, Any],
    *,
    admin_oid: Optional[ObjectId],
    actor_user_id: Any = None,
    owner_stage_label: str = "Normal Agent",
    store_markup_percent: float = 0.0,
) -> Dict[str, Any]:
    s = dict(svc or {})
    s["_id_str"] = str(s.get("_id") or s.get("_id_str") or "")
    s["is_social_boosting"] = True
    s["display_name"] = "Boosting"
    s["image_url"] = SOCIAL_BOOSTING_IMAGE_URL
    s["unit"] = "social"
    s["offers_source"] = "services_offers"
    s["supports_percent_only"] = True

    markup_percent = max(0.0, float(store_markup_percent or 0.0))
    admin_level, stage_label = _social_boosting_actor_context(s.get("owner_id"), admin_oid)
    if owner_stage_label:
        stage_label = owner_stage_label

    normalized_offers: List[Dict[str, Any]] = []
    for idx, raw_offer in enumerate(s.get("services_offers") or [], start=1):
        if not isinstance(raw_offer, dict):
            continue
        offer = dict(raw_offer)
        apply_default_offer_fields(offer)
        provider_service_id = offer_service_id(offer) or idx
        owner_rate_usd = _social_boosting_owner_rate_per_1000(
            offer,
            admin_level,
            admin_oid,
            actor_user_id,
            stage_label,
        )
        owner_rate_ghs = usd_to_ghs_rate(owner_rate_usd)
        store_rate_usd = rate_money(owner_rate_usd * (1 + (markup_percent / 100.0)))
        store_rate_ghs = rate_money(owner_rate_ghs * (1 + (markup_percent / 100.0)))
        admin_rate_usd = admin_rate_per_1000(offer, admin_level)
        admin_rate_ghs = usd_to_ghs_rate(admin_rate_usd)
        provider_rate_usd = float(service_rate_per_1000(offer))
        provider_rate_ghs = usd_to_ghs_rate(provider_rate_usd)
        normalized_offers.append(
            {
                "is_social_boosting": True,
                "amount": owner_rate_ghs,
                "amount_usd": owner_rate_usd,
                "base_amount": owner_rate_ghs,
                "base_amount_usd": owner_rate_usd,
                "total": store_rate_ghs,
                "total_usd": store_rate_usd,
                "customer_price": store_rate_ghs,
                "customer_price_usd": store_rate_usd,
                "rate_per_1000": store_rate_ghs,
                "rate_per_1000_ghs": store_rate_ghs,
                "rate_per_1000_usd": store_rate_usd,
                "store_base_rate_per_1000": owner_rate_ghs,
                "store_base_rate_per_1000_ghs": owner_rate_ghs,
                "store_base_rate_per_1000_usd": owner_rate_usd,
                "admin_rate_per_1000": admin_rate_ghs,
                "admin_rate_per_1000_ghs": admin_rate_ghs,
                "admin_rate_per_1000_usd": admin_rate_usd,
                "base_rate_per_1000": provider_rate_ghs,
                "base_rate_per_1000_ghs": provider_rate_ghs,
                "base_rate_per_1000_usd": provider_rate_usd,
                "usd_to_ghs_rate": 11.01,
                "currency": "USD",
                "display_currency": "GHS",
                "provider": SOCIAL_BOOSTING_PROVIDER,
                "provider_service_id": provider_service_id,
                "offer_id": provider_service_id,
                "offer_type": offer.get("type") or "",
                "requires_custom_comments": offer_requires_custom_comments(offer),
                "value": {
                    "social_boosting": True,
                    "provider_service_id": provider_service_id,
                    "quantity_min": offer.get("min"),
                    "quantity_max": offer.get("max"),
                    "offer_type": offer.get("type") or "",
                    "requires_custom_comments": offer_requires_custom_comments(offer),
                    "comments": normalize_custom_comments(offer),
                    "comments_text": custom_comments_text(offer),
                },
                "value_text": offer.get("name") or f"Service {provider_service_id}",
                "name": offer.get("name") or "",
                "social_media": offer.get("social_media") or "",
                "category": offer.get("category") or "",
                "min": offer.get("min"),
                "max": offer.get("max"),
                "store_percent": markup_percent,
                "_sort_platform": offer.get("social_media") or "",
                "_sort_name": offer.get("name") or "",
            }
        )

    normalized_offers.sort(key=lambda x: (x["_sort_platform"], x["_sort_name"]))
    s["offers"] = [{k: v for k, v in offer.items() if not k.startswith("_sort_")} for offer in normalized_offers]
    return s


# =====================================================================
# ✅ NEW PROFIT RULE HELPERS (PRO, SAFE)
# =====================================================================
def _effective_store_profit_percent(svc_doc: Optional[Dict[str, Any]]) -> float:
    """
    Store checkout profit percent.
    Priority:
      1) svc_doc.store_offers_profit
      2) svc_doc.default_profit_percent
      3) 0.0
    """
    if not svc_doc:
        return 0.0
    try:
        v = svc_doc.get("store_offers_profit")
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass
    try:
        v2 = svc_doc.get("default_profit_percent")
        if v2 is not None and str(v2).strip() != "":
            return float(v2)
    except Exception:
        pass
    return 0.0


# ✅ UPDATED: products loader (NOW loads from store_products_col first)
def _load_store_products(store_doc: Dict[str, Any], wa_number_raw: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def _safe_float(v: Any) -> float:
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    def _safe_int(v: Any) -> int:
        try:
            return int(float(str(v).replace(",", "").strip()))
        except Exception:
            return 0

    def _pick_img(p: Dict[str, Any]) -> str:
        return (
            (p.get("image_url") or p.get("image") or p.get("img") or p.get("photo") or "")
            if isinstance(p, dict)
            else ""
        )

    def _pick_name(p: Dict[str, Any]) -> str:
        return (p.get("name") or p.get("title") or p.get("product_name") or "Product").strip()

    def _pick_desc(p: Dict[str, Any]) -> str:
        return (p.get("description") or p.get("desc") or "").strip()

    def _pick_price(p: Dict[str, Any]) -> float:
        for k in ("price", "amount", "selling_price", "unit_price"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_float(p.get(k))
        return 0.0

    def _pick_qty(p: Dict[str, Any]) -> int:
        for k in ("quantity", "qty", "stock"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_int(p.get(k))
        return 0

    def _product_order_link(pname: str, price: float) -> str:
        msg = f"Hello {store_doc.get('name','')}, I want to order: {pname}"
        if price and price > 0:
            msg += f" (GHS {price:.2f})"
        msg += "."
        return _wa_link_from_number(wa_number_raw, msg)

    slug = store_doc.get("slug")
    owner_id = store_doc.get("owner_id")
    store_id = store_doc.get("_id")

    # 1) ✅ MAIN: store_products collection
    try:
        q_candidates: List[Dict[str, Any]] = []
        if slug:
            q_candidates.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields = {
            "_id": 1,
            "store_slug": 1,
            "store_id": 1,
            "owner_id": 1,
            "manager_id": 1,
            "name": 1,
            "description": 1,
            "image_url": 1,
            "price": 1,
            "quantity": 1,
            "status": 1,
            "created_at": 1,
            "updated_at": 1,
        }

        found: List[Dict[str, Any]] = []
        for q in q_candidates:
            try:
                if store_products_col.count_documents(q, limit=1) > 0:
                    found = list(store_products_col.find(q, fields).sort("created_at", -1))
                    break
            except Exception:
                continue

        if found:
            for p in found:
                pname = _pick_name(p)
                price = _pick_price(p)
                out.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": _pick_desc(p),
                        "image_url": _pick_img(p),
                        "price": round(price, 2),
                        "quantity": _pick_qty(p),
                        "created_at": p.get("created_at") or None,
                        "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                    }
                )
            return out
    except Exception:
        pass

    # 2) embedded on store doc (if any)
    embedded = store_doc.get("products")
    if isinstance(embedded, list) and embedded:
        for p in embedded:
            if not isinstance(p, dict):
                continue
            pname = _pick_name(p)
            price = _pick_price(p)
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": _pick_desc(p),
                    "image_url": _pick_img(p),
                    "price": round(price, 2),
                    "quantity": _pick_qty(p),
                    "created_at": p.get("created_at") or None,
                    "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                }
            )
        return out

    # 3) legacy: products collection fallback
    try:
        q_candidates2: List[Dict[str, Any]] = []
        if slug:
            q_candidates2.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates2.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates2.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields2 = {
            "_id": 1,
            "name": 1,
            "title": 1,
            "description": 1,
            "image_url": 1,
            "image": 1,
            "price": 1,
            "amount": 1,
            "selling_price": 1,
            "unit_price": 1,
            "quantity": 1,
            "created_at": 1,
            "status": 1,
        }

        found2: List[Dict[str, Any]] = []
        for q in q_candidates2:
            try:
                if products_col.count_documents(q, limit=1) > 0:
                    found2 = list(products_col.find(q, fields2).sort("created_at", -1))
                    break
            except Exception:
                continue

        for p in found2:
            pname = (p.get("name") or p.get("title") or "Product").strip()
            price = 0.0
            for k in ("price", "amount", "selling_price", "unit_price"):
                if k in p and p.get(k) not in (None, ""):
                    try:
                        price = float(str(p.get(k)).replace(",", "").strip())
                    except Exception:
                        price = 0.0
                    break
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": (p.get("description") or "").strip(),
                    "image_url": (p.get("image_url") or p.get("image") or "").strip(),
                    "price": round(price, 2),
                    "quantity": 0,
                    "created_at": p.get("created_at") or None,
                    "order_link": _wa_link_from_number(
                        wa_number_raw,
                        f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                    )
                    if wa_number_raw
                    else "",
                }
            )
    except Exception:
        return []

    return out


# ---------------------------------------------------------------------
# Parse + labels
# ---------------------------------------------------------------------
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes") or name == "afa talktime":
        return "minutes"
    return "data"

def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                try:
                    data = ast.literal_eval(vt)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    if isinstance(value, dict):
        vol = value.get("volume") or value.get("offer") or value.get("gb")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            v = float(vol)
            if unit == "minutes":
                return v
            vol_s = str(vol).upper()
            if "GB" in vol_s:
                return v * 1000.0
            if "MB" in vol_s:
                return v
            return v
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
                return float(vol_s)
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
                return float(s2)
            return None

    return None

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        parsed = _parse_value_field(cleaned)
        if isinstance(parsed, dict):
            vol = _extract_volume(parsed, unit)
            return _format_volume_unit(vol, unit) if vol is not None else "-"
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"


# ---------- pricing map builder ----------
def _build_pricing_map(pricing: Dict[str, Any]) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    percent_default = float(pricing.get("percent_default") or 0.0)
    per_map: Dict[str, Dict[str, Any]] = {}
    for x in (pricing.get("per_service") or []):
        sid = str(x.get("service_id") or "")
        if not sid:
            continue
        entry: Dict[str, Any] = {"percent": None, "offers": {}}
        if x.get("percent") is not None:
            try:
                entry["percent"] = float(x.get("percent"))
            except Exception:
                entry["percent"] = None
        for o in (x.get("offers") or []):
            try:
                idx = int(o.get("index"))
                tot = _to_float(o.get("total"))
                if tot is not None:
                    entry["offers"][idx] = float(tot)
            except Exception:
                continue
        per_map[sid] = entry
    return percent_default, per_map


# ---------- apply pricing to a service (for page render) ----------
def _offer_value_text(o: Dict[str, Any], unit: str) -> str:
    vt = o.get("value_text")
    if isinstance(vt, str) and vt.strip():
        try:
            cleaned = _PKG_TAIL.sub("", vt).strip()
            vol = _extract_volume(cleaned, unit)
            if vol is not None:
                return _format_volume_unit(vol, unit)
        except Exception:
            pass
    lab = _value_text_for_display(o.get("value"), unit)
    return lab or "-"

def _apply_store_pricing_to_service(
    svc: Dict[str, Any],
    percent_default: float,
    per_service_map: Dict[str, Dict[str, Any]],
    *,
    admin_oid: Optional[ObjectId] = None,
    actor_user_id: Any = None,
    owner_stage_label: str = "Normal Agent",
) -> Dict[str, Any]:
    s = dict(svc)
    if is_social_boosting_service(s):
        pct = _pricing_percent_for_service(s.get("_id") or s.get("_id_str"), percent_default, per_service_map)
        return _normalize_social_boosting_service_for_store(
            s,
            admin_oid=admin_oid,
            actor_user_id=actor_user_id,
            owner_stage_label=owner_stage_label,
            store_markup_percent=pct,
        )

    unit = _service_unit(s)
    src_offers = _svc_offers_list(s)
    svc_id_str = str(s.get("_id"))
    per_entry = per_service_map.get(svc_id_str, {})
    svc_percent: Optional[float] = per_entry.get("percent")
    offer_overrides: Dict[int, float] = per_entry.get("offers") or {}

    norm_offers: List[Dict[str, Any]] = []
    for idx, of in enumerate(src_offers):
        base_amount = _offer_base_amount(of)
        explicit_store_price = _to_float(of.get("customer_price"))
        if idx in offer_overrides:
            total = round(float(offer_overrides[idx]), 2)
        elif explicit_store_price is not None:
            total = round(float(explicit_store_price), 2)
        else:
            total = None
        vt = _offer_value_text(of, unit)
        norm_offers.append(
            {
                "value_text": vt,
                "total": total,
                "amount": base_amount,
                "value": of.get("value"),
            }
        )

    priced_offers = [offer for offer in norm_offers if offer.get("total") is not None]
    s["offers"] = priced_offers
    if not priced_offers:
        s["can_order"] = False
        s["disabled_reason"] = "Store price not configured."
    s["offers_source"] = "store_offers" if (isinstance(s.get("store_offers"), list) and s.get("store_offers")) else "offers"
    return s


# ---------- DB loads for editor/view ----------
def _load_all_services_for_store_edit() -> List[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    fields = {
        "_id": 1,
        "name": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,
        "services_offers": 1,
        "base_service_id": 1,
        "unit": 1,
        "network": 1,
        "service_network": 1,
    }
    admin_oid = current_admin_id_from_session(session)
    if not admin_oid:
        return []
    raw = list(
        services_col.find(
            {
                "$or": [
                    {
                        "admin_id": admin_oid,
                        "_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                        "base_service_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                        "name": {"$ne": SOCIAL_BOOSTING_NAME},
                    },
                    {"_id": SOCIAL_BOOSTING_SERVICE_ID},
                ],
                "agent_visible": {"$ne": False},
                "display_enabled": {"$ne": False},
                f"agent_visibility_by_admin.{str(admin_oid)}": {"$ne": False},
            },
            fields,
        )
    )
    raw.sort(key=lambda x: _norm(x.get("name") or ""))

    owner_stage_label = ((_lookup_user_any_status(session.get("user_id")) or {}).get("stage_label") or "Normal Agent").strip() or "Normal Agent"
    clean: List[Dict[str, Any]] = []
    for r in raw:
        if str(r.get("_id") or "") == BULK_SMS_SERVICE_ID or _norm(r.get("name") or "") == "bulk sms":
            continue
        if is_social_boosting_service(r):
            clean.append(
                _json_safe(
                    _normalize_social_boosting_service_for_store(
                        r,
                        admin_oid=admin_oid,
                        actor_user_id=session.get("user_id"),
                        owner_stage_label=owner_stage_label,
                        store_markup_percent=0.0,
                    )
                )
            )
            continue

        s: Dict[str, Any] = {
            "_id_str": str(r.get("_id")),
            "name": r.get("name") or "",
            "image_url": r.get("image_url") or "",
            "network": r.get("network") or "",
            "service_network": r.get("service_network") or "",
        }
        unit = _service_unit(r)
        src_offers = _svc_offers_list(r)

        new_off: List[Dict[str, Any]] = []
        for o in src_offers:
            base_amount = _offer_base_amount(o)
            new_off.append(
                {
                    "amount": base_amount,
                    "base_amount": base_amount,
                    "customer_price": None,
                    "total": round(float(base_amount), 2) if base_amount is not None else None,
                    "value": o.get("value"),
                    "value_text": _offer_value_text(o, unit),
                }
            )

        s["offers"] = new_off
        s["offers_source"] = "store_offers" if (isinstance(r.get("store_offers"), list) and r.get("store_offers")) else "offers"
        clean.append(_json_safe(s))
    return clean

def _load_services_for_store_view(
    scope: str,
    ids: List[str],
    admin_oid: Optional[ObjectId] = None,
) -> List[Dict[str, Any]]:
    if not admin_oid:
        return []
    q: Dict[str, Any] = {}
    if scope == "selected" and ids:
        try:
            q = {"_id": {"$in": [ObjectId(x) for x in ids if x]}}
        except Exception:
            q = {"_id": {"$in": []}}

    if admin_oid:
        q["$or"] = [
            {
                "admin_id": admin_oid,
                "_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "base_service_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID},
                "name": {"$ne": SOCIAL_BOOSTING_NAME},
            },
            {"_id": SOCIAL_BOOSTING_SERVICE_ID},
        ]
        q["agent_visible"] = {"$ne": False}
        q["display_enabled"] = {"$ne": False}
        q[f"agent_visibility_by_admin.{str(admin_oid)}"] = {"$ne": False}

    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,
        "services_offers": 1,
        "base_service_id": 1,
        "store_offers_profit": 1,  # ✅ IMPORTANT for profit logic
        "service_category": 1,
        "priority": 1,
        "display_order": 1,
        "created_at": 1,
        "unit": 1,
        "default_profit_percent": 1,
        "network_id": 1,
        "network": 1,
        "service_network": 1,
        "provider": 1,
        "closed_message": 1,
        "out_of_stock_message": 1,
    }
    raw = list(services_col.find(q, fields))
    raw = _sorted_services(raw)
    for s in raw:
        s["_id_str"] = str(s["_id"])
        s.update(_service_state(s))
    return raw

def _load_products_as_services_fallback(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        if store_doc.get("slug"):
            q_alt = {"store_slug": store_doc.get("slug"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_alt, limit=1) > 0:
                q = q_alt
        if store_doc.get("owner_id"):
            q_owner = {"owner_id": store_doc.get("owner_id"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_owner, limit=1) > 0:
                q = q_owner

        fields = {"_id": 1, "name": 1, "title": 1, "image_url": 1, "price": 1, "amount": 1, "created_at": 1}
        prods = list(products_col.find(q, fields).sort("created_at", -1))
        out: List[Dict[str, Any]] = []
        for p in prods:
            name = (p.get("name") or p.get("title") or "Product").strip()
            price = _to_float(p.get("price")) or _to_float(p.get("amount")) or 0.0
            svc = {
                "_id": p.get("_id"),
                "_id_str": str(p.get("_id")),
                "name": name,
                "type": "MANUAL",
                "status": "OPEN",
                "availability": "AVAILABLE",
                "image_url": p.get("image_url"),
                "service_category": "product",
                "priority": None,
                "display_order": None,
                "created_at": p.get("created_at") or datetime.utcnow(),
                "unit": "item",
                "offers": [
                    {
                        "value_text": "1 item",
                        "total": round(float(price), 2),
                        "amount": round(float(price), 2),
                        "value": {"volume": 1},
                    }
                ],
            }
            svc.update(_service_state(svc))
            out.append(svc)
        return out
    except Exception:
        return []


# ---------- NEW: safe ObjectId + user lookup (NO status filter) ----------
def _safe_oid(v: Any) -> Optional[ObjectId]:
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, str):
        try:
            return ObjectId(v)
        except Exception:
            return None
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _lookup_user_any_status(user_id: Any) -> Dict[str, Any]:
    """
    Fetch user by _id WITHOUT filtering status.
    """
    oid = _safe_oid(user_id)
    if not oid:
        return {}
    try:
        u = users_col.find_one(
            {"_id": oid},
            {"email": 1, "phone": 1, "username": 1, "first_name": 1, "last_name": 1, "name": 1, "status": 1, "stage_label": 1},
        )
        return u or {}
    except Exception:
        return {}

def _user_first_last(u: Dict[str, Any]) -> Tuple[str, str]:
    """
    Derive first/last from first_name/last_name, or from 'name' if present.
    """
    first = (u.get("first_name") or "").strip()
    last = (u.get("last_name") or "").strip()
    if first or last:
        return first, last

    full = (u.get("name") or u.get("username") or "").strip()
    if not full:
        return "", ""
    parts = [p for p in re.split(r"\s+", full) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------- JSON-safe converter (UPDATED to include owner email/phone safely) ----------
def _store_to_client(s: Optional[dict]) -> dict:
    if not s:
        return {}
    out: Dict[str, Any] = {}
    for k, v in s.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [
                (str(x) if isinstance(x, ObjectId) else x.isoformat() if isinstance(x, datetime) else x)
                for x in v
            ]
        elif isinstance(v, dict):
            if k == "pricing":
                per = []
                for row in (v.get("per_service") or []):
                    row2 = dict(row)
                    if isinstance(row2.get("service_id"), ObjectId):
                        row2["service_id"] = str(row2["service_id"])
                    per.append(row2)
                out[k] = {**v, "per_service": per}
            else:
                out[k] = {
                    kk: (
                        str(vv)
                        if isinstance(vv, ObjectId)
                        else vv.isoformat()
                        if isinstance(vv, datetime)
                        else vv
                    )
                    for kk, vv in v.items()
                }
        else:
            out[k] = v
    if "service_ids" in out:
        out["service_ids"] = [str(x) for x in (out.get("service_ids") or [])]

    # ✅ attach owner info from users collection (even if user.status == 'deleted')
    try:
        u = _lookup_user_any_status(s.get("owner_id"))
        out["owner_email"] = (u.get("email") or "").strip()
        out["owner_phone"] = (u.get("phone") or "").strip()
        out["owner_username"] = (u.get("username") or "").strip()
        out["owner_status"] = (u.get("status") or "").strip()
        fn, ln = _user_first_last(u or {})
        out["owner_first_name"] = fn
        out["owner_last_name"] = ln
    except Exception:
        out["owner_email"] = out.get("owner_email") or ""
        out["owner_phone"] = out.get("owner_phone") or ""
        out["owner_first_name"] = out.get("owner_first_name") or ""
        out["owner_last_name"] = out.get("owner_last_name") or ""

    return _json_safe(out)


def _normalize_store_afa_config(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    cfg = store_doc.get("afa_product") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = bool(cfg.get("enabled"))
    price = _to_float(cfg.get("price")) or 0.0
    if price < 0:
        price = 0.0
    title = (cfg.get("title") or "AFA Registration").strip()
    description = (cfg.get("description") or "Register your AFA quickly and securely.").strip()
    image_url = (cfg.get("image_url") or "").strip()
    return {
        "enabled": bool(enabled and price > 0),
        "price": round(float(price), 2),
        "title": title,
        "description": description,
        "image_url": image_url,
    }


def _normalize_store_checker_config(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    cfg = store_doc.get("checker_product") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = bool(cfg.get("enabled"))
    title = (cfg.get("title") or "Results Checker").strip()
    description = (cfg.get("description") or "Buy BECE or WASSCE checker and receive it by SMS.").strip()
    image_url = (cfg.get("image_url") or "https://waecgambia.org/wp-content/uploads/2025/06/510-x-600-WASSCE.png").strip()
    types_cfg = cfg.get("types") or {}
    if not isinstance(types_cfg, dict):
        types_cfg = {}

    def _type_entry(key: str, label: str) -> Dict[str, Any]:
        raw = types_cfg.get(key) or {}
        if not isinstance(raw, dict):
            raw = {}
        type_enabled = bool(raw.get("enabled"))
        price = round(float(_to_float(raw.get("price")) or 0.0), 2)
        return {
            "key": key,
            "label": label,
            "enabled": bool(type_enabled and price > 0),
            "price": price,
        }

    type_options = [
        _type_entry("wassce", "WASSCE"),
        _type_entry("bece", "BECE"),
    ]
    active_types = [item for item in type_options if item.get("enabled")]
    return {
        "enabled": bool(enabled and active_types),
        "title": title,
        "description": description,
        "image_url": image_url,
        "types": type_options,
        "active_types": active_types,
    }


def _bulk_sms_owner_price(store_doc: Dict[str, Any]) -> Optional[float]:
    admin_id = _store_admin_id(store_doc)
    owner_doc = _lookup_user_any_status(store_doc.get("owner_id")) or {}
    service = find_bulk_sms_service_for_admin(admin_id)
    return sms_price_for_user(service, owner_doc.get("stage_label"))


def _normalize_store_bulk_sms_config(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    cfg = store_doc.get("bulk_sms_product") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = bool(cfg.get("enabled"))
    admin_id = _store_admin_id(store_doc)
    service = find_bulk_sms_service_for_admin(admin_id) or {}
    title = (service.get("display_name") or service.get("name") or "Bulk SMS").strip()
    description = (
        service.get("description")
        or cfg.get("description")
        or "Send one sender name to multiple recipient numbers."
    ).strip()
    image_url = (service.get("image_url") or "/uploads/bulk_sms_59d515e8.jpg").strip()
    owner_price = _bulk_sms_owner_price(store_doc)
    owner_price_num = round(float(owner_price), 4) if owner_price is not None else None
    price = round(float(_to_float(cfg.get("price_per_sms") or cfg.get("price")) or 0.0), 4)
    if owner_price_num is not None and 0 < price < owner_price_num:
        price = owner_price_num
    if price < 0:
        price = 0.0
    return {
        "enabled": bool(enabled and price > 0 and owner_price_num is not None),
        "configured": bool(enabled and price > 0),
        "price_per_sms": price,
        "owner_price_per_sms": owner_price_num,
        "profit_per_sms": round(max(0.0, price - owner_price_num), 4) if owner_price_num is not None else 0.0,
        "title": title,
        "description": description,
        "image_url": image_url,
        "disclaimer": SMS_DISCLAIMER_TEXT,
    }


def _store_checker_owner_price(store_doc: Dict[str, Any], checker_type: Any) -> Optional[float]:
    checker_kind = normalize_checker_type(checker_type)
    admin_id = _store_admin_id(store_doc)
    owner_doc = _lookup_user_any_status(store_doc.get("owner_id")) or {}
    stage_label = (owner_doc.get("stage_label") or "Normal Agent").strip() or "Normal Agent"
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}
    admin_level = normalize_admin_level((admin_doc or {}).get("admin_level"))
    pricing_doc = get_checker_pricing_doc(checker_kind)
    return customer_stage_price(pricing_doc, admin_id=admin_id, admin_level=admin_level, stage_label=stage_label)


def _available_checker_stock(checker_type: Any) -> Optional[Dict[str, Any]]:
    return checker_stock_col.find_one(
        {"type": normalize_checker_type(checker_type), "status": "not_sold"},
        sort=[("created_at", 1), ("_id", 1)],
    )


def _checker_sms_message(checker: Dict[str, Any], sender_name: str) -> str:
    checker_type = str(checker.get("type") or "").upper() or "RESULT CHECKER"
    body = str(checker.get("message") or "").strip()
    sender_label = sender_name or "Azico"
    return f"{checker_type} via {sender_label}\n{body}" if body else f"{checker_type} via {sender_label}"


def _admin_afa_settings_price(default: float = 0.0, admin_oid: ObjectId | None = None) -> float:
    """
    Read the tenant admin AFA settings price.
    Returns `default` if missing/invalid.
    """
    return load_afa_price(admin_oid, default=default)


def _afa_profit_layers(admin_oid: ObjectId | None, selling_amount: Any) -> Dict[str, float]:
    """
    AFA has three commercial layers:
    - main admin configured price: cost/base for the sub-admin
    - sub-admin configured price: base for the agent/store owner
    - final selling amount: store/customer price

    There is no lower provider cost configured for AFA, so main_base_amount is 0
    and the main admin profit is the main admin AFA price.
    """
    main_admin_price = ledger_money(load_afa_base_price(default=0.0))
    admin_base_price = ledger_money(load_afa_admin_base_price(admin_oid, users_col, default=main_admin_price))
    admin_agent_price = ledger_money(_admin_afa_settings_price(default=admin_base_price, admin_oid=admin_oid))
    selling = ledger_money(selling_amount)
    return {
        "main_base_amount": main_admin_price,
        "admin_base_amount": admin_base_price,
        "store_owner_base_amount": admin_agent_price,
        "selling_amount": selling,
        "store_profit_amount": max(0.0, round(selling - admin_agent_price, 2)),
    }


# ---------- helper: find current user's store ----------
def _find_user_store(user_id: ObjectId, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    q: Dict[str, Any] = {"owner_id": user_id, "status": {"$ne": "deleted"}}
    if slug:
        q["slug"] = slug
    return stores_col.find_one(q, sort=[("updated_at", -1), ("created_at", -1)])


# ---------- compatibility helper: _find (some files import it) ----------
def _find(col, q: dict, projection: Optional[dict] = None, sort: Optional[list] = None):
    """
    Compatibility helper (kept to prevent ImportError in files that do:
      from .store_page import _find
    """
    try:
        if sort:
            return col.find_one(q, projection or None, sort=sort)
        return col.find_one(q, projection or None)
    except Exception:
        return None


# ---------- helper: store owner's email (UPDATED: no status filter) ----------
def _get_owner_email_for_store(store_doc: Dict[str, Any]) -> str:
    try:
        oid2 = _safe_oid(store_doc.get("owner_id"))
        if not oid2:
            return ""
        u = users_col.find_one({"_id": oid2}, {"email": 1})
        if not u:
            return ""
        return (u.get("email") or "").strip()
    except Exception:
        return ""

def _get_owner_identity_for_store(store_doc: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    ✅ Paystack payer identity MUST come from DB (no fallback defaults).
    Returns (email, first_name, last_name)
    """
    try:
        u = _lookup_user_any_status(store_doc.get("owner_id"))
        email = (u.get("email") or "").strip()
        first, last = _user_first_last(u or {})
        return email, (first or "").strip(), (last or "").strip()
    except Exception:
        return "", "", ""


# ---------- shared upsert ----------
def _upsert_store_from_payload(owner_id: ObjectId, data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    name = (data.get("name") or "").strip()
    slug = _slugify(data.get("slug") or name)
    status = (data.get("status") or "published").strip()
    if not name or not slug:
        return False, {"message": "Name and slug are required"}

    existing = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if existing and str(existing.get("owner_id")) != str(owner_id):
        return False, {"message": "Slug already taken"}

    afa_product = data.get("afa_product") or {}
    if not isinstance(afa_product, dict):
        afa_product = {}
    afa_enabled = bool(afa_product.get("enabled"))
    afa_price = _to_float(afa_product.get("price"))
    owner_admin_id = current_admin_id_from_session(session) or resolve_admin_id_for_user_id(users_col, owner_id)
    system_afa_price = _admin_afa_settings_price(default=0.0, admin_oid=owner_admin_id)
    if afa_enabled:
        if afa_price is None or afa_price <= 0:
            return False, {"message": "Set a valid AFA store price above 0 or disable AFA."}
        if system_afa_price > 0 and float(afa_price) < float(system_afa_price):
            return False, {"message": f"AFA store price cannot be below the system price of GHS {system_afa_price:.2f}."}
    sanitized_afa_product = {
        "enabled": bool(afa_enabled and (afa_price or 0) > 0),
        "price": round(float(afa_price or 0.0), 2),
    }

    doc = {
        "owner_id": owner_id,
        "admin_id": owner_admin_id,
        "name": name,
        "slug": slug,
        "logo_url": (data.get("logo_url") or "").strip(),
        "layout": (data.get("layout") or "grid-2").strip(),
        "theme": data.get("theme") or {},
        "hero": data.get("hero") or {},
        "service_scope": data.get("service_scope") or "all",
        "service_ids": data.get("service_ids") or [],
        "pricing": data.get("pricing") or {"mode": "percent", "percent_default": 0.0, "per_service": []},
        "afa_product": sanitized_afa_product,
        "checker_product": data.get("checker_product") or {},
        "bulk_sms_product": data.get("bulk_sms_product") or {},
        "products": data.get("products") or data.get("store_products") or data.get("items") or [],
        "whatsapp_number": (data.get("whatsapp_number") or data.get("whatsapp") or "").strip()
        if isinstance(data.get("whatsapp_number") or data.get("whatsapp") or "", str)
        else data.get("whatsapp_number") or data.get("whatsapp"),
        "whatsapp_group": (data.get("whatsapp_group") or data.get("whatsapp_group_link") or "").strip(),
        "status": status,
        "updated_at": datetime.utcnow(),
    }
    stores_col.update_one(
        {"slug": slug, "owner_id": owner_id},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return True, {"slug": slug, "status": status}


# =====================================================================
# PAGES (PUBLIC)
# =====================================================================
@stores_bp.route("/s/<slug>", methods=["GET"])
@stores_bp.route("/store/<slug>", methods=["GET"])
def store_public_page(slug: str):
    store_doc = stores_col.find_one(
        {"slug": slug, "status": {"$regex": r"^published$", "$options": "i"}}
    )
    if not store_doc:
        # allow preview=1 for logged-in owner
        if request.args.get("preview") == "1" and session.get("user_id"):
            store_doc = stores_col.find_one(
                {"slug": slug, "owner_id": ObjectId(session["user_id"]), "status": {"$ne": "deleted"}}
            )
            if not store_doc:
                return "Store not found", 404
        else:
            if _host_is_store_domain(request.host):
                return redirect(url_for("index.landing"))
            return "Store not found", 404

    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    store_admin_id = _store_admin_id(store_doc)
    services = _load_services_for_store_view(scope, service_ids, admin_oid=store_admin_id)

    # legacy fallback (only if you were using products as services)
    if not services:
        services = _load_products_as_services_fallback(store_doc)

    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    owner_stage_label = ((_lookup_user_any_status(store_doc.get("owner_id")) or {}).get("stage_label") or "Normal Agent").strip() or "Normal Agent"
    priced = [
        _apply_store_pricing_to_service(
            s,
            percent_default,
            per_map,
            admin_oid=store_admin_id,
            actor_user_id=store_doc.get("owner_id"),
            owner_stage_label=owner_stage_label,
        )
        for s in services
    ]

    q = request.query_string.decode("utf-8")
    canonical_url = f"https://{TARGET_STORE_HOST}{request.path}" + (f"?{q}" if q else "")

    wa = _extract_store_whatsapp(store_doc)

    # ✅ REAL products list for the Products tab
    products = _load_store_products(store_doc, wa.get("number_raw") or "")

    # ✅ Ensure we never pass secret key to frontend
    pk_for_frontend, _ = _load_store_paystack_keys(admin_id=store_admin_id)
    pk_for_frontend = pk_for_frontend if _is_pk(pk_for_frontend) else ""

    # ✅ Fetch owner identity for Paystack payer identity (NO DEFAULTS)
    ps_email, ps_first, ps_last = _get_owner_identity_for_store(store_doc)

    # ✅ Fetch email (store email + owner email) for extra context if needed
    owner_email = _get_owner_email_for_store(store_doc)
    owner_doc = _lookup_user_any_status(store_doc.get("owner_id")) or {}
    owner_stage = owner_doc.get("stage_label")
    agent_code_doc = get_or_create_agent_code_for_user(
        store_doc.get("owner_id"),
        admin_id=owner_doc.get("admin_id"),
    )
    owner_agent_code = {
        "agent_code": (agent_code_doc or {}).get("agent_code") or "",
        "status": ((agent_code_doc or {}).get("status") or "active").strip().lower(),
    }
    afa_config = _normalize_store_afa_config(store_doc)
    checker_config = _normalize_store_checker_config(store_doc)
    bulk_sms_config = _normalize_store_bulk_sms_config(store_doc)

    return render_template(
        "store_page.html",
        store=store_doc,
        services=priced,
        products=products,
        paystack_pk=pk_for_frontend,
        canonical_url=canonical_url,
        whatsapp_number=wa.get("number_raw") or "",
        whatsapp_number_digits=wa.get("number_digits") or "",
        whatsapp_number_link=wa.get("number_link") or "",
        whatsapp_group_link=wa.get("group_link") or "",
        # extra fields (won't break template even if unused)
        store_email=(store_doc.get("email") or "").strip(),
        owner_email=owner_email,
        owner_stage=owner_stage,
        owner_agent_code=owner_agent_code,
        afa_config=afa_config,
        checker_config=checker_config,
        bulk_sms_config=bulk_sms_config,

        # ✅ REQUIRED by your HTML scripts (NO DEFAULTS)
        paystack_payer_email=ps_email,
        paystack_payer_first=ps_first,
        paystack_payer_last=ps_last,
    )


# ✅ API: fetch store email (and owner email) without touching HTML
@stores_bp.route("/api/store-email/<slug>", methods=["GET"])
def api_store_email(slug: str):
    try:
        store_doc = stores_col.find_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"email": 1, "owner_id": 1, "slug": 1, "name": 1},
        )
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404
        owner_email = _get_owner_email_for_store(store_doc)
        return jsonify(
            {
                "success": True,
                "slug": slug,
                "store_name": store_doc.get("name") or "",
                "store_email": (store_doc.get("email") or "").strip(),
                "owner_email": owner_email,
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# ✅ API: Store products payload builder
def _products_payload(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    wa = _extract_store_whatsapp(store_doc or {})
    products = _load_store_products(store_doc or {}, wa.get("number_raw") or "")
    return {
        "success": True,
        "store": {
            "slug": store_doc.get("slug") or "",
            "name": store_doc.get("name") or "",
            "logo_url": store_doc.get("logo_url") or "",
            "status": store_doc.get("status") or "",
            "owner_id": str(store_doc.get("owner_id")) if store_doc.get("owner_id") else "",
        },
        "whatsapp": {
            "number_raw": wa.get("number_raw") or "",
            "number_digits": wa.get("number_digits") or "",
            "number_link": wa.get("number_link") or "",
            "group_link": wa.get("group_link") or "",
        },
        "count": len(products),
        "products": products,
    }

@stores_bp.route("/api/store-products/<slug>", methods=["GET"])
def api_store_products_by_slug(slug: str):
    """
    Frontend-friendly products API.
    - Returns products created for this store (store_products primary, then fallbacks).
    - Optional: ?owner_id=<id> or ?manager_id=<id> (filters if you use those fields)
    """
    try:
        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404
        store_admin_id = _store_admin_id(store_doc)
        store_admin_id = _store_admin_id(store_doc)
        store_admin_id = _store_admin_id(store_doc)
        if not store_admin_id:
            return jsonify({"success": False, "message": "Store is not linked to an admin"}), 400

        owner_id = (request.args.get("owner_id") or "").strip()
        manager_id = (request.args.get("manager_id") or "").strip()

        if owner_id or manager_id:
            q: Dict[str, Any] = {"store_slug": slug, "status": {"$ne": "deleted"}}
            if owner_id:
                q["owner_id"] = owner_id
            if manager_id:
                q["manager_id"] = manager_id

            fields = {
                "_id": 1,
                "name": 1,
                "description": 1,
                "image_url": 1,
                "price": 1,
                "quantity": 1,
                "created_at": 1,
                "updated_at": 1,
            }

            found = list(store_products_col.find(q, fields).sort("created_at", -1))
            wa = _extract_store_whatsapp(store_doc)
            products: List[Dict[str, Any]] = []
            for p in found:
                try:
                    price = float(str(p.get("price") or "0").replace(",", "").strip())
                except Exception:
                    price = 0.0
                pname = (p.get("name") or "Product").strip()
                qty_raw = p.get("quantity")
                try:
                    qty = int(float(str(qty_raw).replace(",", "").strip())) if str(qty_raw or "").strip() != "" else 0
                except Exception:
                    qty = 0

                products.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": (p.get("description") or "").strip(),
                        "image_url": (p.get("image_url") or "").strip(),
                        "price": round(price, 2),
                        "quantity": qty,
                        "created_at": p.get("created_at") or None,
                        "order_link": _wa_link_from_number(
                            wa.get("number_raw") or "",
                            f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                        )
                        if (wa.get("number_raw") or "")
                        else "",
                    }
                )

            payload = _products_payload(store_doc)
            payload["products"] = products
            payload["count"] = len(products)
            payload["filters"] = {"owner_id": owner_id, "manager_id": manager_id}
            return jsonify(payload), 200

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500

@stores_bp.route("/api/store-products/by-owner/<owner_id>", methods=["GET"])
def api_store_products_by_owner(owner_id: str):
    """
    Useful for dashboards:
    GET /api/store-products/by-owner/<owner_id>
    Optional: ?slug=<store_slug>
    """
    try:
        owner_id = (owner_id or "").strip()
        if not owner_id:
            return jsonify({"success": False, "message": "owner_id required"}), 400

        slug = (request.args.get("slug") or "").strip()

        store_q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        oid = _safe_oid(owner_id)
        if oid:
            store_q["owner_id"] = oid
        else:
            store_q["owner_id"] = owner_id

        if slug:
            store_q["slug"] = slug

        store_doc = stores_col.find_one(store_q, sort=[("updated_at", -1), ("created_at", -1)])
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found for owner"}), 404

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# =====================================================================
# PAYSTACK FLOW (Store)
# =====================================================================
def _utc_day_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def _safe_verify_payload(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    return {"value": v}

def _insert_paystack_audit(
    *,
    store_slug: str,
    order_id: Optional[str],
    paystack_reference: str,
    profile_used: str,
    verify_ok: bool,
    paid_ghs: Optional[float],
    expected_ghs: Optional[float],
    gateway_fee_overage_ghs: Optional[float],
    paystack_currency: Optional[str],
    paystack_channel: Optional[str],
    response_message: str,
    raw_verify_data: Any,
    day_key: str,
    secondary_count_after: Optional[int],
) -> None:
    try:
        audit_paystack.insert_one(
            {
                "created_at": datetime.utcnow(),
                "store_slug": store_slug,
                "order_id": order_id or None,
                "paystack_reference": paystack_reference,
                "profile_used": profile_used,
                "verify_ok": bool(verify_ok),
                "paid_ghs": paid_ghs,
                "expected_ghs": expected_ghs,
                "gateway_fee_overage_ghs": gateway_fee_overage_ghs,
                "paystack_currency": paystack_currency,
                "paystack_channel": paystack_channel,
                "response_message": response_message or "",
                "raw_verify_data": _safe_verify_payload(raw_verify_data),
                "day_key": day_key,
                "secondary_count_after": secondary_count_after,
            }
        )
    except Exception:
        pass

def _verify_paystack(reference: str, profile: str = "primary", admin_id: ObjectId | None = None) -> Tuple[bool, Dict[str, Any], str, Dict[str, Any]]:
    _, secret = _load_store_paystack_keys(admin_id=admin_id)
    if not secret or not _is_sk(secret):
        return (False, {}, "Payment processor not configured.", {"status": False, "message": "Payment processor not configured."})
    try:
        headers = {"Authorization": f"Bearer {secret}"}
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        r = requests.get(url, headers=headers, timeout=25)
        result = r.json()
        if not result.get("status"):
            return (False, result.get("data") or {}, result.get("message") or "Verification failed.", result)
        data = result.get("data") or {}
        ok = data.get("status") == "success"
        if not ok:
            return (False, data, data.get("gateway_response") or "Payment not successful.", result)
        return (True, data, result.get("message") or "", result)
    except Exception as e:
        return (False, {}, f"Verify error: {str(e)}", {"exception": str(e)})


@stores_bp.route("/api/store-afa/register/<slug>", methods=["POST"])
def api_store_afa_register(slug: str):
    try:
        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        afa_cfg = _normalize_store_afa_config(store_doc)
        if not afa_cfg.get("enabled"):
            return jsonify({"success": False, "message": "AFA registration is not enabled for this store."}), 400

        payload = request.get_json(silent=True) or {}
        paystack = payload.get("paystack") or {}
        ps_ref = (paystack.get("reference") or payload.get("paystack_reference") or "").strip()

        name = (payload.get("name") or "").strip()
        phone = _normalize_gh_phone(payload.get("phone") or "")
        dob = (payload.get("dob") or "").strip() or None
        location = (payload.get("location") or "").strip() or None
        ghana_card = (payload.get("ghana_card") or "").strip() or None

        if not name:
            return jsonify({"success": False, "message": "Name is required"}), 400
        if not re.match(r"^0\d{9}$", phone):
            return jsonify({"success": False, "message": "Phone must be 0xxxxxxxxx"}), 400
        if not ps_ref:
            return jsonify({"success": False, "message": "Paystack reference is required"}), 400

        existing = afa_col.find_one({"store_slug": slug, "paystack_reference": ps_ref}, {"_id": 1})
        if existing:
            return jsonify({"success": True, "message": "Registration already received.", "idempotent": True}), 200

        store_admin_id = _store_admin_id(store_doc)
        ok, verify_data, verify_message, _raw_verify = _verify_paystack(ps_ref, profile="primary", admin_id=store_admin_id)
        if not ok:
            return jsonify({"success": False, "message": f"Payment verification failed: {verify_message}"}), 400

        paid_pes = int((verify_data or {}).get("amount") or 0)
        paid_ghs = round(paid_pes / 100.0, 2)
        currency = str((verify_data or {}).get("currency") or "GHS").upper()
        expected_ghs = round(float(afa_cfg.get("price") or 0.0), 2)
        afa_layers = _afa_profit_layers(store_admin_id, expected_ghs)
        admin_afa_price = afa_layers["store_owner_base_amount"]
        store_profit_amount = afa_layers["store_profit_amount"]
        expected_pes = int(round(expected_ghs * 100))

        if currency != "GHS" or paid_pes < expected_pes:
            return jsonify({"success": False, "message": "Amount paid is less than configured AFA price."}), 400

        admin_wallet_debit_total = round(admin_afa_price, 2)
        agent_wallet_debit_total = 0.0
        wallet_debit_status = "completed"
        debit_ok, debit_message, debit_rows = debit_wallets_for_order(
            balances_col=balances_col,
            balance_logs_col=balance_logs_col,
            transactions_col=transactions_col,
            debits=[
                {"user_id": store_admin_id, "amount": admin_wallet_debit_total, "label": "admin_base_debit"},
            ],
            order_id=ps_ref,
            admin_id=store_admin_id,
            source="store_afa_registration",
            note="Store AFA wallet debit",
            meta={
                "store_slug": slug,
                "paystack_reference": ps_ref,
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "agent_wallet_debit_total": agent_wallet_debit_total,
                "customer_charge_total": expected_ghs,
                "store_profit_amount": round(store_profit_amount, 2),
                "allow_negative_wallet": True,
            },
            allow_negative=True,
        )
        if not debit_ok:
            message = debit_message if debit_message == WALLET_OVERDRAFT_LIMIT_MESSAGE else f"Wallet debit failed: {debit_message}"
            return jsonify({"success": False, "message": message}), 400
        try:
            evaluate_admin_wallet_low_balance(store_admin_id, send_alert=True, run_auto_credit=True)
        except Exception:
            pass

        customer_oid = None
        if session.get("role") in {"customer", "agent"} and session.get("user_id"):
            try:
                customer_oid = ObjectId(session["user_id"])
            except Exception:
                customer_oid = None

        now = datetime.utcnow()
        reg_doc: Dict[str, Any] = {
            "store_slug": slug,
            "store_owner_id": store_doc.get("owner_id"),
            "admin_id": store_admin_id,
            "source": "store_page_paystack",
            "paystack_reference": ps_ref,
            "status": "pending",
            "charged": True,
            "amount": expected_ghs,
            "charged_amount": expected_ghs,
            "paystack_paid_amount": paid_ghs,
            "admin_wallet_debit_total": admin_wallet_debit_total,
            "agent_wallet_debit_total": agent_wallet_debit_total,
            "wallet_debit_status": wallet_debit_status,
            "wallet_debits": debit_rows,
            "charged_at": now,
            "charged_by": "store_page",
            "name": name,
            "phone": phone,
            "dob": dob,
            "location": location,
            "ghana_card": ghana_card,
            "created_at": now,
            "updated_at": now,
        }
        if customer_oid:
            reg_doc["customer_id"] = customer_oid

        reg_id = afa_col.insert_one(reg_doc).inserted_id
        line = {
            "phone": phone,
            "base_amount": afa_layers["admin_base_amount"],
            "main_base_amount": afa_layers["main_base_amount"],
            "admin_base_amount": afa_layers["admin_base_amount"],
            "store_owner_base_amount": afa_layers["store_owner_base_amount"],
            "selling_amount": expected_ghs,
            "amount": expected_ghs,
            "profit_amount": 0.0,
            "profit_percent_used": 0.0,
            "value": "AFA Registration",
            "value_obj": {"registration_id": str(reg_id), "source": "store_page_paystack"},
            "serviceId": "afa_registration",
            "serviceName": "AFA Registration",
            "service_type": "AFA",
            "line_status": "completed",
            "api_status": "not_applicable",
            "api_response": {"note": "AFA registration recorded."},
            "store_profit_amount": store_profit_amount,
        }
        finalized_items, profit_split_totals = _finalize_store_profit_lines([line], store_doc)
        order_id = f"AFA-{ps_ref}"
        order_doc = {
            "user_id": customer_oid,
            "admin_id": store_admin_id,
            "wallet_owner_user_id": store_admin_id,
            "store_slug": slug,
            "store_owner_id": store_doc.get("owner_id"),
            "order_id": order_id,
            "items": finalized_items,
            "total_amount": expected_ghs,
            "charged_amount": expected_ghs,
            "admin_wallet_debit_total": admin_wallet_debit_total,
            "agent_wallet_debit_total": agent_wallet_debit_total,
            "wallet_debit_status": wallet_debit_status,
            "wallet_debits": debit_rows,
            "profit_amount_total": profit_split_totals["profit_amount_total"],
            "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
            "admin_profit_total": profit_split_totals["admin_profit_total"],
            "store_profit_total": profit_split_totals["store_profit_total"],
            "status": "completed",
            "paid_from": "paystack_inline",
            "paystack_reference": ps_ref,
            "kind": "afa_registration",
            "created_at": now,
            "updated_at": now,
        }
        orders_col.update_one(
            {"order_id": order_id},
            {"$setOnInsert": order_doc},
            upsert=True,
        )
        _clear_dashboard_cache_safely()
        if store_profit_amount > 0:
            try:
                store_accounts_col.update_one(
                    {"store_slug": slug, "admin_id": store_admin_id},
                    {
                        "$inc": {"total_profit_balance": round(store_profit_amount, 2)},
                        "$set": {
                            "last_updated_profit": round(store_profit_amount, 2),
                            "updated_at": datetime.utcnow(),
                        },
                        "$setOnInsert": {
                            "store_slug": slug,
                            "admin_id": store_admin_id,
                            "created_at": datetime.utcnow(),
                        },
                    },
                    upsert=True,
                )
            except Exception:
                jlog("store_account_update_error", store_slug=slug)
        return jsonify(
            {
                "success": True,
                "message": "AFA registration submitted successfully.",
                "registration_id": str(reg_id),
                "price": expected_ghs,
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "agent_wallet_debit_total": agent_wallet_debit_total,
                "store_profit_amount": round(store_profit_amount, 2),
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


@stores_bp.route("/api/store-paystack-public-key/<slug>", methods=["GET"])
def api_store_paystack_public_key(slug: str):
    try:
        store_doc = stores_col.find_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"_id": 1, "admin_id": 1, "owner_id": 1},
        )
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        day_key = _utc_day_key()
        key_used = "default"
        store_admin_id = _store_admin_id(store_doc)
        pk, _ = _load_store_paystack_keys(admin_id=store_admin_id)
        pk = pk if _is_pk(pk) else ""
        global_secondary_used = 0

        if not pk:
            return jsonify({"success": False, "message": "Payment is not configured"}), 400

        try:
            jlog(
                "store_paystack_pk_selected",
                day=day_key,
                global_secondary_used=global_secondary_used,
                key_used=key_used,
                slug=slug,
            )
        except Exception:
            print(
                "[store_paystack_pk_selected]",
                "day=" + day_key,
                "global_secondary_used=" + str(global_secondary_used),
                "key_used=" + str(key_used),
                "slug=" + str(slug),
            )

        return jsonify(
            {
                "success": True,
                "public_key": pk,
                "key_used": key_used,
                "day_key": day_key,
                "global_secondary_used": global_secondary_used,
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500

def _paid_enough(paid_pesewas: int, expected_pesewas: int) -> bool:
    return int(paid_pesewas or 0) + 1 >= int(expected_pesewas or 0)


def _decimal_amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _round_half_up_pesewas(amount_ghs: float) -> int:
    amount = _decimal_amount(amount_ghs)
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _paystack_fee_inclusive_total(base_amount: float) -> Dict[str, Any]:
    base = _decimal_amount(base_amount)
    base_pes = _round_half_up_pesewas(base)
    fee_pes = int(
        (base * Decimal(str(PAYSTACK_INLINE_FEE_RATE)) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    total_pes = base_pes + fee_pes
    return {
        "base_ghs": round(base_pes / 100.0, 2),
        "fee_ghs": round(fee_pes / 100.0, 2),
        "gross_ghs": round(total_pes / 100.0, 2),
        "gross_pesewas": total_pes,
    }

DUP_WINDOW_MINUTES = 30

def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0

def _canonical_store_total_for_offer(
    store_doc: Dict[str, Any],
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    if not svc_doc:
        return None

    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    svc_id_str = str(svc_doc.get("_id"))
    per_entry = per_map.get(svc_id_str, {})
    svc_percent = per_entry.get("percent")

    if is_social_boosting_service(svc_doc):
        store_admin_id = _store_admin_id(store_doc or {})
        admin_level, owner_stage_label = _social_boosting_actor_context((store_doc or {}).get("owner_id"), store_admin_id)
        provider_service_id_int, quantity, social_offer, _social_value = _resolve_social_boosting_request(svc_doc, value_obj)
        if not social_offer or quantity <= 0 or provider_service_id_int is None:
            return None
        owner_rate_usd = _social_boosting_owner_rate_per_1000(
            social_offer,
            admin_level,
            store_admin_id,
            (store_doc or {}).get("owner_id"),
            owner_stage_label,
        )
        owner_rate_ghs = usd_to_ghs_rate(owner_rate_usd)
        pct = _pricing_percent_for_service(svc_doc.get("_id"), percent_default, per_map)
        store_rate_ghs = rate_money(owner_rate_ghs * (1 + (pct / 100.0)))
        return total_for_quantity(store_rate_ghs, quantity)

    offers = _svc_offers_list(svc_doc)
    if not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")

    for idx, of in enumerate(offers):
        parsed = _parse_value_field(of.get("value"))
        vol = _extract_volume(parsed, unit)
        if vol_needed is not None and vol is not None:
            diff = abs(float(vol) - float(vol_needed))
            if diff < best_diff:
                best_idx, best_diff = idx, diff
        elif best_idx is None:
            best_idx = idx

    if best_idx is None:
        return None

    base_amount = _offer_base_amount(offers[best_idx])
    if base_amount is None:
        return None

    offer_overrides = per_entry.get("offers") or {}
    if best_idx in offer_overrides:
        return round(float(offer_overrides[best_idx]), 2)

    explicit_store_price = _to_float(offers[best_idx].get("customer_price"))
    if explicit_store_price is not None:
        return round(float(explicit_store_price), 2)

    return None

def _canonical_store_base_for_offer(
    store_doc: Dict[str, Any],
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    if not svc_doc:
        return None

    if is_social_boosting_service(svc_doc):
        store_admin_id = _store_admin_id(store_doc or {})
        admin_level, owner_stage_label = _social_boosting_actor_context((store_doc or {}).get("owner_id"), store_admin_id)
        provider_service_id_int, quantity, social_offer, _social_value = _resolve_social_boosting_request(svc_doc, value_obj)
        if not social_offer or quantity <= 0 or provider_service_id_int is None:
            return None
        owner_rate_usd = _social_boosting_owner_rate_per_1000(
            social_offer,
            admin_level,
            store_admin_id,
            (store_doc or {}).get("owner_id"),
            owner_stage_label,
        )
        owner_rate_ghs = usd_to_ghs_rate(owner_rate_usd)
        return total_for_quantity(owner_rate_ghs, quantity)

    offers = _svc_offers_list(svc_doc)
    if not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")

    for idx, of in enumerate(offers):
        parsed = _parse_value_field(of.get("value"))
        vol = _extract_volume(parsed, unit)
        if vol_needed is not None and vol is not None:
            diff = abs(float(vol) - float(vol_needed))
            if diff < best_diff:
                best_idx, best_diff = idx, diff
        elif best_idx is None:
            best_idx = idx

    if best_idx is None:
        return None

    base_amount = _offer_base_amount(offers[best_idx])
    if base_amount is None:
        return None
    return round(float(base_amount), 2)

def _store_profit_percent_for_item(
    store_doc: Dict[str, Any],
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
    base_amount: float,
) -> float:
    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    if not svc_doc:
        return 0.0

    svc_id_str = str(svc_doc.get("_id") or "")
    per_entry = per_map.get(svc_id_str, {})
    svc_percent = per_entry.get("percent")
    if is_social_boosting_service(svc_doc):
        return _pricing_percent_for_service(svc_doc.get("_id"), percent_default, per_map)
    if svc_percent is not None:
        try:
            return float(svc_percent)
        except Exception:
            return 0.0

    offer_overrides = per_entry.get("offers") or {}
    if offer_overrides:
        offers = _svc_offers_list(svc_doc)
        unit = _service_unit(svc_doc)
        vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

        best_idx: Optional[int] = None
        best_diff = float("inf")
        for idx, of in enumerate(offers):
            parsed = _parse_value_field(of.get("value"))
            vol = _extract_volume(parsed, unit)
            if vol_needed is not None and vol is not None:
                diff = abs(float(vol) - float(vol_needed))
                if diff < best_diff:
                    best_idx, best_diff = idx, diff
            elif best_idx is None:
                best_idx = idx

        if best_idx is not None and best_idx in offer_overrides:
            override_total = _to_float(offer_overrides.get(best_idx))
            base = float(base_amount or 0.0)
            if base <= 0 and best_idx < len(offers):
                base = float(_offer_base_amount(offers[best_idx]) or 0.0)
            if override_total is not None and base > 0:
                return round(((float(override_total) - base) / base) * 100.0, 2)

    return 0.0

def _server_reprice_store_cart(
    store_doc: Dict[str, Any], cart: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], float]:
    revised: List[Dict[str, Any]] = []
    sys_total = 0.0
    store_admin_id = _store_admin_id(store_doc)
    afa_cfg = _normalize_store_afa_config(store_doc or {})
    checker_cfg = _normalize_store_checker_config(store_doc or {})
    bulk_sms_cfg = _normalize_store_bulk_sms_config(store_doc or {})
    for item in cart:
        if item.get("serviceId") in (None, "", []):
            alt = item.get("service_id")
            if alt not in (None, "", []):
                item["serviceId"] = alt
        if item.get("value_obj") in (None, "", []):
            alt = item.get("valueObj")
            if alt not in (None, "", []):
                item["value_obj"] = alt
        if item.get("base_amount") in (None, "", []):
            alt = item.get("baseAmount")
            if alt not in (None, "", []):
                item["base_amount"] = alt

        service_id_raw = item.get("serviceId")
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

        svc_doc: Optional[Dict[str, Any]] = None
        if service_id_raw:
            try:
                svc_oid = ObjectId(service_id_raw)
                svc_query: Dict[str, Any] = {"_id": svc_oid}
                if store_admin_id:
                    svc_query = {
                        "_id": svc_oid,
                        "$or": [
                            {"admin_id": store_admin_id},
                            {"_id": SOCIAL_BOOSTING_SERVICE_ID},
                        ],
                        "agent_visible": {"$ne": False},
                        "display_enabled": {"$ne": False},
                        f"agent_visibility_by_admin.{str(store_admin_id)}": {"$ne": False},
                    }
                svc_doc = services_col.find_one(
                    svc_query,
                    {
                        "offers": 1,
                        "store_offers": 1,
                        "services_offers": 1,
                        "unit": 1,
                        "name": 1,
                        "type": 1,
                        "provider": 1,
                        "base_service_id": 1,
                        "service_category": 1,
                        "default_profit_percent": 1,
                        "store_offers_profit": 1,
                        "status": 1,
                        "availability": 1,
                        "network_id": 1,
                        "network": 1,
                    },
                )
            except Exception:
                svc_doc = None

        is_afa = str(item.get("kind") or "").strip().lower() == "afa_registration"
        is_checker = str(item.get("kind") or "").strip().lower() == "results_checker"
        is_bulk_sms = str(item.get("kind") or "").strip().lower() == "bulk_sms_delivery"
        if is_afa and afa_cfg.get("enabled") and (afa_cfg.get("price") or 0) > 0:
            canonical = round(float(afa_cfg.get("price") or 0.0), 2)
            canonical_base = canonical
        elif is_checker and checker_cfg.get("enabled"):
            checker_type = normalize_checker_type(item.get("checker_type") or item.get("value"))
            type_cfg = next((x for x in (checker_cfg.get("active_types") or []) if x.get("key") == checker_type), None)
            canonical = round(float((type_cfg or {}).get("price") or 0.0), 2)
            canonical_base = _checker_profit_layers(store_doc or {}, checker_type, canonical)["store_owner_base_amount"]
        elif is_bulk_sms and bulk_sms_cfg.get("enabled"):
            recipients = _store_sms_recipients_from_item(item)
            count = len(recipients)
            canonical = round(float(bulk_sms_cfg.get("price_per_sms") or 0.0) * count, 2)
            canonical_base = round(float(bulk_sms_cfg.get("owner_price_per_sms") or 0.0) * count, 2)
            item["recipients"] = recipients
            item["recipient_count"] = count
        else:
            canonical = _canonical_store_total_for_offer(
                store_doc or {}, svc_doc or {}, value_obj, item.get("value")
            )
            canonical_base = _canonical_store_base_for_offer(
                store_doc or {}, svc_doc or {}, value_obj, item.get("value")
            )
        if canonical is None:
            canonical = 0.0
        if canonical_base is None:
            canonical_base = 0.0

        revised.append({**item, "amount": canonical, "base_amount": canonical_base})
        sys_total += canonical

    return revised, round(sys_total, 2)

def _resolve_network_group(svc_doc: Optional[Dict[str, Any]], svc_name: Optional[str] = None) -> str:
    if svc_doc:
        sn = (svc_doc.get("service_network") or "").strip().lower()
        if sn in ("mtn", "telecel", "airteltigo"):
            return sn
        nw = (svc_doc.get("network") or "").strip().lower()
        if "mtn" in nw:
            return "mtn"
        if "telecel" in nw or "vodafone" in nw:
            return "telecel"
        if "airteltigo" in nw or "ishare" in nw or "bigtime" in nw or nw.startswith("at"):
            return "airteltigo"
    name = (svc_name or "").strip().lower()
    if "mtn" in name:
        return "mtn"
    if "telecel" in name or "vodafone" in name:
        return "telecel"
    if "airteltigo" in name or "ishare" in name or "bigtime" in name or name.startswith("at"):
        return "airteltigo"
    return ""

def _extract_gh_prefix(phone: str) -> Optional[str]:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) == 10 and digits.startswith("0"):
        return digits[:3]
    if len(digits) == 12 and digits.startswith("233"):
        return "0" + digits[3:5]
    return None

def _normalize_gh_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    if len(digits) == 12 and digits.startswith("233"):
        return "0" + digits[3:]
    return digits

def _is_valid_gh_phone(raw: Any) -> bool:
    norm = _normalize_gh_phone(raw)
    if len(norm) != 10 or not norm.startswith("0"):
        return False
    prefix = norm[:3]
    allowed = {p for v in PORTED_PREFIXES.values() for p in v}
    return prefix in allowed


def _normalize_store_sms_recipient(raw: Any) -> Optional[str]:
    compact = re.sub(r"[\s().+-]+", "", str(raw or ""))
    if len(compact) == 10 and compact.startswith("0"):
        return "233" + compact[1:]
    if len(compact) == 9:
        return "233" + compact
    if len(compact) == 12 and compact.startswith("233"):
        return compact
    return None


def _store_sms_recipients_from_item(item: Dict[str, Any]) -> List[str]:
    raw = item.get("recipients")
    if not isinstance(raw, list):
        raw = []
    cleaned: List[str] = []
    seen = set()
    for value in raw:
        number = _normalize_store_sms_recipient(value)
        if number and number not in seen:
            seen.add(number)
            cleaned.append(number)
    return cleaned


# =====================================================================
# ✅ IMPORTANT FIX: Profit MUST be computed from SYSTEM offers (svc.offers)
# - base_amount = svc.offers[].amount
# - profit% = svc.store_offers_profit (fallback default_profit_percent)
# - profit = base_amount * profit%
# =====================================================================
def _system_offer_base_amount_from_service(
    store_doc: Dict[str, Any],
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    """
    ✅ System base amount must come from svc_doc.offers (NOT store_offers).
    We match closest offer by volume/minutes.
    """
    if not svc_doc:
        return None

    if is_social_boosting_service(svc_doc):
        store_admin_id = _store_admin_id(store_doc or {})
        admin_level, _owner_stage_label = _social_boosting_actor_context((store_doc or {}).get("owner_id"), store_admin_id)
        provider_service_id_int, quantity, social_offer, _social_value = _resolve_social_boosting_request(svc_doc, value_obj)
        if not social_offer or quantity <= 0 or provider_service_id_int is None:
            return None
        admin_rate_usd = admin_rate_per_1000(social_offer, admin_level)
        return total_for_quantity_ghs(admin_rate_usd, quantity)

    offers = svc_doc.get("offers")
    if not isinstance(offers, list) or not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")

    for idx, of in enumerate(offers):
        try:
            parsed = _parse_value_field(of.get("value"))
            vol = _extract_volume(parsed, unit)
            if vol_needed is not None and vol is not None:
                diff = abs(float(vol) - float(vol_needed))
                if diff < best_diff:
                    best_idx, best_diff = idx, diff
            elif best_idx is None:
                best_idx = idx
        except Exception:
            continue

    if best_idx is None:
        return None

    return _to_float((offers[best_idx] or {}).get("amount"))


def _main_base_amount_from_base_service(
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
    fallback: Any = 0.0,
) -> float:
    if not svc_doc:
        return ledger_money(fallback)
    base_id = svc_doc.get("base_service_id")
    if not isinstance(base_id, ObjectId):
        return ledger_money(fallback)
    try:
        base_doc = services_col.find_one({"_id": base_id}, {"offers": 1, "unit": 1, "name": 1})
    except Exception:
        base_doc = None
    main_base = _system_offer_base_amount_from_service({}, base_doc, value_obj, value_raw) if base_doc else None
    return ledger_money(main_base if main_base is not None else fallback)


def _admin_checker_price(store_doc: Dict[str, Any], checker_type: Any) -> float:
    checker_kind = normalize_checker_type(checker_type)
    admin_id = _store_admin_id(store_doc)
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}
    admin_level = normalize_admin_level((admin_doc or {}).get("admin_level"))
    pricing_doc = get_checker_pricing_doc(checker_kind)
    owner_price = _store_checker_owner_price(store_doc, checker_kind)
    return ledger_money(admin_stage_price(pricing_doc, admin_level, legacy_amount=owner_price) or owner_price)


def _checker_base_cost(checker_type: Any) -> float:
    return ledger_money(checker_base_cost(get_checker_pricing_doc(checker_type)))


def _checker_profit_layers(store_doc: Dict[str, Any], checker_type: Any, selling_amount: Any) -> Dict[str, float]:
    checker_kind = normalize_checker_type(checker_type)
    admin_price = _admin_checker_price(store_doc, checker_kind)
    owner_price = ledger_money(_store_checker_owner_price(store_doc, checker_kind) or admin_price)
    main_base = _checker_base_cost(checker_kind)
    selling = ledger_money(selling_amount)
    return {
        "main_base_amount": main_base,
        "admin_base_amount": admin_price,
        "store_owner_base_amount": owner_price,
        "selling_amount": selling,
        "store_profit_amount": max(0.0, round(selling - owner_price, 2)),
    }


def _admin_bulk_sms_price_per_number(store_doc: Dict[str, Any]) -> float:
    admin_id = _store_admin_id(store_doc)
    service = find_bulk_sms_service_for_admin(admin_id) or {}
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}
    level = normalize_admin_level((admin_doc or {}).get("admin_level"))
    prices = service.get("sms_admin_stage_prices") if isinstance(service.get("sms_admin_stage_prices"), dict) else {}
    price = _to_float(prices.get(level))
    if price is None and level != "admin":
        price = _to_float(prices.get("admin"))
    if price is None:
        price = _to_float(service.get("sms_price_per_number"))
    if price is None:
        price = _bulk_sms_owner_price(store_doc)
    return round(float(price or 0.0), 4)


def _main_bulk_sms_base_price_per_number(store_doc: Dict[str, Any]) -> float:
    admin_id = _store_admin_id(store_doc)
    service = find_bulk_sms_service_for_admin(admin_id) or {}
    price = _to_float(service.get("sms_base_price_per_number"))
    if price is None and service.get("base_service_id"):
        try:
            base_sms = services_col.find_one({"_id": service.get("base_service_id")}, {"sms_base_price_per_number": 1})
        except Exception:
            base_sms = None
        price = _to_float((base_sms or {}).get("sms_base_price_per_number"))
    return round(float(price or 0.0), 4)


def _finalize_store_profit_lines(lines: List[Dict[str, Any]], store_doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    finalized: List[Dict[str, Any]] = []
    for line in lines or []:
        selling = ledger_money(line.get("selling_amount") if line.get("selling_amount") is not None else line.get("amount"))
        existing_store_profit = line.get("store_profit_amount")
        store_owner_base = line.get("store_owner_base_amount")
        if store_owner_base in (None, ""):
            if existing_store_profit not in (None, ""):
                store_owner_base = max(0.0, round(selling - ledger_money(existing_store_profit), 2))
            else:
                store_owner_base = line.get("base_amount")

        admin_base = line.get("admin_base_amount")
        if admin_base in (None, ""):
            admin_base = max(0.0, round(ledger_money(store_owner_base) - ledger_money(line.get("profit_amount")), 2))

        main_base = line.get("main_base_amount")
        if main_base in (None, ""):
            if str(line.get("service_type") or "").upper() == "AFA":
                main_base = load_afa_base_price(default=ledger_money(admin_base))
            elif str(line.get("service_type") or "").upper() == "RESULTS_CHECKER":
                main_base = admin_base
            elif str(line.get("service_type") or "").upper() == "BULK_SMS":
                main_base = admin_base
            elif str(line.get("provider") or "").strip().lower() == SOCIAL_BOOSTING_PROVIDER:
                qty = int(_to_float(line.get("quantity")) or 0)
                rate = _to_float(line.get("base_rate_per_1000_ghs"))
                main_base = total_for_quantity(rate, qty) if rate is not None and qty > 0 else admin_base
            else:
                service_id = line.get("serviceId")
                svc_doc = None
                if service_id:
                    try:
                        svc_doc = services_col.find_one({"_id": ObjectId(service_id)}, {"base_service_id": 1, "offers": 1, "unit": 1, "name": 1})
                    except Exception:
                        svc_doc = None
                main_base = _main_base_amount_from_base_service(svc_doc, line.get("value_obj") or line.get("value"), line.get("value"), admin_base)

        normalized = normalize_profit_line(
            line,
            selling_amount=selling,
            main_base_amount=main_base,
            admin_base_amount=admin_base,
            store_owner_base_amount=store_owner_base,
            store_profit_amount=existing_store_profit,
        )
        finalized.append(apply_profit_split(normalized))
    return finalized, profit_totals(finalized)


def _log_store_profit_summary(event: str, order_doc: Dict[str, Any], totals: Dict[str, float]) -> None:
    try:
        items = order_doc.get("items") or []
        jlog(
            event,
            order_id=order_doc.get("order_id"),
            admin_id=str(order_doc.get("admin_id") or ""),
            user_id=str(order_doc.get("user_id") or ""),
            store_slug=order_doc.get("store_slug"),
            line_count=len(items),
            selling_total=round(sum(_money(it.get("selling_amount")) for it in items), 2),
            admin_base_total=round(sum(_money(it.get("admin_base_amount")) for it in items), 2),
            main_base_total=round(sum(_money(it.get("main_base_amount")) for it in items), 2),
            admin_profit_total=totals.get("admin_profit_total"),
            main_admin_profit_total=totals.get("main_admin_profit_total"),
            store_profit_total=order_doc.get("store_profit_total"),
        )
    except Exception:
        pass


@stores_bp.route("/store-checkout/<slug>", methods=["POST"])
def store_checkout_paystack(slug: str):
    body = request.get_json(silent=True) or {}
    return _store_checkout_handler(slug, body)


def admin_override_store_checkout(slug: str, cart: List[Dict[str, Any]], admin_ref: str = ""):
    body = {
        "cart": cart or [],
        "method": "admin_override",
        "paystack": {"reference": admin_ref or ""},
    }
    return _store_checkout_handler(slug, body)


def _load_admin_override_complaint(slug: str, complaint_id: str, admin_token: str = "") -> Optional[Dict[str, Any]]:
    token_payload = verify_admin_override_token(admin_token)
    try:
        complaint_oid = ObjectId(complaint_id)
    except Exception:
        return None

    query: Dict[str, Any] = {"_id": complaint_oid, "payment_confirmed": True}
    if token_payload:
        token_slug = str(token_payload.get("store_slug") or "").strip()
        if token_slug != (slug or "").strip():
            return None
        token_complaint_id = str(token_payload.get("complaint_id") or "").strip()
        if token_complaint_id != complaint_id:
            return None
        token_role = str(token_payload.get("actor_role") or "").strip().lower()
        token_admin_id = str(token_payload.get("admin_id") or "").strip()
        if token_role == "main_admin":
            query["sent_to_main_admin"] = True
        elif token_admin_id:
            try:
                query["admin_id"] = ObjectId(token_admin_id)
            except Exception:
                return None
        else:
            return None
    else:
        if not complaint_id or not is_admin_role(session.get("role")) or not session.get("user_id"):
            return None
        role = (session.get("role") or "").strip().lower()
        if role == "main_admin":
            query["sent_to_main_admin"] = True
        else:
            admin_oid = current_admin_id_from_session(session)
            if not admin_oid:
                return None
            query["admin_id"] = admin_oid

    complaint = complaints_col.find_one(query)
    if not complaint:
        return None
    complaint_slug = (complaint.get("store_slug") or slug or "").strip()
    if complaint_slug != slug:
        return None
    return complaint


def _admin_override_auth_ok(admin_token: str = "") -> bool:
    return bool(verify_admin_override_token(admin_token))


@stores_bp.route("/api/store-admin-complaints/<slug>/<complaint_id>/snapshot", methods=["GET"])
def store_admin_complaint_snapshot(slug: str, complaint_id: str):
    admin_token = (request.args.get("admin_token") or "").strip()
    complaint = _load_admin_override_complaint(slug, complaint_id, admin_token=admin_token)
    if not complaint:
        return jsonify({"success": False, "message": "Confirmed complaint not found or link expired"}), 403

    return jsonify(
        {
            "success": True,
            "store_slug": slug,
            "paystack_reference": complaint.get("paystack_reference"),
            "admin_override_reference": complaint.get("paystack_reference") or f"COMPLAINT-{complaint_id}",
            "customer_phone": complaint.get("customer_phone") or "",
            "customer_name": complaint.get("customer_name") or "",
            "cart_snapshot": complaint.get("cart_snapshot") or [],
            "can_process": True,
        }
    )


def _store_checkout_handler(slug: str, body: Dict[str, Any]):
    try:
        body = body or {}
        cart = body.get("cart") or []
        method = (body.get("method") or "paystack_inline").strip().lower()
        ps_info = body.get("paystack") or {}
        payment_info = body.get("payment") if isinstance(body.get("payment"), dict) else {}
        ps_ref = (ps_info.get("reference") or "").strip()
        complaint_id = (body.get("complaint_id") or "").strip()
        admin_token = (body.get("admin_token") or "").strip()
        admin_override_complaint = None
        payer_info = ps_info.get("payer") if isinstance(ps_info.get("payer"), dict) else {}
        if not payer_info and isinstance(payment_info.get("payer"), dict):
            payer_info = payment_info.get("payer") or {}
        payer_email = (payer_info.get("email") or ps_info.get("email") or "").strip().lower()
        payer_email_source = (payer_info.get("email_source") or "").strip().lower()
        payer_phone = (payer_info.get("phone") or "").strip()
        paystack_verified = False
        create_txn = True

        jlog("store_public_checkout_incoming", slug=slug, payload={"method": method, "has_ref": bool(ps_ref), "cart_len": len(cart) if isinstance(cart, list) else -1})

        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404
        store_admin_id = _store_admin_id(store_doc)

        if method == "admin_override":
            if not complaint_id:
                return jsonify({"success": False, "message": "Complaint ID is required for admin override"}), 403
            admin_override_complaint = _load_admin_override_complaint(slug, complaint_id, admin_token=admin_token)
            if not admin_override_complaint:
                return jsonify({"success": False, "message": "Confirmed complaint not found for this admin/store"}), 403
            complaint_cart = admin_override_complaint.get("cart_snapshot") or []
            if isinstance(complaint_cart, list) and complaint_cart:
                cart = complaint_cart
            if not ps_ref:
                ps_ref = (admin_override_complaint.get("paystack_reference") or f"COMPLAINT-{complaint_id}").strip()

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        # AFA lines (optional) must be enabled for this store
        afa_cfg = _normalize_store_afa_config(store_doc)
        checker_cfg = _normalize_store_checker_config(store_doc)
        bulk_sms_cfg = _normalize_store_bulk_sms_config(store_doc)
        afa_lines = [it for it in cart if isinstance(it, dict) and str(it.get("kind") or "").lower() == "afa_registration"]
        checker_lines = [it for it in cart if isinstance(it, dict) and str(it.get("kind") or "").lower() == "results_checker"]
        bulk_sms_lines = [it for it in cart if isinstance(it, dict) and str(it.get("kind") or "").lower() == "bulk_sms_delivery"]
        if afa_lines:
            if not afa_cfg.get("enabled") or (afa_cfg.get("price") or 0) <= 0:
                return jsonify({"success": False, "message": "AFA registration is not enabled for this store."}), 400
            if len(afa_lines) > 1:
                return jsonify({"success": False, "message": "Only one AFA registration can be paid per checkout."}), 400
            if not (afa_lines[0].get("name") or "").strip():
                return jsonify({"success": False, "message": "AFA name is required."}), 400
        if checker_lines:
            if not checker_cfg.get("enabled") or not (checker_cfg.get("active_types") or []):
                return jsonify({"success": False, "message": "Results checker is not enabled for this store."}), 400
            for idx, checker_line in enumerate(checker_lines, start=1):
                checker_type = normalize_checker_type(checker_line.get("checker_type") or checker_line.get("value"))
                type_cfg = next((x for x in (checker_cfg.get("active_types") or []) if x.get("key") == checker_type), None)
                if not type_cfg or (type_cfg.get("price") or 0) <= 0:
                    return jsonify({"success": False, "message": f"{checker_type.upper()} is not available for this store."}), 400
                if not _available_checker_stock(checker_type):
                    return jsonify({"success": False, "message": f"{checker_type.upper()} checker is currently out of stock."}), 400
        if bulk_sms_lines:
            if not bulk_sms_cfg.get("enabled") or (bulk_sms_cfg.get("price_per_sms") or 0) <= 0:
                return jsonify({"success": False, "message": "Bulk SMS is not enabled for this store."}), 400
            for idx, sms_line in enumerate(bulk_sms_lines, start=1):
                sender_name = re.sub(r"\s+", " ", str(sms_line.get("sender_name") or "").strip())
                if not sender_name:
                    return jsonify({"success": False, "message": f"Sender name is required for Bulk SMS line {idx}."}), 400
                message_body, message_error = validate_sms_message_body(sms_line.get("message_body") or sms_line.get("message"))
                if message_error:
                    return jsonify({"success": False, "message": f"{message_error} for Bulk SMS line {idx}."}), 400
                recipients = _store_sms_recipients_from_item(sms_line)
                if not recipients:
                    return jsonify({"success": False, "message": f"Add at least one valid recipient for Bulk SMS line {idx}."}), 400
                if len(recipients) > 1000:
                    return jsonify({"success": False, "message": f"Bulk SMS line {idx} has too many recipients. Maximum is 1000."}), 400
                sms_line["message_body"] = message_body
                sms_line["recipients"] = recipients
                sms_line["recipient_count"] = len(recipients)

        # Normalize and validate phones early (supports legacy field names)
        phone_fallback_keys = (
            "phone",
            "msisdn",
            "recipient",
            "number",
            "recipientPhone",
            "phone_number",
        )
        for idx, item in enumerate(cart):
            raw_phone = ""
            if isinstance(item, dict):
                for k in phone_fallback_keys:
                    val = item.get(k)
                    if val not in (None, ""):
                        raw_phone = val
                        break
            is_social_boosting = _is_social_boosting_cart_item(item)
            is_checker = isinstance(item, dict) and str(item.get("kind") or "").lower() == "results_checker"
            is_bulk_sms = isinstance(item, dict) and str(item.get("kind") or "").lower() == "bulk_sms_delivery"
            target_link = ""
            if isinstance(item, dict):
                value_obj_raw = item.get("value_obj")
                if value_obj_raw in (None, "", [], {}):
                    value_obj_raw = item.get("valueObj")
                if isinstance(value_obj_raw, dict):
                    target_link = str(value_obj_raw.get("link") or "").strip()
                if not target_link:
                    target_link = str(item.get("target_link") or "").strip()
            norm_phone = target_link if is_social_boosting else ("" if is_bulk_sms else _normalize_gh_phone(raw_phone))
            if isinstance(item, dict):
                item["phone"] = norm_phone
                if is_social_boosting and target_link:
                    item["target_link"] = target_link
            is_afa = isinstance(item, dict) and str(item.get("kind") or "").lower() == "afa_registration"
            if is_afa:
                if not re.match(r"^0\d{9}$", norm_phone or ""):
                    return jsonify(
                        {
                            "success": False,
                            "message": f"AFA phone must be 0xxxxxxxxx (line {idx + 1}).",
                            "line_index": idx,
                        }
                    ), 400
            elif is_checker:
                if not re.match(r"^0\d{9}$", norm_phone or ""):
                    return jsonify(
                        {
                            "success": False,
                            "message": f"Checker phone must be 0xxxxxxxxx (line {idx + 1}).",
                            "line_index": idx,
                        }
                    ), 400
            elif is_social_boosting:
                if not target_link:
                    return jsonify(
                        {
                            "success": False,
                            "message": f"Target link is required for boosting on line {idx + 1}.",
                            "line_index": idx,
                        }
                    ), 400
            elif is_bulk_sms:
                pass
            elif not _is_valid_gh_phone(norm_phone):
                return jsonify(
                    {
                        "success": False,
                        "message": f"Invalid phone number on line {idx + 1} (e.g. 0530393625).",
                        "line_index": idx,
                    }
                ), 400

        # idempotency: same reference should not create multiple orders
        if ps_ref:
            prior = orders_col.find_one({"store_slug": slug, "paystack_reference": ps_ref})
            if prior:
                prior_batch_id = prior.get("batch_id") or prior.get("order_id")
                prior_orders = list(orders_col.find(
                    {"store_slug": slug, "batch_id": prior_batch_id}
                ).sort("batch_position", 1)) if prior.get("batch_id") else [prior]
                prior_order_ids = [doc.get("order_id") for doc in prior_orders]
                prior_items = [item for doc in prior_orders for item in (doc.get("items") or [])]
                if admin_override_complaint:
                    try:
                        complaints_col.update_one(
                            {"_id": admin_override_complaint["_id"]},
                            {
                                "$set": {
                                    "store_order_id": prior_order_ids[0],
                                    "store_order_ids": prior_order_ids,
                                    "store_batch_id": prior_batch_id,
                                    "store_order_processed": True,
                                    "store_order_processed_at": datetime.utcnow(),
                                    "status": "resolved",
                                    "resolved_at": datetime.utcnow(),
                                    "updated_at": datetime.utcnow(),
                                }
                            },
                        )
                    except Exception:
                        pass
                return jsonify(
                    {
                        "success": True,
                        "message": f"Orders already created. Order IDs: {', '.join(prior_order_ids)}",
                        "order_id": prior_order_ids[0],
                        "order_ids": prior_order_ids,
                        "batch_id": prior_batch_id,
                        "status": prior.get("status"),
                        "charged_amount": round(sum(_money(doc.get("charged_amount")) for doc in prior_orders), 2),
                        "profit_amount_total": round(sum(_money(doc.get("profit_amount_total")) for doc in prior_orders), 2),
                        "items": prior_items,
                        "idempotent": True,
                        "redirect_url": url_for("admin_complaints.admin_view_complaints", status="resolved") if admin_override_complaint else url_for("checkout.invoice_batch_view", batch_id=prior_batch_id),
                    }
                ), 200

        # server-side repricing (prevents client tampering)
        cart, total_requested = _server_reprice_store_cart(store_doc, cart)
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        fee_delta_ghs = 0.0
        paystack_fee_ghs = 0.0
        paid_ghs = 0.0
        paystack_amounts = _paystack_fee_inclusive_total(total_requested)
        expected_pay_ghs = paystack_amounts["gross_ghs"]
        expected_pay_pes = paystack_amounts["gross_pesewas"]
        paystack_fee_ghs = paystack_amounts["fee_ghs"]

        if method == "admin_override":
            if not _admin_override_auth_ok(admin_token):
                return jsonify({"success": False, "message": "Admin access required"}), 403
            paystack_verified = True
            create_txn = False
            expected_pay_ghs = round(total_requested, 2)
            expected_pay_pes = int(round(expected_pay_ghs * 100))
            paid_ghs = expected_pay_ghs
            paystack_fee_ghs = 0.0
            if not ps_ref:
                ps_ref = "ADMIN-" + uuid.uuid4().hex[:12]
        elif method == "moolre":
            if not ps_ref:
                ps_ref = (payment_info.get("reference") or "").strip()
            if not ps_ref:
                return jsonify({"success": False, "message": "Payment reference is missing."}), 400
            paystack_verified = True
            create_txn = True
            expected_pay_ghs = round(float(payment_info.get("expected_amount_ghs") or expected_pay_ghs), 2)
            expected_pay_pes = int(round(expected_pay_ghs * 100))
            paid_ghs = expected_pay_ghs
            paystack_fee_ghs = round(float(payment_info.get("fee_ghs") or paystack_fee_ghs), 2)
            moolre_raw = payment_info.get("raw") or {}
            txn_user_id = ObjectId(session["user_id"]) if session.get("user_id") else store_doc.get("owner_id")
            txn_doc = {
                "user_id": txn_user_id,
                "admin_id": resolve_admin_id_for_user_id(users_col, txn_user_id)
                or store_doc.get("admin_id")
                or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id")),
                "amount": round(paid_ghs, 2),
                "reference": ps_ref,
                "status": "success",
                "type": "debit",
                "source": "store_checkout",
                "gateway": "Moolre",
                "currency": "GHS",
                "verified_at": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "raw": moolre_raw,
                "payment_provider": "moolre",
                "payment_reference": ps_ref,
                "payment_gateway": "Moolre",
                "payment_status": "success",
                "payment_verified_at": datetime.utcnow(),
                "payment_raw": moolre_raw,
                "meta": {
                    "store_checkout": True,
                    "store_slug": slug,
                    "payer_email": payer_email,
                    "payer_email_source": payer_email_source,
                    "payer_phone": payer_phone,
                    "expected_order_total_ghs": round(total_requested, 2),
                    "expected_pay_total_ghs": expected_pay_ghs,
                    "paid_total_ghs": paid_ghs,
                    "payment_provider": "moolre",
                    "moolre": moolre_raw,
                    "paystack_fee_rate": PAYSTACK_INLINE_FEE_RATE,
                    "paystack_fee_ghs": paystack_fee_ghs,
                    "gateway_fee_overage_ghs": 0.0,
                    "note": "Customer payment captured via Moolre checkout (server repriced).",
                    "paystack_profile": "store",
                },
            }
            if create_txn and not transactions_col.find_one({"reference": ps_ref, "status": "success"}):
                txn_result = transactions_col.insert_one(txn_doc)
                try:
                    record_admin_paystack_credit(
                        admin_id=txn_doc.get("admin_id") or store_admin_id,
                        amount=total_requested,
                        profile="store",
                        reference=ps_ref,
                        transaction_id=txn_result.inserted_id,
                        meta={
                            "source": "store_checkout",
                            "payment_provider": "moolre",
                            "moolre": moolre_raw,
                            "store_slug": slug,
                            "payer_phone": payer_phone,
                            "payer_email": payer_email,
                            "expected_order_total_ghs": round(total_requested, 2),
                            "expected_pay_total_ghs": expected_pay_ghs,
                            "paid_total_ghs": paid_ghs,
                            "paystack_credit_ghs": round(total_requested, 2),
                            "paystack_fee_rate": PAYSTACK_INLINE_FEE_RATE,
                            "paystack_fee_ghs": paystack_fee_ghs,
                            "gateway_fee_overage_ghs": 0.0,
                        },
                    )
                except Exception:
                    pass
        else:
            if method != "paystack_inline" or not ps_ref:
                return jsonify({"success": False, "message": "Payment missing. Please pay first."}), 400

            day_key = _utc_day_key()
            profile_used = "primary"
            secondary_count_before = None
            secondary_count_after = None

            ok, verify_data, verify_message, raw_verify = _verify_paystack(ps_ref, profile=profile_used, admin_id=store_admin_id)

            paid_pes = int(verify_data.get("amount") or 0) if isinstance(verify_data, dict) else 0
            paid_ghs = round(paid_pes / 100.0, 2) if paid_pes else 0.0
            currency = (verify_data.get("currency") or "").upper() if isinstance(verify_data, dict) else ""
            channel = verify_data.get("channel") if isinstance(verify_data, dict) else None

            if not ok:
                _insert_paystack_audit(
                    store_slug=slug,
                    order_id=None,
                    paystack_reference=ps_ref,
                    profile_used=profile_used,
                    verify_ok=False,
                    paid_ghs=paid_ghs or None,
                    expected_ghs=expected_pay_ghs,
                    gateway_fee_overage_ghs=None,
                    paystack_currency=(currency or None),
                    paystack_channel=channel,
                    response_message=verify_message,
                    raw_verify_data=raw_verify,
                    day_key=day_key,
                    secondary_count_after=secondary_count_after,
                )
                return jsonify({"success": False, "message": f"Payment verification failed: {verify_message}"}), 400
            paystack_verified = True

            paid_pes = int(verify_data.get("amount") or 0)
            paid_ghs = round(paid_pes / 100.0, 2)
            currency = (verify_data.get("currency") or "GHS").upper()
            if paid_pes <= 0 or currency != "GHS":
                _insert_paystack_audit(
                    store_slug=slug,
                    order_id=None,
                    paystack_reference=ps_ref,
                    profile_used=profile_used,
                    verify_ok=True,
                    paid_ghs=paid_ghs or None,
                    expected_ghs=expected_pay_ghs,
                    gateway_fee_overage_ghs=None,
                    paystack_currency=(currency or None),
                    paystack_channel=channel,
                    response_message=verify_message,
                    raw_verify_data=raw_verify,
                    day_key=day_key,
                    secondary_count_after=secondary_count_after,
                )
                return jsonify({"success": False, "message": "Invalid payment amount/currency."}), 400

            if not _paid_enough(paid_pes, expected_pay_pes):
                jlog(
                    "store_public_checkout_amount_underpaid",
                    slug=slug,
                    paid_pes=paid_pes,
                    expected_pes=expected_pay_pes,
                    paid_ghs=paid_ghs,
                    expected_ghs=expected_pay_ghs,
                )
                _insert_paystack_audit(
                    store_slug=slug,
                    order_id=None,
                    paystack_reference=ps_ref,
                    profile_used=profile_used,
                    verify_ok=True,
                    paid_ghs=paid_ghs or None,
                    expected_ghs=expected_pay_ghs,
                    gateway_fee_overage_ghs=None,
                    paystack_currency=(currency or None),
                    paystack_channel=channel,
                    response_message=verify_message,
                    raw_verify_data=raw_verify,
                    day_key=day_key,
                    secondary_count_after=secondary_count_after,
                )
                return jsonify(
                    {
                        "success": False,
                        "message": "Payment amount is below the 0.5% fee-inclusive total.",
                        "paid": paid_ghs,
                        "required": expected_pay_ghs,
                    }
                ), 400

            fee_delta_ghs = paystack_fee_ghs
            gateway_overage_ghs = max(0.0, round(paid_ghs - expected_pay_ghs, 2))

            _insert_paystack_audit(
                store_slug=slug,
                order_id=None,
                paystack_reference=ps_ref,
                profile_used=profile_used,
                verify_ok=True,
                paid_ghs=paid_ghs or None,
                expected_ghs=expected_pay_ghs,
                gateway_fee_overage_ghs=fee_delta_ghs,
                paystack_currency=(currency or None),
                paystack_channel=channel,
                response_message=verify_message,
                raw_verify_data=raw_verify,
                day_key=day_key,
                secondary_count_after=secondary_count_after,
            )

            # transaction doc (align with checkout.py)
            txn_user_id = ObjectId(session["user_id"]) if session.get("user_id") else store_doc.get("owner_id")
            txn_doc = {
                "user_id": txn_user_id,
                "admin_id": resolve_admin_id_for_user_id(users_col, txn_user_id)
                or store_doc.get("admin_id")
                or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id")),
                "amount": round(paid_ghs, 2),
                "reference": ps_ref,
                "status": "success",
                "type": "debit",
                "source": "paystack_inline",
                "gateway": "Paystack",
                "currency": "GHS",
                "channel": verify_data.get("channel"),
                "verified_at": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "raw": verify_data,
                "meta": {
                    "store_checkout": True,
                    "store_slug": slug,
                    "payer_email": payer_email,
                    "payer_email_source": payer_email_source,
                    "payer_phone": payer_phone,
                    "expected_order_total_ghs": round(total_requested, 2),
                    "expected_pay_total_ghs": expected_pay_ghs,
                    "paid_total_ghs": paid_ghs,
                    "paystack_fee_rate": PAYSTACK_INLINE_FEE_RATE,
                    "paystack_fee_ghs": paystack_fee_ghs,
                    "gateway_fee_overage_ghs": gateway_overage_ghs,
                    "note": "Customer payment captured via store inline checkout (server repriced).",
                    "paystack_profile": "store",
                },
            }

            if create_txn and not transactions_col.find_one({"reference": ps_ref, "source": "paystack_inline", "status": "success"}):
                if _checkout_helpers.get("txn_fn"):
                    try:
                        txn_result = _checkout_helpers["txn_fn"](transactions_col, txn_doc)
                    except Exception:
                        txn_result = transactions_col.insert_one(txn_doc)
                else:
                    txn_result = transactions_col.insert_one(txn_doc)
                try:
                    record_admin_paystack_credit(
                        admin_id=txn_doc.get("admin_id") or store_admin_id,
                        amount=total_requested,
                        profile="store",
                        reference=ps_ref,
                        transaction_id=(txn_result.inserted_id if txn_result and getattr(txn_result, "inserted_id", None) else None),
                        meta={
                            "source": "store_checkout",
                            "store_slug": slug,
                            "payer_phone": payer_phone,
                            "payer_email": payer_email,
                            "expected_order_total_ghs": round(total_requested, 2),
                            "expected_pay_total_ghs": expected_pay_ghs,
                            "paid_total_ghs": paid_ghs,
                            "paystack_credit_ghs": round(total_requested, 2),
                            "paystack_fee_rate": PAYSTACK_INLINE_FEE_RATE,
                            "paystack_fee_ghs": paystack_fee_ghs,
                            "gateway_fee_overage_ghs": gateway_overage_ghs,
                        },
                    )
                except Exception:
                    pass

        paid_from = "admin_complaint" if method == "admin_override" else ("moolre" if method == "moolre" else "paystack_inline")

        order_id = generate_order_id()
        results: List[Dict[str, Any]] = []
        debug_events: List[Dict[str, Any]] = []

        profit_amount_total = 0.0
        total_processing_amount = 0.0
        api_requested_total = 0.0
        seen_keys = set()
        api_jobs: List[Dict[str, Any]] = []
        codecraft_regular_map = None
        codecraft_bigtime_map = None

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)
            ported_fields = _extract_ported_fields(item)

            service_id_raw = item.get("serviceId")
            svc_doc: Optional[Dict[str, Any]] = None
            svc_type: Optional[str] = None
            svc_name = item.get("serviceName") or None
            svc_provider = ""

            is_afa = str(item.get("kind") or "").strip().lower() == "afa_registration"
            is_checker = str(item.get("kind") or "").strip().lower() == "results_checker"
            is_bulk_sms = str(item.get("kind") or "").strip().lower() == "bulk_sms_delivery"

            if service_id_raw:
                try:
                    svc_oid = ObjectId(service_id_raw)
                    svc_query: Dict[str, Any] = {"_id": svc_oid}
                    if store_admin_id:
                        svc_query = {
                            "_id": svc_oid,
                            "$or": [
                                {"admin_id": store_admin_id},
                                {"_id": SOCIAL_BOOSTING_SERVICE_ID},
                            ],
                            "agent_visible": {"$ne": False},
                            "display_enabled": {"$ne": False},
                            f"agent_visibility_by_admin.{str(store_admin_id)}": {"$ne": False},
                        }
                    svc_doc = services_col.find_one(
                        svc_query,
                        {
                            "type": 1,
                            "provider": 1,
                            "base_service_id": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "service_network": 1,
                            "offers": 1,
                            "store_offers": 1,
                            "services_offers": 1,
                            "base_service_id": 1,
                            "store_offers_profit": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "status": 1,
                            "availability": 1,
                            "unit": 1,
                        },
                    )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = st.strip().upper() if isinstance(st, str) else st
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            if service_id_raw and not svc_doc and not is_afa and not is_checker:
                return jsonify(
                    {
                        "success": False,
                        "message": "This service is no longer available.",
                        "unavailable": {
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "reason": "This service is no longer available.",
                        },
                    }
                ), 400

            svc_name_norm = (svc_name or "").strip().lower()
            is_mtn_express_name = svc_name_norm == "mtn express"

            # MTN NORMAL / MTN EXPRESS: refresh live provider/type/flag at checkout time
            if service_id_raw and (_is_mtn_normal_service(service_id_raw, svc_doc) or is_mtn_express_name):
                try:
                    live_doc = services_col.find_one(
                        {
                            "_id": ObjectId(service_id_raw),
                            "admin_id": store_admin_id,
                            "agent_visible": {"$ne": False},
                            "display_enabled": {"$ne": False},
                            f"agent_visibility_by_admin.{str(store_admin_id)}": {"$ne": False},
                        },
                        {
                            "type": 1,
                            "provider": 1,
                            "mtn_normal_use_portal02": 1,
                            "mtn_express_use_portal02": 1,
                            "name": 1,
                        },
                    )
                except Exception:
                    live_doc = None
                if live_doc:
                    if not svc_doc:
                        svc_doc = live_doc
                    else:
                        for k in (
                            "type",
                            "provider",
                            "mtn_normal_use_portal02",
                            "mtn_express_use_portal02",
                            "name",
                        ):
                            if k in live_doc:
                                svc_doc[k] = live_doc.get(k)
                    if live_doc.get("name"):
                        svc_name = live_doc.get("name") or svc_name
                    st = live_doc.get("type")
                    if isinstance(st, str):
                        svc_type = st.strip().upper()
                    elif st is not None:
                        svc_type = st

            if svc_doc and svc_doc.get("provider"):
                svc_provider = str(svc_doc.get("provider") or "").strip().lower()
            elif item.get("provider"):
                svc_provider = str(item.get("provider") or "").strip().lower()

            if not is_afa and not is_checker and not is_bulk_sms:
                svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
                is_unavail, reason_text = _service_unavailability_reason(svc_doc)
                if is_unavail and svc_type_flag == "OFF":
                    is_unavail = False
                if is_unavail:
                    return jsonify(
                        {
                            "success": False,
                            "message": reason_text,
                            "unavailable": {"serviceId": service_id_raw, "serviceName": svc_name, "reason": reason_text},
                        }
                    ), 400

            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

            if is_afa:
                expected_ghs = round(float(afa_cfg.get("price") or 0.0), 2)
                afa_layers = _afa_profit_layers(store_admin_id, expected_ghs)
                admin_afa_price = afa_layers["store_owner_base_amount"]
                store_profit_amount = afa_layers["store_profit_amount"]
                if amt_total < expected_ghs:
                    return jsonify({"success": False, "message": "AFA amount is less than required."}), 400

                name = (item.get("name") or "").strip()
                if not name:
                    return jsonify({"success": False, "message": "AFA name is required."}), 400

                now = datetime.utcnow()
                existing = afa_col.find_one({"store_slug": slug, "paystack_reference": ps_ref}, {"_id": 1})
                if not existing:
                    afa_admin_id = store_doc.get("admin_id") or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id"))
                    reg_doc: Dict[str, Any] = {
                        "store_slug": slug,
                        "store_owner_id": store_doc.get("owner_id"),
                        "admin_id": afa_admin_id,
                        "source": "store_checkout_paystack",
                        "paystack_reference": ps_ref,
                        "status": "pending",
                        "charged": True,
                        "amount": expected_ghs,
                        "charged_amount": expected_ghs,
                        "paystack_paid_amount": expected_ghs,
                        "charged_at": now,
                        "charged_by": "store_checkout",
                        "name": name,
                        "phone": phone,
                        "dob": (item.get("dob") or None),
                        "location": (item.get("location") or None),
                        "ghana_card": (item.get("ghana_card") or None),
                        "created_at": now,
                        "updated_at": now,
                    }
                    if session.get("role") in {"customer", "agent"} and session.get("user_id"):
                        try:
                            reg_doc["customer_id"] = ObjectId(session["user_id"])
                        except Exception:
                            pass
                    afa_col.insert_one(reg_doc)

                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": afa_layers["admin_base_amount"],
                        "main_base_amount": afa_layers["main_base_amount"],
                        "admin_base_amount": afa_layers["admin_base_amount"],
                        "store_owner_base_amount": afa_layers["store_owner_base_amount"],
                        "selling_amount": expected_ghs,
                        "amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": "AFA Registration",
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name or "AFA Registration",
                        "service_type": "AFA",
                        "network_id": None,
                        "bundle_key": None,
                        "line_amount_key": amount_key,
                        **({"store_profit_amount": store_profit_amount} if paystack_verified else {}),
                        "line_status": "completed",
                        "api_status": "not_applicable",
                        "api_response": {"note": "AFA registration recorded."},
                    }
                )
                continue

            if is_checker:
                checker_type = normalize_checker_type(item.get("checker_type") or item.get("value"))
                type_cfg = next((x for x in (checker_cfg.get("active_types") or []) if x.get("key") == checker_type), None)
                expected_ghs = round(float((type_cfg or {}).get("price") or 0.0), 2)
                checker_layers = _checker_profit_layers(store_doc, checker_type, expected_ghs)
                owner_price = checker_layers["store_owner_base_amount"]
                admin_checker_price = checker_layers["admin_base_amount"]
                if expected_ghs <= 0:
                    return jsonify({"success": False, "message": f"{checker_type.upper()} checker price is not configured."}), 400
                if amt_total < expected_ghs:
                    return jsonify({"success": False, "message": f"{checker_type.upper()} checker amount is less than required."}), 400

                checker_stock = _available_checker_stock(checker_type)
                if not checker_stock:
                    return jsonify({"success": False, "message": f"{checker_type.upper()} checker is out of stock."}), 400

                normalized_sms_phone = normalize_ghana_sms_phone(phone or "")
                if not normalized_sms_phone:
                    return jsonify({"success": False, "message": "Enter a valid phone number for checker SMS delivery."}), 400

                stock_update = checker_stock_col.update_one(
                    {"_id": checker_stock["_id"], "status": "not_sold"},
                    {
                        "$set": {
                            "status": "sold",
                            "sold_at": datetime.utcnow(),
                            "delivery_phone": normalized_sms_phone,
                            "store_slug": slug,
                            "paystack_reference": ps_ref,
                            "sold_to": str(session.get("user_id") or store_doc.get("owner_id") or ""),
                        }
                    },
                )
                if not stock_update.modified_count:
                    return jsonify({"success": False, "message": f"{checker_type.upper()} checker just sold out. Please try again."}), 400
                sender_name = resolve_admin_sender_name(store_admin_id)
                sms_status = send_sms(
                    normalized_sms_phone,
                    _checker_sms_message(checker_stock, sender_name),
                    sender_id=sender_name,
                )
                checker_base_cost_ghs = checker_layers["main_base_amount"]
                checker_profit_amount = max(0.0, round(expected_ghs - checker_base_cost_ghs, 2))
                purchase_history_col.insert_one(
                    {
                        "user_id": str(session.get("user_id") or ""),
                        "admin_id": store_admin_id,
                        "checker_id": str(checker_stock["_id"]),
                        "type": checker_type,
                        "amount": expected_ghs,
                        "base_cost_ghs": checker_base_cost_ghs,
                        "profit_amount": checker_profit_amount,
                        "message": checker_stock.get("message", ""),
                        "delivery_phone": normalized_sms_phone,
                        "sms_delivery_status": sms_status,
                        "store_slug": slug,
                        "paystack_reference": ps_ref,
                        "purchased_at": datetime.utcnow(),
                        "pricing_meta": {
                            "source": "store_checkout",
                            "store_owner_price": owner_price,
                            "store_sell_price": expected_ghs,
                            "base_cost_ghs": checker_base_cost_ghs,
                            "profit_amount": checker_profit_amount,
                        },
                    }
                )

                store_profit_amount = checker_layers["store_profit_amount"]
                profit_amount = max(0.0, round(owner_price - admin_checker_price, 2))
                total_processing_amount += expected_ghs
                results.append(
                    {
                        "phone": phone,
                        "base_amount": admin_checker_price,
                        "main_base_amount": checker_layers["main_base_amount"],
                        "admin_base_amount": admin_checker_price,
                        "store_owner_base_amount": owner_price,
                        "selling_amount": expected_ghs,
                        "amount": expected_ghs,
                        "profit_amount": profit_amount,
                        "profit_percent_used": 0.0,
                        "value": checker_type.upper(),
                        "value_obj": {"type": "results_checker", "checker_type": checker_type},
                        "serviceId": None,
                        "serviceName": f"{checker_type.upper()} Results Checker",
                        "service_type": "RESULTS_CHECKER",
                        "network_id": None,
                        "bundle_key": None,
                        "line_amount_key": amount_key,
                        **({"store_profit_amount": store_profit_amount} if paystack_verified else {}),
                        "line_status": "completed",
                        "api_status": "not_applicable",
                        "api_response": {
                            "note": "Checker fulfilled and sent by SMS.",
                            "sms_delivery_status": sms_status,
                        },
                    }
                )
                continue

            if is_bulk_sms:
                expected_price = round(float(bulk_sms_cfg.get("price_per_sms") or 0.0), 4)
                owner_price = round(float(bulk_sms_cfg.get("owner_price_per_sms") or 0.0), 4)
                recipients = _store_sms_recipients_from_item(item)
                sender_name = re.sub(r"\s+", " ", str(item.get("sender_name") or "").strip())
                message_body, message_error = validate_sms_message_body(item.get("message_body") or item.get("message"))
                if not bulk_sms_cfg.get("enabled") or expected_price <= 0 or owner_price <= 0:
                    return jsonify({"success": False, "message": "Bulk SMS is not enabled for this store."}), 400
                if not sender_name:
                    return jsonify({"success": False, "message": "Sender name is required for Bulk SMS."}), 400
                if message_error:
                    return jsonify({"success": False, "message": message_error}), 400
                if not recipients:
                    return jsonify({"success": False, "message": "Add at least one valid recipient for Bulk SMS."}), 400

                expected_ghs = round(expected_price * len(recipients), 2)
                base_amount = round(owner_price * len(recipients), 2)
                admin_sms_price = _admin_bulk_sms_price_per_number(store_doc)
                admin_sms_base = round(admin_sms_price * len(recipients), 2)
                main_sms_base_price = _main_bulk_sms_base_price_per_number(store_doc)
                main_sms_base = round(main_sms_base_price * len(recipients), 2)
                if amt_total < expected_ghs:
                    return jsonify({"success": False, "message": "Bulk SMS amount is less than required."}), 400

                delivery_doc = {
                    "reference": f"{ps_ref or order_id}-SMS-{idx}",
                    "admin_id": store_admin_id,
                    "user_id": store_doc.get("owner_id"),
                    "store_owner_id": store_doc.get("owner_id"),
                    "user_role": "store_buyer",
                    "customer_name": payer_email or payer_phone or "Store buyer",
                    "customer_username": "",
                    "customer_phone": payer_phone or "",
                    "service_id": None,
                    "service_name": "Bulk SMS",
                    "sender_name": sender_name,
                    "message_body": message_body,
                    "recipients": [{"number": number, "original": number} for number in recipients],
                    "recipient_count": len(recipients),
                    "price_per_number": expected_price,
                    "owner_price_per_number": owner_price,
                    "admin_price_per_number": admin_sms_price,
                    "main_base_price_per_number": main_sms_base_price,
                    "total_amount": expected_ghs,
                    "base_amount": base_amount,
                    "admin_base_amount": admin_sms_base,
                    "main_base_amount": main_sms_base,
                    "main_admin_profit_amount": max(0.0, round(admin_sms_base - main_sms_base, 2)),
                    "admin_profit_amount": max(0.0, round(base_amount - admin_sms_base, 2)),
                    "currency": "GHS",
                    "status": "pending",
                    "delivery_status": "pending",
                    "source": "store_checkout",
                    "store_slug": slug,
                    "paystack_reference": ps_ref,
                    "disclaimer_accepted": True,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
                delivery_insert = bulk_sms_deliveries_col.insert_one(delivery_doc)
                delivery_doc["_id"] = delivery_insert.inserted_id
                send_result = dispatch_bulk_sms_delivery(delivery_doc)
                delivery_status = str(send_result.get("delivery_status") or "pending").strip().lower()
                provider_status = str(send_result.get("provider_status") or "").strip()
                provider_message = str(send_result.get("provider_message") or "").strip()
                line_status = "delivered" if delivery_status == "delivered" else "failed"
                api_status = "success" if delivery_status == "delivered" else "failed"

                store_profit_amount = max(0.0, round(expected_ghs - base_amount, 2))
                profit_amount = max(0.0, round(base_amount - admin_sms_base, 2))
                total_processing_amount += expected_ghs
                results.append(
                    {
                        "phone": f"{len(recipients)} recipients",
                        "base_amount": base_amount,
                        "main_base_amount": main_sms_base,
                        "admin_base_amount": admin_sms_base,
                        "store_owner_base_amount": base_amount,
                        "selling_amount": expected_ghs,
                        "amount": expected_ghs,
                        "profit_amount": profit_amount,
                        "profit_percent_used": 0.0,
                        "value": f"{len(recipients)} SMS",
                        "value_obj": {
                            "type": "bulk_sms_delivery",
                            "sender_name": sender_name,
                            "message_body": message_body,
                            "recipients": recipients,
                            "recipient_count": len(recipients),
                        },
                        "serviceId": None,
                        "serviceName": "Bulk SMS",
                        "service_type": "BULK_SMS",
                        "network_id": None,
                        "bundle_key": None,
                        "line_amount_key": amount_key,
                        **({"store_profit_amount": store_profit_amount} if paystack_verified else {}),
                        "line_status": line_status,
                        "api_status": api_status,
                        "api_response": {
                            "note": (
                                "Bulk SMS sent successfully."
                                if delivery_status == "delivered"
                                else (provider_message or "Bulk SMS delivery failed.")
                            ),
                            "delivery_id": str(delivery_insert.inserted_id),
                            "delivery_status": delivery_status,
                            "provider_status": provider_status,
                            "provider_message": provider_message,
                        },
                    }
                )
                continue

            if _is_social_boosting_cart_item(item) or is_social_boosting_service(svc_doc or service_id_raw):
                target_link = (
                    str(item.get("target_link") or "").strip()
                    or (value_obj.get("link") if isinstance(value_obj, dict) else "")
                    or phone
                    or ""
                ).strip()
                provider_service_id_int, quantity, social_offer, social_value = _resolve_social_boosting_request(svc_doc, value_obj, item)
                if not target_link or not provider_service_id_int or not quantity or not social_offer:
                    return jsonify(
                        {
                            "success": False,
                            "message": "Boosting order is missing target link, quantity, or service selection.",
                        }
                    ), 400

                requires_custom_comments = offer_requires_custom_comments(social_offer)
                social_comments = normalize_custom_comments(item.get("comments") if isinstance(item, dict) else None)
                if not social_comments:
                    social_comments = normalize_custom_comments(social_value)
                if requires_custom_comments:
                    if not social_comments:
                        return jsonify(
                            {
                                "success": False,
                                "message": f"Custom comments are required for {social_offer.get('name') or 'this boosting service'}. Enter one comment per line.",
                            }
                        ), 400
                    quantity = len(social_comments)

                min_qty = _to_float(social_offer.get("min"))
                max_qty = _to_float(social_offer.get("max"))
                if (min_qty is not None and quantity < int(min_qty)) or (max_qty is not None and quantity > int(max_qty)):
                    return jsonify(
                        {
                            "success": False,
                            "message": f"Quantity for {social_offer.get('name') or 'this service'} must be between {social_offer.get('min')} and {social_offer.get('max')}.",
                        }
                    ), 400

                admin_level, owner_stage_label = _social_boosting_actor_context(store_doc.get("owner_id"), store_admin_id)
                provider_rate_usd = float(service_rate_per_1000(social_offer))
                admin_rate_usd = admin_rate_per_1000(social_offer, admin_level)
                owner_rate_usd = _social_boosting_owner_rate_per_1000(
                    social_offer,
                    admin_level,
                    store_admin_id,
                    store_doc.get("owner_id"),
                    owner_stage_label,
                )
                owner_rate_ghs = usd_to_ghs_rate(owner_rate_usd)
                admin_rate_ghs = usd_to_ghs_rate(admin_rate_usd)
                provider_rate_ghs = usd_to_ghs_rate(provider_rate_usd)

                base_amount = round(float(_to_float(item.get("base_amount")) or 0.0), 2)
                if base_amount <= 0:
                    canonical_base = _canonical_store_base_for_offer(store_doc or {}, svc_doc or {}, value_obj, item.get("value"))
                    base_amount = round(float(canonical_base or 0.0), 2)
                if amt_total <= 0:
                    canonical_total = _canonical_store_total_for_offer(store_doc or {}, svc_doc or {}, value_obj, item.get("value"))
                    amt_total = round(float(canonical_total or 0.0), 2)
                amount_key = _normalize_amount_key(amt_total)

                store_profit_percent = _store_profit_percent_for_item(
                    store_doc,
                    svc_doc,
                    value_obj,
                    item.get("value"),
                    base_amount,
                )
                store_rate_usd = rate_money(owner_rate_usd * (1 + (store_profit_percent / 100.0)))
                amount_usd = total_for_quantity(store_rate_usd, quantity)
                base_amount_usd = total_for_quantity(owner_rate_usd, quantity)
                system_offer_base = total_for_quantity_ghs(admin_rate_usd, quantity)
                system_offer_base_usd = total_for_quantity(admin_rate_usd, quantity)
                main_offer_base = total_for_quantity(provider_rate_ghs, quantity)
                profit_amount = max(0.0, round(base_amount - float(system_offer_base or 0.0), 2))
                profit_percent_used = round((profit_amount / float(system_offer_base)) * 100.0, 2) if system_offer_base else 0.0
                profit_amount_usd = max(0.0, round(base_amount_usd - float(system_offer_base_usd or 0.0), 2))
                profit_amount_total += profit_amount
                store_profit_amount = max(0.0, round(amt_total - base_amount, 2))
                store_profit_field = {"store_profit_amount": store_profit_amount} if paystack_verified else {}

                display_service_name = (
                    (item.get("serviceName") or "").strip()
                    or ((social_offer.get("social_media") or "").strip() + " Boosting").strip()
                    or "Boosting"
                )
                social_value_obj = {
                    "social_boosting": True,
                    "provider_service_id": provider_service_id_int,
                    "quantity": quantity,
                    "link": target_link,
                    "offer_type": social_offer.get("type") or "",
                    "requires_custom_comments": requires_custom_comments,
                    "comments": social_comments,
                    "comments_text": custom_comments_text(social_comments),
                    "comments_count": len(social_comments),
                    "social_media": social_offer.get("social_media") or "",
                    "category": social_offer.get("category") or "",
                    "rate_per_1000": rate_money(owner_rate_ghs * (1 + (store_profit_percent / 100.0))),
                    "rate_per_1000_ghs": rate_money(owner_rate_ghs * (1 + (store_profit_percent / 100.0))),
                    "rate_per_1000_usd": store_rate_usd,
                    "store_base_rate_per_1000": owner_rate_ghs,
                    "store_base_rate_per_1000_ghs": owner_rate_ghs,
                    "store_base_rate_per_1000_usd": owner_rate_usd,
                    "admin_rate_per_1000": admin_rate_ghs,
                    "admin_rate_per_1000_ghs": admin_rate_ghs,
                    "admin_rate_per_1000_usd": admin_rate_usd,
                    "base_rate_per_1000": provider_rate_ghs,
                    "base_rate_per_1000_ghs": provider_rate_ghs,
                    "base_rate_per_1000_usd": provider_rate_usd,
                    "currency": "USD",
                    "display_currency": "GHS",
                    "usd_to_ghs_rate": 11.01,
                }

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": target_link,
                        "target_link": target_link,
                        "quantity": quantity,
                        "base_amount": base_amount,
                        "main_base_amount": main_offer_base,
                        "admin_base_amount": system_offer_base,
                        "store_owner_base_amount": base_amount,
                        "selling_amount": amt_total,
                        "base_amount_usd": base_amount_usd,
                        "amount": amt_total,
                        "amount_usd": amount_usd,
                        "profit_amount": profit_amount,
                        "profit_amount_usd": profit_amount_usd,
                        "profit_percent_used": profit_percent_used,
                        **store_profit_field,
                        "value": social_offer.get("name") or item.get("value"),
                        "value_obj": social_value_obj,
                        "serviceId": service_id_raw or str(SOCIAL_BOOSTING_SERVICE_ID),
                        "serviceName": display_service_name,
                        "service_type": svc_type,
                        "provider": SOCIAL_BOOSTING_PROVIDER,
                        "currency": "GHS",
                        "provider_currency": "USD",
                        "usd_to_ghs_rate": 11.01,
                        "provider_service_id": provider_service_id_int,
                        "provider_reference": None,
                        "provider_order_id": None,
                        "provider_request_order_id": external_ref,
                        "social_media": social_offer.get("social_media") or "",
                        "category": social_offer.get("category") or "",
                        "comments_count": len(social_comments),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "queued",
                        "api_response": {"note": "Queued for ExoSupplier background API call"},
                    }
                )
                api_jobs.append(
                    {
                        "provider_request_order_id": external_ref,
                        "provider": SOCIAL_BOOSTING_PROVIDER,
                        "provider_service_id": provider_service_id_int,
                        "link": target_link,
                        "quantity": quantity,
                        "comments": social_comments,
                        "service_id": svc_doc["_id"] if svc_doc and svc_doc.get("_id") else SOCIAL_BOOSTING_SERVICE_ID,
                        "raw_item": item,
                        "line_index": idx,
                    }
                )
                continue

            if not is_afa and not is_checker:
                network_group = _resolve_network_group(svc_doc, svc_name)
                detected_prefix = _extract_gh_prefix(phone)
                expected_prefixes = PORTED_PREFIXES.get(network_group, [])
                ported_confirmed = bool(item.get("ported_confirmed"))
                if expected_prefixes and detected_prefix and detected_prefix not in expected_prefixes:
                    if not ported_confirmed:
                        return jsonify(
                            {
                                "success": False,
                                "message": "Number prefix does not match selected network. Confirm ported number to proceed.",
                                "needs_ported_confirm": True,
                                "line_index": idx - 1,
                                "network_group": network_group,
                                "detected_prefix": detected_prefix,
                                "expected_prefixes": expected_prefixes,
                            }
                        ), 400

            system_offer_base = _system_offer_base_amount_from_service(store_doc, svc_doc, value_obj, item.get("value"))
            base_amount = round(float(_to_float(item.get("base_amount")) or 0.0), 2)
            profit_amount = 0.0
            profit_percent_used = 0.0
            if system_offer_base is not None and base_amount > 0:
                profit_amount = max(0.0, round(base_amount - float(system_offer_base), 2))
                if system_offer_base > 0:
                    profit_percent_used = round((profit_amount / float(system_offer_base)) * 100.0, 2)
            profit_amount_total += profit_amount
            store_profit_percent = _store_profit_percent_for_item(
                store_doc, svc_doc, value_obj, item.get("value"), base_amount
            )
            store_profit_amount = max(0.0, round(amt_total - base_amount, 2))
            store_profit_field = {"store_profit_amount": store_profit_amount} if paystack_verified else {}

            network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None

            bundle_key = _build_bundle_key(value_obj, item)

            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), bundle_key[1], bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                            "amount": 0.0,
                            "originally_requested_amount": amt_total,
                            "profit_amount": 0.0,
                            "profit_percent_used": 0.0,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {"note": "Duplicate line in this cart (same number, network, bundle, amount)"},
                        }
                    )
                    continue
                seen_keys.add(cart_key)

            is_dup_strict = _has_processing_conflict_strict(
                phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
            )
            if is_dup_strict:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        **ported_fields,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            svc_name_norm = (svc_name or "").strip().lower()
            is_mtn_normal = (svc_name_norm == "mtn normal") or _is_mtn_normal_service(service_id_raw, svc_doc)
            is_mtn_express = (svc_name_norm == "mtn express")

            if is_mtn_normal or is_mtn_express:
                svc_provider = str((svc_doc or {}).get("provider") or "").strip().lower()
                if svc_type_flag == "OFF":
                    jlog(
                        "store_mtn_service_disabled",
                        order_id=order_id,
                        idx=idx,
                        serviceId=service_id_raw,
                        serviceName=svc_name,
                    )
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": (svc_type if svc_type else "unknown"),
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "not_applicable_type_off",
                            "api_response": {
                                "note": "Service type is OFF (API disabled). Order recorded and queued for manual processing."
                            },
                        }
                    )
                    continue

            if str(item.get("provider") or "").strip().lower() == "portal02" and not (
                is_mtn_normal or is_mtn_express
            ):
                jlog(
                    "portal02" + "_blocked",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": (svc_type if svc_type else "unknown"),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_portal_blocked",
                        "api_response": {"note": "Portal provider disabled; queued for manual processing."},
                    }
                )
                continue

            resolved_network = _resolve_dataconnect_network(svc_doc, item, admin_id=store_admin_id)

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
            type_allows_api = svc_type_flag in ("ON", "API")
            api_allowed = type_allows_api
            if svc_type_flag == "OFF":
                api_allowed = False

            chosen_mtn_normal_provider = None
            chosen_mtn_express_provider = None
            use_portal02 = False
            allowed_mtn_providers = {"portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"}
            if is_mtn_normal:
                chosen_mtn_normal_provider = (svc_provider or "").strip().lower()
                if chosen_mtn_normal_provider not in allowed_mtn_providers:
                    chosen_mtn_normal_provider = ""
                if not chosen_mtn_normal_provider:
                    use_portal02_flag = bool(svc_doc.get("mtn_normal_use_portal02")) if svc_doc else False
                    chosen_mtn_normal_provider = "portal02" if use_portal02_flag else "dataconnect"
                if api_allowed and chosen_mtn_normal_provider == "portal02":
                    use_portal02 = True

            if is_mtn_express:
                chosen_mtn_express_provider = (svc_provider or "").strip().lower()
                if chosen_mtn_express_provider not in allowed_mtn_providers:
                    chosen_mtn_express_provider = ""
                if not chosen_mtn_express_provider:
                    use_portal02_flag = bool(svc_doc.get("mtn_express_use_portal02")) if svc_doc else False
                    chosen_mtn_express_provider = "portal02" if use_portal02_flag else "dataconnect"
                if api_allowed and chosen_mtn_express_provider == "portal02":
                    use_portal02 = True

            use_codecraft = bool(
                api_allowed
                and (
                    (is_mtn_normal and chosen_mtn_normal_provider == "codecraft")
                    or (is_mtn_express and chosen_mtn_express_provider == "codecraft")
                    or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "codecraft")
                )
                and not use_portal02
            )
            codecraft_network = _resolve_codecraft_network_name(svc_doc, item, admin_id=store_admin_id) if use_codecraft else None
            use_bundleportal = bool(
                api_allowed
                and (
                    (is_mtn_normal and chosen_mtn_normal_provider == "bundleportal")
                    or (is_mtn_express and chosen_mtn_express_provider == "bundleportal")
                    or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "bundleportal")
                )
                and not use_portal02
                and not use_codecraft
            )
            bundleportal_network = _resolve_bundleportal_network_name(svc_doc, item, admin_id=store_admin_id) if use_bundleportal else None
            use_skplug = bool(
                api_allowed
                and (
                    (is_mtn_normal and chosen_mtn_normal_provider == "skplug")
                    or (is_mtn_express and chosen_mtn_express_provider == "skplug")
                    or ((not is_mtn_normal and not is_mtn_express) and svc_provider == "skplug")
                )
                and not use_portal02
                and not use_codecraft
                and not use_bundleportal
            )
            skplug_network = _resolve_skplug_network_name(svc_doc, item, admin_id=store_admin_id) if use_skplug else None

            use_dataconnect_express = (
                resolved_network == "mtn"
                and is_mtn_express
                and chosen_mtn_express_provider == "dataconnect"
                and api_allowed
            )
            use_dataconnect_mtn_normal = (
                is_mtn_normal and chosen_mtn_normal_provider == "dataconnect" and api_allowed
            )
            use_dataconnect = (use_dataconnect_express or use_dataconnect_mtn_normal) and not use_codecraft and not use_bundleportal and not use_skplug
            use_datakazina = bool(
                api_allowed
                and (
                    (is_mtn_normal and chosen_mtn_normal_provider == "datakazina")
                    or (is_mtn_express and chosen_mtn_express_provider == "datakazina")
                    or (resolved_network == "mtn" and svc_provider == "datakazina")
                )
                and not use_skplug
                and not use_bundleportal
            )

            jlog(
                "checkout_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                svc_name=svc_name,
                resolved_network=resolved_network,
                svc_type_flag=svc_type_flag,
                is_mtn_express=is_mtn_express,
                is_mtn_normal=is_mtn_normal,
                mtn_normal_provider=chosen_mtn_normal_provider,
                mtn_express_provider=chosen_mtn_express_provider,
                api_allowed=api_allowed,
                use_portal02=use_portal02,
                use_dataconnect=use_dataconnect,
                use_datakazina=use_datakazina,
                svc_provider=svc_provider,
                use_codecraft=use_codecraft,
                codecraft_network=codecraft_network,
                use_bundleportal=use_bundleportal,
                bundleportal_network=bundleportal_network,
                use_skplug=use_skplug,
                skplug_network=skplug_network,
            )

            if not use_dataconnect and not use_datakazina and not use_codecraft and not use_bundleportal and not use_skplug and not use_portal02:
                total_processing_amount += amt_total

                if not api_allowed:
                    note = (
                        "API calls disabled for this service (type OFF); queued for manual processing."
                    )
                    api_status = "not_applicable_type_off"
                else:
                    note = (
                        "Not API eligible for this provider; queued for manual processing."
                    )
                    api_status = "not_applicable_network"

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": api_status,
                        "api_response": {
                            "note": note,
                            "resolved_network": resolved_network,
                            "serviceName": svc_name,
                            "service_type_flag": svc_type_flag,
                        },
                    }
                )
                continue

            if use_portal02:
                package_size_gb = _resolve_package_size_gb(value_obj, item)

                if not phone or package_size_gb is None:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "package_size_gb": package_size_gb,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    **store_profit_field,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "portal02",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "portal02",
                    "portal02_network_slug": "mtn",
                    "package_size_gb": package_size_gb,
                    "portal02_offer_slug": PORTAL02_OFFER_SLUG_MTN_NORMAL,
                    "service_id": svc_doc["_id"],
                    "raw_item": item,
                }

                api_jobs.append(job_payload)
                continue

            if use_skplug:
                provider_gig = _resolve_package_size_gb(value_obj, item)

                if not phone or not provider_gig or not skplug_network:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "SKPlug API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "provider_network": skplug_network,
                                    "provider_gig": provider_gig,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    **store_profit_field,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "ported_confirmed": bool(ported_confirmed),
                    "detected_prefix": detected_prefix or "",
                    "expected_prefixes": expected_prefixes or [],
                    "network_group": network_group or "",
                    "provider": "skplug",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_network": skplug_network,
                    "provider_gig": provider_gig,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                if item.get("ported_expected_network") not in (None, ""):
                    line_record["ported_expected_network"] = str(item.get("ported_expected_network"))
                if item.get("ported_detected_network") not in (None, ""):
                    line_record["ported_detected_network"] = str(item.get("ported_detected_network"))
                if item.get("ported_prefix") not in (None, ""):
                    line_record["ported_prefix"] = str(item.get("ported_prefix"))

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "provider_gig": provider_gig,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                }

                api_jobs.append(job_payload)
                continue

            if use_bundleportal:
                api_requested_total += amt_total
                provider_gig = _resolve_package_size_gb(value_obj, item)
                normalized_phone = _normalize_bundleportal_phone(phone)
                if not re.fullmatch(r"0\d{9}", normalized_phone) or not provider_gig or not bundleportal_network:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({
                        "phone": phone, "base_amount": base_amount, "amount": amt_total,
                        "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                        **ported_fields, "value": item.get("value"), "value_obj": value_obj,
                        "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                        "provider": "bundleportal", "provider_network": bundleportal_network,
                        "provider_gig": provider_gig, "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key, "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {"note": "BundlePortal fields missing; queued for manual processing"},
                    })
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
                has_processing = True
                total_processing_amount += amt_total
                results.append({
                    "phone": normalized_phone, "base_amount": base_amount, "amount": amt_total,
                    "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                    **ported_fields, "value": item.get("value"), "value_obj": value_obj,
                    "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                    "provider": "bundleportal", "provider_reference": None,
                    "provider_order_id": None, "provider_request_order_id": external_ref,
                    "provider_network": bundleportal_network, "provider_gig": provider_gig,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key, "line_status": "pending", "api_status": "queued",
                    "api_response": {"note": "Queued for BundlePortal API call"},
                })
                api_jobs.append({
                    "provider_request_order_id": external_ref, "phone": normalized_phone,
                    "provider": "bundleportal", "provider_network": bundleportal_network,
                    "provider_gig": provider_gig,
                    "service_id": svc_doc["_id"] if svc_doc else None, "line_index": idx,
                })
                continue

            if use_codecraft:
                volume_mb = None
                if isinstance(value_obj, dict):
                    vol_raw = value_obj.get("volume")
                    if vol_raw not in (None, "", []):
                        try:
                            volume_mb = int(float(vol_raw))
                        except Exception:
                            volume_mb = None
                if volume_mb is None:
                    gb_fallback = _resolve_package_size_gb(value_obj, item)
                    if gb_fallback is not None:
                        volume_mb = int(gb_fallback * 1000)

                provider_gig = None
                if volume_mb is not None:
                    try:
                        provider_gig = max(1, int(float(volume_mb) / 1000))
                    except Exception:
                        provider_gig = None

                if not phone or not provider_gig or not codecraft_network:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "provider_network": codecraft_network,
                                    "provider_gig": provider_gig,
                                },
                            },
                        }
                    )
                    continue

                if codecraft_regular_map is None or codecraft_bigtime_map is None:
                    codecraft_regular_map, codecraft_bigtime_map = _codecraft_get_packages_cached()

                key = (codecraft_network, provider_gig)
                provider_mode = None
                provider_amount = None
                if codecraft_regular_map and key in codecraft_regular_map:
                    provider_mode = "regular"
                    provider_amount = codecraft_regular_map.get(key)
                elif codecraft_bigtime_map and key in codecraft_bigtime_map:
                    provider_mode = "regular"
                    provider_amount = codecraft_bigtime_map.get(key)

                if not provider_mode:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_package_not_found",
                            "api_response": {
                                "note": "Package not found in CodeCraft; queued for processing",
                                "provider_network": codecraft_network,
                                "provider_gig": provider_gig,
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    **store_profit_field,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "ported_confirmed": bool(ported_confirmed),
                    "detected_prefix": detected_prefix or "",
                    "expected_prefixes": expected_prefixes or [],
                    "network_group": network_group or "",
                    "provider": "codecraft",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_mode": provider_mode,
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_package_amount": provider_amount,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                if item.get("ported_expected_network") not in (None, ""):
                    line_record["ported_expected_network"] = str(item.get("ported_expected_network"))
                if item.get("ported_detected_network") not in (None, ""):
                    line_record["ported_detected_network"] = str(item.get("ported_detected_network"))
                if item.get("ported_prefix") not in (None, ""):
                    line_record["ported_prefix"] = str(item.get("ported_prefix"))

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "codecraft",
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_mode": provider_mode,
                    "provider_amount": provider_amount,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                }

                api_jobs.append(job_payload)
                continue

            if use_datakazina:
                shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item, svc_doc)

                jlog(
                    "datakazina_routing_selected",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    shared_bundle=shared_bundle,
                )

                if not phone or shared_bundle is None:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "shared_bundle": shared_bundle,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    **store_profit_field,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "ported_confirmed": bool(ported_confirmed),
                    "detected_prefix": detected_prefix or "",
                    "expected_prefixes": expected_prefixes or [],
                    "network_group": network_group or "",
                    "provider": "datakazina",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                    "shared_bundle": shared_bundle,
                }

                if item.get("ported_expected_network") not in (None, ""):
                    line_record["ported_expected_network"] = str(item.get("ported_expected_network"))
                if item.get("ported_detected_network") not in (None, ""):
                    line_record["ported_detected_network"] = str(item.get("ported_detected_network"))
                if item.get("ported_prefix") not in (None, ""):
                    line_record["ported_prefix"] = str(item.get("ported_prefix"))

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "shared_bundle": shared_bundle,
                    "incoming_api_ref": external_ref,
                    "network_id": 3,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                }

                api_jobs.append(job_payload)
                continue

            if not use_dataconnect:
                continue

            package_size_gb = _resolve_package_size_gb(value_obj, item)

            shared_bundle = None
            if isinstance(value_obj, dict):
                sb = value_obj.get("volume") or value_obj.get("shared_bundle") or value_obj.get("mb")
                if sb not in (None, "", []):
                    try:
                        shared_bundle = int(float(sb))
                    except Exception:
                        shared_bundle = None
            if shared_bundle is None and package_size_gb is not None:
                shared_bundle = int(package_size_gb * 1000)

            if not phone or package_size_gb is None:
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {
                            "note": "API fields missing; queued for processing",
                            "got": {
                                "phone": bool(phone),
                                "resolved_network": resolved_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                    }
                )
                continue

            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

            provider_name = "dataconnect"

            total_processing_amount += amt_total

            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                **ported_fields,
                **store_profit_field,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "ported_confirmed": bool(ported_confirmed),
                "detected_prefix": detected_prefix or "",
                "expected_prefixes": expected_prefixes or [],
                "network_group": network_group or "",
                "provider": provider_name,
                "provider_reference": None,
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                "line_status": "processing",
                "api_status": "queued",
                "api_response": {"note": "Queued for background API call"},
            }

            if provider_name == "dataconnect":
                line_record["shared_bundle"] = shared_bundle
            if item.get("ported_expected_network") not in (None, ""):
                line_record["ported_expected_network"] = str(item.get("ported_expected_network"))
            if item.get("ported_detected_network") not in (None, ""):
                line_record["ported_detected_network"] = str(item.get("ported_detected_network"))
            if item.get("ported_prefix") not in (None, ""):
                line_record["ported_prefix"] = str(item.get("ported_prefix"))

            results.append(line_record)

            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": provider_name,
                "service_id": svc_doc["_id"] if svc_doc else None,
            }

            if provider_name == "dataconnect":
                job_payload["network_id"] = network_id
                job_payload["shared_bundle"] = shared_bundle

            api_jobs.append(job_payload)

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )

        results, profit_split_totals = _finalize_store_profit_lines(results, store_doc)
        profit_amount_total = profit_split_totals["profit_amount_total"]
        admin_wallet_debit_total = round(
            sum(_money(it.get("admin_base_amount")) for it in results if _money(it.get("amount")) > 0),
            2,
        )
        agent_wallet_debit_total = 0.0
        store_profit_total = 0.0
        if paystack_verified:
            store_profit_total = sum(_money(it.get("store_profit_amount")) for it in results)

        order_admin_id = store_doc.get("admin_id") or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id"))
        wallet_debit_status = "completed"
        debit_ok, debit_message, debit_rows = debit_wallets_for_order(
            balances_col=balances_col,
            balance_logs_col=balance_logs_col,
            transactions_col=transactions_col,
            debits=[
                {"user_id": order_admin_id, "amount": admin_wallet_debit_total, "label": "admin_base_debit"},
            ],
            order_id=order_id,
            admin_id=order_admin_id,
            source="store_checkout",
            note="Store order wallet debit",
            meta={
                "store_slug": slug,
                "paystack_reference": ps_ref,
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "agent_wallet_debit_total": agent_wallet_debit_total,
                "store_profit_total": round(store_profit_total, 2),
                "customer_charge_total": round(total_processing_amount, 2),
                "allow_negative_wallet": True,
                "complaint_override": bool(admin_override_complaint),
            },
            allow_negative=True,
        )
        if not debit_ok:
            message = debit_message if debit_message == WALLET_OVERDRAFT_LIMIT_MESSAGE else f"Order debit failed: {debit_message}"
            return jsonify({"success": False, "message": message}), 400
        try:
            evaluate_admin_wallet_low_balance(order_admin_id, send_alert=True, run_auto_credit=True)
        except Exception:
            pass

        order_doc = {
            "user_id": (ObjectId(session["user_id"]) if session.get("user_id") else store_doc.get("owner_id")),
            "admin_id": order_admin_id,
            "store_slug": slug,
            "store_owner_id": store_doc.get("owner_id"),
            "order_id": order_id,
            "items": results,
            "total_amount": round(total_requested, 2),
            "charged_amount": round(total_processing_amount, 2),
            "admin_wallet_debit_total": admin_wallet_debit_total,
            "agent_wallet_debit_total": agent_wallet_debit_total,
            "wallet_debit_status": wallet_debit_status,
            "wallet_debits": debit_rows,
            "profit_amount_total": round(profit_amount_total, 2),
            "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
            "admin_profit_total": profit_split_totals["admin_profit_total"],
            "store_profit_total": round(store_profit_total, 2),
            "status": "processing",
            "paid_from": paid_from,
            "payment_provider": "moolre" if method == "moolre" else ("paystack" if paystack_verified else paid_from),
            "payment_reference": ps_ref,
            "payment_gateway": "Moolre" if method == "moolre" else ("Paystack" if paystack_verified else str(paid_from or "Store").title()),
            "payment_status": "success" if paystack_verified else "",
            "payment_verified_at": datetime.utcnow() if paystack_verified else None,
            "payment_raw": (payment_info.get("raw") or {}) if method == "moolre" else {},
            "paystack_reference": ps_ref,
            "moolre_reference": payment_info.get("moolre_reference") if method == "moolre" else "",
            "paystack_charged_amount": round(paid_ghs, 2),
            "paystack_fee_amount": round(paystack_fee_ghs, 2),
            "payer_email": payer_email,
            "payer_email_source": payer_email_source,
            "payer_phone": payer_phone,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "debug": {
                "store_checkout": True,
                "events": debug_events[-10:],
                "paystack_paid_ghs": paid_ghs,
                "paystack_expected_ghs": expected_pay_ghs,
                "paystack_fee_ghs": paystack_fee_ghs,
                "gateway_fee_overage_ghs": 0.0 if paystack_verified else fee_delta_ghs,
                "skipped_count": skipped_count,
            },
        }
        if admin_override_complaint:
            order_doc["complaint_id"] = str(admin_override_complaint.get("_id"))
            order_doc["complaint_source"] = "admin_payment_confirmed"

        order_docs, order_ids = _split_order_documents(order_doc, results, order_id)
        for line_doc in order_docs:
            line_totals = {
                "profit_amount_total": line_doc.get("profit_amount_total", 0),
                "main_admin_profit_total": line_doc.get("main_admin_profit_total", 0),
                "admin_profit_total": line_doc.get("admin_profit_total", 0),
                "store_profit_total": line_doc.get("store_profit_total", 0),
            }
            _log_store_profit_summary("store_checkout_profit_summary", line_doc, line_totals)
            if _checkout_helpers.get("order_fn"):
                try:
                    _checkout_helpers["order_fn"](orders_col, line_doc)
                    continue
                except Exception:
                    pass
            orders_col.insert_one(line_doc)
        _clear_dashboard_cache_safely()

        for line_doc in order_docs:
            try:
                send_mtn_mashup_order_sms(line_doc)
            except Exception as exc:
                try:
                    jlog("store_mtn_mashup_sms_error", order_id=line_doc["order_id"], error=str(exc))
                except Exception:
                    pass

        if admin_override_complaint:
            try:
                complaints_col.update_one(
                    {"_id": admin_override_complaint["_id"]},
                    {
                        "$set": {
                            "store_order_id": order_ids[0],
                            "store_order_ids": order_ids,
                            "store_batch_id": order_id,
                            "store_order_processed": True,
                            "store_order_processed_at": datetime.utcnow(),
                            "store_order_processed_by": {
                                "user_id": session.get("user_id"),
                                "username": session.get("username") or session.get("email") or "admin",
                            },
                            "status": "resolved",
                            "resolved_at": datetime.utcnow(),
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
            except Exception:
                pass

        try:
            log_activity(
                "store_order_placed",
                actor_id=order_doc.get("user_id"),
                actor_role="customer" if order_doc.get("user_id") else "guest",
                admin_id=order_doc.get("admin_id"),
                target_type="order",
                target_id=order_doc.get("order_id") or order_doc.get("_id"),
                message="Store order placed",
                meta={
                    "total_amount": order_doc.get("total_amount"),
                    "paid_from": order_doc.get("paid_from"),
                    "store_slug": order_doc.get("store_slug"),
                    "source": "store",
                },
            )
        except Exception:
            pass

        try:
            providers_used = sorted({it.get("provider") for it in results if it.get("provider")})
            provider_request_ids = [
                it.get("provider_request_order_id")
                for it in results
                if it.get("provider_request_order_id")
            ]
            existing_store_txn = transactions_col.find_one(
                {
                    "reference": order_id,
                    "status": "success",
                    "type": "purchase",
                    "source": "store_order",
                },
                {"_id": 1},
            )
            if not existing_store_txn:
                transactions_col.insert_one(
                    {
                        "user_id": order_doc.get("user_id"),
                        "admin_id": order_doc.get("admin_id"),
                        "amount": round(total_processing_amount, 2),
                        "reference": order_id,
                        "status": "success",
                        "type": "purchase",
                        "source": "store_order",
                        "gateway": "Admin Complaint" if method == "admin_override" else ("Moolre" if method == "moolre" else ("Paystack" if paystack_verified else str(paid_from or "Store").title())),
                        "currency": "GHS",
                        "created_at": order_doc.get("created_at") or datetime.utcnow(),
                        "verified_at": datetime.utcnow(),
                        "meta": {
                            "store_checkout": True,
                            "store_slug": slug,
                            "store_owner_id": store_doc.get("owner_id"),
                            "payer_phone": payer_phone,
                            "payer_email": payer_email,
                            "paid_from": paid_from,
                            "payment_provider": "moolre" if method == "moolre" else ("paystack" if paystack_verified else paid_from),
                            "payment_reference": ps_ref,
                            "moolre": (payment_info.get("raw") or {}) if method == "moolre" else {},
                            "paystack_reference": ps_ref,
                            "charged_amount": round(total_processing_amount, 2),
                            "requested_amount": round(total_requested, 2),
                            "admin_wallet_debit_total": admin_wallet_debit_total,
                            "agent_wallet_debit_total": agent_wallet_debit_total,
                            "profit_amount_total": round(profit_amount_total, 2),
                            "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
                            "admin_profit_total": profit_split_totals["admin_profit_total"],
                            "store_profit_total": round(store_profit_total, 2),
                            "providers_used": providers_used,
                            "provider_request_ids": provider_request_ids,
                        },
                    }
                )
        except Exception:
            pass

        if paystack_verified and store_profit_total > 0:
            try:
                store_accounts_col.update_one(
                    {"store_slug": slug},
                    {
                        "$inc": {"total_profit_balance": round(store_profit_total, 2)},
                        "$set": {
                            "last_updated_profit": round(store_profit_total, 2),
                            "updated_at": datetime.utcnow(),
                        },
                        "$setOnInsert": {
                            "store_slug": slug,
                            "admin_id": order_admin_id,
                            "created_at": datetime.utcnow(),
                        },
                    },
                    upsert=True,
                )
            except Exception:
                jlog("store_account_update_error", store_slug=slug)

        order_id_by_provider_ref = {
            doc["items"][0].get("provider_request_order_id"): doc["order_id"]
            for doc in order_docs if doc["items"][0].get("provider_request_order_id")
        }
        order_id_by_position = {doc.get("batch_position"): doc["order_id"] for doc in order_docs}
        for job in api_jobs:
            job["order_id"] = order_id_by_provider_ref.get(
                job.get("provider_request_order_id"),
                order_id_by_position.get(job.get("line_index"), order_ids[0]),
            )

        if api_jobs:
            try:
                _background_process_providers(order_id, api_jobs)
            except Exception as e:
                jlog("store_checkout_sync_dispatch_error", order_id=order_id, error=str(e))

        latest_orders = list(orders_col.find({"batch_id": order_id}, {"items": 1, "status": 1}).sort("batch_position", 1))
        response_items = [item for doc in latest_orders for item in (doc.get("items") or [])] or results
        response_status = "processing"

        return jsonify(
            {
                "success": True,
                "message": f"Orders received and sent to the provider API. Order IDs: {', '.join(order_ids)}",
                "order_id": order_ids[0],
                "order_ids": order_ids,
                "batch_id": order_id,
                "status": response_status,
                "charged_amount": round(total_processing_amount, 2),
                "admin_wallet_debit_total": admin_wallet_debit_total,
                "agent_wallet_debit_total": agent_wallet_debit_total,
                "profit_amount_total": round(profit_amount_total, 2),
                "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
                "admin_profit_total": profit_split_totals["admin_profit_total"],
                "store_profit_total": round(store_profit_total, 2),
                "skipped_count": skipped_count,
                "items": response_items,
                "paid_ghs": paid_ghs,
                "expected_ghs": expected_pay_ghs,
                "redirect_url": url_for("admin_complaints.admin_view_complaints", status="resolved") if admin_override_complaint else url_for("checkout.invoice_batch_view", batch_id=order_id),
            }
        ), 200

    except Exception:
        try:
            jlog("store_public_checkout_uncaught", slug=slug, error=traceback.format_exc())
        except Exception:
            pass
        return jsonify({"success": False, "message": "Server error"}), 500


@stores_bp.route("/api/store-complaints/<slug>", methods=["POST"])
def api_store_complaint(slug: str):
    try:
        payload = request.get_json(silent=True) or {}

        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        name = (payload.get("name") or "").strip()
        phone_raw = (payload.get("phone") or "").strip()
        phone_digits = re.sub(r"\D+", "", phone_raw)
        if phone_digits.startswith("233") and len(phone_digits) == 12:
            phone_digits = "0" + phone_digits[3:]
        phone = phone_digits
        paystack_ref = (payload.get("paystack_reference") or "").strip()
        payment_date = (payload.get("payment_date") or "").strip()
        payment_time = (payload.get("payment_time") or "").strip()
        order_id = (payload.get("order_id") or "").strip()
        message = (payload.get("message") or "").strip()
        cart = payload.get("cart") or []

        if not phone:
            return jsonify({"success": False, "message": "Phone number is required"}), 400
        if not re.fullmatch(r"0\d{9}", phone):
            return jsonify({"success": False, "message": "Phone number must be exactly 10 digits, e.g. 0551234567"}), 400
        if not paystack_ref:
            return jsonify({"success": False, "message": "Paystack reference is required"}), 400
        active_complaint = complaints_col.find_one(
            _active_store_complaint_query(slug, phone, paystack_ref),
            {"_id": 1, "status": 1, "submitted_at": 1},
        )
        if active_complaint:
            submitted_at = active_complaint.get("submitted_at")
            return jsonify({
                "success": False,
                "message": "You already have an active complaint. Please wait until it is resolved before submitting another.",
                "complaint_id": str(active_complaint.get("_id")),
                "complaint_status": active_complaint.get("status") or "pending",
                "submitted_at": submitted_at.isoformat() if isinstance(submitted_at, datetime) else "",
            }), 409
        if not payment_date:
            return jsonify({"success": False, "message": "Payment date is required"}), 400
        if not payment_time:
            return jsonify({"success": False, "message": "Payment time is required"}), 400
        try:
            payment_dt = datetime.strptime(f"{payment_date} {payment_time}", "%Y-%m-%d %H:%M")
        except Exception:
            return jsonify({"success": False, "message": "Invalid payment date/time format (YYYY-MM-DD HH:MM)"}), 400
        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart snapshot is required"}), 400

        first = cart[0] if cart else {}
        service_name = first.get("serviceName") or first.get("service_name")
        offer = first.get("value") or first.get("offer")
        total_amount = 0.0
        for it in cart:
            try:
                total_amount += float(it.get("amount") or 0)
            except Exception:
                continue

        existing_order = orders_col.find_one(
            {"store_slug": slug, "paystack_reference": paystack_ref},
            {"order_id": 1},
        )
        complaint_doc = {
            "admin_id": store_doc.get("admin_id") or resolve_admin_id_for_user_id(users_col, store_doc.get("owner_id")),
            "store_slug": slug,
            "store_name": store_doc.get("name") or "",
            "customer_name": name,
            "customer_phone": phone,
            "customer_phone_norm": _normalize_complaint_phone(phone),
            "sent_to_main_admin": False,
            "paystack_reference": paystack_ref,
            "paystack_reference_norm": paystack_ref.lower(),
            "payment_date": f"{payment_date} {payment_time}",
            "payment_date_dt": payment_dt,
            "payment_date_str": f"{payment_date} {payment_time}",
            "order_number_provided": order_id or paystack_ref,
            "order_ref": {"order_id": order_id} if order_id else {},
            "service_name": service_name,
            "offer": offer,
            "cart_snapshot": cart,
            "cart_total": round(total_amount, 2),
            "description": message,
            "flagged_ref_exists": bool(existing_order),
            "flagged_ref_order_id": (existing_order or {}).get("order_id"),
            "submitted_at": datetime.utcnow(),
            "status": "pending",
            "source": "store_page",
        }

        complaints_col.insert_one(complaint_doc)
        return jsonify({"success": True, "message": "Complaint submitted"}), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


@stores_bp.route("/api/store-order/<order_id>", methods=["GET"])
def api_store_order(order_id: str):
    try:
        order_id = (order_id or "").strip()
        if not order_id:
            return jsonify({"success": False, "message": "order_id required"}), 400

        doc = orders_col.find_one(
            {"order_id": order_id},
            {
                "_id": 0,
                "order_id": 1,
                "store_slug": 1,
                "status": 1,
                "total_amount": 1,
                "charged_amount": 1,
                "admin_wallet_debit_total": 1,
                "agent_wallet_debit_total": 1,
                "wallet_debit_status": 1,
                "profit_amount_total": 1,
                "main_admin_profit_total": 1,
                "admin_profit_total": 1,
                "store_profit_total": 1,
                "items": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        if not doc:
            return jsonify({"success": False, "message": "Order not found"}), 404

        # datetime safe
        for k in ("created_at", "updated_at"):
            v = doc.get(k)
            if isinstance(v, datetime):
                doc[k] = v.isoformat()

        return jsonify({"success": True, "order": doc}), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


@stores_bp.route("/api/store-order-by-ref/<slug>", methods=["GET"])
def api_store_order_by_ref(slug: str):
    try:
        ref = (request.args.get("ref") or "").strip()
        if not ref:
            return jsonify({"success": False, "message": "ref required"}), 400

        doc = orders_col.find_one(
            {"store_slug": slug, "paystack_reference": ref},
            {"order_id": 1, "store_slug": 1},
        )
        if doc:
            return jsonify({"success": True, "exists": True, "order_id": doc.get("order_id")}), 200
        return jsonify({"success": True, "exists": False}), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


@stores_bp.route("/store-invoice/<order_id>", methods=["GET"])
def store_invoice_view(order_id: str):
    order = orders_col.find_one({"order_id": order_id})
    if not order:
        abort(404)

    order["display_items"] = build_order_display_items(order.get("items") or [])

    store_doc = stores_col.find_one(
        {"slug": order.get("store_slug"), "status": {"$ne": "deleted"}},
        {"name": 1, "slug": 1, "logo_url": 1, "owner_id": 1, "whatsapp_number": 1},
    ) if order.get("store_slug") else {}

    buyer_label = "Store Customer"
    if order.get("payer_email"):
        buyer_label = str(order.get("payer_email")).strip()
    elif order.get("payer_phone"):
        buyer_label = str(order.get("payer_phone")).strip()
    else:
        try:
            uid = order.get("user_id")
            if uid:
                user_doc = users_col.find_one({"_id": uid}, {"name": 1, "full_name": 1, "username": 1, "email": 1, "phone": 1}) or {}
                buyer_label = (
                    user_doc.get("name")
                    or user_doc.get("full_name")
                    or user_doc.get("username")
                    or user_doc.get("email")
                    or user_doc.get("phone")
                    or buyer_label
                )
        except Exception:
            pass

    return render_template(
        "store_invoice.html",
        order=order,
        store=store_doc or {},
        buyer_label=buyer_label,
    )
