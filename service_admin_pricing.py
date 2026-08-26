from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import re
import json
from ast import literal_eval

from bson import ObjectId

from db import db

services_col = db["services"]
users_col = db["users"]

ADMIN_LEVELS = {"admin", "super_admin", "super_professional"}

_INT_RE = re.compile(r"^\s*[\d,]+\s*$")


def normalize_admin_level(raw: str | None) -> str:
    lvl = (raw or "").strip().lower()
    if lvl in {"super_admin", "superadmin"}:
        return "super_admin"
    if lvl in {"super_professional", "professional_admin", "professional"}:
        return "super_professional"
    return "admin"


def _to_int(s: Any) -> Optional[int]:
    try:
        if isinstance(s, str):
            s = s.replace(",", "").strip()
        return int(float(s))
    except Exception:
        return None


def extract_offer_id(value_raw: Any) -> Optional[int]:
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


def _offer_key(offer: Dict[str, Any], idx: int) -> str:
    raw = offer.get("offer_id") or extract_offer_id(offer.get("value"))
    return str(raw or idx)


def _to_float(s: Any) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def admin_stage_price_from_offer(offer: Dict[str, Any], level: str) -> Optional[float]:
    prices = offer.get("admin_stage_prices")
    if not isinstance(prices, dict):
        return None

    lvl = normalize_admin_level(level)
    if lvl in prices:
        return _to_float(prices.get(lvl))

    lowered = {str(k).strip().lower(): v for k, v in prices.items()}
    aliases = {
        "admin": ("admin", "normal_admin"),
        "super_admin": ("super_admin", "superadmin", "super admin"),
        "super_professional": ("super_professional", "professional_admin", "professional", "pro_admin"),
    }.get(lvl, ())
    for a in aliases:
        if a in lowered:
            return _to_float(lowered.get(a))
    return None


def admin_offer_amount(base_offer: Dict[str, Any], admin_level: str) -> Optional[float]:
    return admin_stage_price_from_offer(base_offer, admin_level)


def apply_admin_pricing_to_offers(
    base_offers: List[Dict[str, Any]],
    admin_offers: List[Dict[str, Any]],
    admin_level: str,
) -> List[Dict[str, Any]]:
    if not base_offers or not admin_offers:
        return admin_offers

    base_map: Dict[str, Dict[str, Any]] = {}
    for idx, of in enumerate(base_offers, start=1):
        base_map[_offer_key(of, idx)] = of

    for idx, of in enumerate(admin_offers, start=1):
        key = _offer_key(of, idx)
        base_of = base_map.get(key) or (base_offers[idx - 1] if idx - 1 < len(base_offers) else None)
        if not base_of:
            continue
        amt = admin_offer_amount(base_of, admin_level)
        if amt is not None:
            of["amount"] = round(float(amt), 2)
        else:
            of["amount"] = None
    return admin_offers


def reprice_admin_services_for_base(base_service_doc: Dict[str, Any]) -> int:
    if not base_service_doc:
        return 0
    base_id = base_service_doc.get("_id")
    if not isinstance(base_id, ObjectId):
        return 0

    base_offers = base_service_doc.get("offers") or []
    cursor = services_col.find({"base_service_id": base_id}, {"_id": 1, "admin_id": 1, "offers": 1})
    updated = 0
    now = datetime.utcnow()

    for svc in cursor:
        admin_id = svc.get("admin_id")
        if not isinstance(admin_id, ObjectId):
            continue
        admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1})
        level = normalize_admin_level((admin_doc or {}).get("admin_level"))
        offers = svc.get("offers") or []
        offers = apply_admin_pricing_to_offers(base_offers, offers, level)
        res = services_col.update_one(
            {"_id": svc["_id"]},
            {"$set": {"offers": offers, "updated_at": now}},
        )
        if res.modified_count:
            updated += 1
    return updated


def reprice_admin_services_for_admin(admin_id: ObjectId) -> int:
    if not isinstance(admin_id, ObjectId):
        return 0
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1})
    level = normalize_admin_level((admin_doc or {}).get("admin_level"))
    cursor = services_col.find({"admin_id": admin_id, "base_service_id": {"$exists": True}}, {"_id": 1, "base_service_id": 1, "offers": 1})
    updated = 0
    now = datetime.utcnow()
    for svc in cursor:
        base_id = svc.get("base_service_id")
        if not isinstance(base_id, ObjectId):
            continue
        base_doc = services_col.find_one({"_id": base_id}, {"offers": 1})
        if not base_doc:
            continue
        offers = svc.get("offers") or []
        offers = apply_admin_pricing_to_offers(base_doc.get("offers") or [], offers, level)
        res = services_col.update_one(
            {"_id": svc["_id"]},
            {"$set": {"offers": offers, "updated_at": now}},
        )
        if res.modified_count:
            updated += 1
    return updated
