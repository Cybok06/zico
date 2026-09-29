from __future__ import annotations

from typing import Any, Optional

from bson import ObjectId

from db import db
from social_boosting_pricing import money, normalize_admin_level, normalize_customer_stage


checker_pricing_col = db["checker_pricing"]

VALID_CHECKER_TYPES = {"wassce", "bece"}
ADMIN_LEVEL_KEYS = ("admin", "super_admin", "super_professional")
CUSTOMER_STAGE_KEYS = ("normal_agent", "elite_agent", "premium")


def normalize_checker_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in VALID_CHECKER_TYPES else "wassce"


def _oid_text(value: Any) -> str:
    if isinstance(value, ObjectId):
        return str(value)
    return str(value or "").strip()


def get_checker_pricing_doc(checker_type: Any) -> dict:
    ctype = normalize_checker_type(checker_type)
    doc = checker_pricing_col.find_one({"checker_type": ctype}) or {}
    return doc if isinstance(doc, dict) else {}


def checker_base_cost(pricing_doc: dict | None, legacy_amount: Any = None) -> float:
    raw = (pricing_doc or {}).get("base_cost")
    if raw in (None, ""):
        raw = legacy_amount
    try:
        return money(raw)
    except Exception:
        return 0.0


def upsert_checker_base_cost(checker_type: Any, base_cost: Any) -> None:
    ctype = normalize_checker_type(checker_type)
    checker_pricing_col.update_one(
        {"checker_type": ctype},
        {
            "$set": {
                "checker_type": ctype,
                "base_cost": money(base_cost),
            },
            "$currentDate": {"updated_at": True},
        },
        upsert=True,
    )


def upsert_admin_stage_prices(checker_type: Any, prices: dict[str, Any]) -> None:
    ctype = normalize_checker_type(checker_type)
    stage_prices = {}
    for key in ADMIN_LEVEL_KEYS:
        raw = prices.get(key)
        if raw in (None, ""):
            continue
        stage_prices[key] = money(raw)
    checker_pricing_col.update_one(
        {"checker_type": ctype},
        {"$set": {"checker_type": ctype, "admin_stage_prices": stage_prices}, "$currentDate": {"updated_at": True}},
        upsert=True,
    )


def upsert_customer_stage_prices(checker_type: Any, admin_id: Any, prices: dict[str, Any]) -> None:
    ctype = normalize_checker_type(checker_type)
    admin_key = _oid_text(admin_id)
    if not admin_key:
        return
    stage_prices = {}
    for key in CUSTOMER_STAGE_KEYS:
        raw = prices.get(key)
        if raw in (None, ""):
            continue
        stage_prices[key] = money(raw)
    checker_pricing_col.update_one(
        {"checker_type": ctype},
        {
            "$set": {
                "checker_type": ctype,
                f"customer_stage_prices_by_admin.{admin_key}": stage_prices,
            },
            "$currentDate": {"updated_at": True},
        },
        upsert=True,
    )


def admin_stage_price(pricing_doc: dict | None, admin_level: Any, legacy_amount: Any = None) -> Optional[float]:
    stage_prices = (pricing_doc or {}).get("admin_stage_prices") or {}
    level_key = normalize_admin_level(admin_level)
    price = stage_prices.get(level_key)
    if price in (None, ""):
        price = stage_prices.get("admin")
    if price in (None, "") and legacy_amount not in (None, ""):
        return money(legacy_amount)
    return money(price) if price not in (None, "") else None


def customer_stage_price(
    pricing_doc: dict | None,
    admin_id: Any,
    admin_level: Any,
    stage_label: Any,
    legacy_amount: Any = None,
) -> Optional[float]:
    base_price = admin_stage_price(pricing_doc, admin_level, legacy_amount=legacy_amount)
    by_admin = (pricing_doc or {}).get("customer_stage_prices_by_admin") or {}
    stage_prices = by_admin.get(_oid_text(admin_id)) or {}
    stage_key = normalize_customer_stage(stage_label)
    price = stage_prices.get(stage_key)
    if price in (None, ""):
        aliases = {
            "normal_agent": ("normal", "normal_agent", "normal agent"),
            "elite_agent": ("elite", "elite_agent", "elite agent"),
            "premium": ("premium", "premium_agent"),
        }.get(stage_key, ())
        lowered = {str(k).strip().lower(): v for k, v in stage_prices.items()}
        for alias in aliases:
            if alias in lowered:
                price = lowered.get(alias)
                break
    if price in (None, ""):
        return base_price
    return money(price)
