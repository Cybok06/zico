from __future__ import annotations

import os
from typing import Dict, Any, Tuple

from db import db

settings_col = db["settings"]
users_col = db["users"]
PAYSTACK_FIELDS = (
    "store_public_key",
    "store_secret_key",
    "deposit_public_key",
    "deposit_secret_key",
)


def _clean(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _has_paystack_value(doc: Dict[str, Any] | None) -> bool:
    if not doc:
        return False
    return any(_clean(doc.get(field)) for field in PAYSTACK_FIELDS)


def _candidate_paystack_docs() -> list[Dict[str, Any]]:
    docs: list[Dict[str, Any]] = []
    seen_ids = set()

    def append_doc(doc: Dict[str, Any] | None) -> None:
        if not doc:
            return
        doc_id = doc.get("_id")
        if doc_id in seen_ids:
            return
        seen_ids.add(doc_id)
        docs.append(doc)

    append_doc(settings_col.find_one({"key": "paystack_keys", "admin_id": {"$exists": False}}))
    append_doc(settings_col.find_one({"key": "paystack_keys", "admin_id": None}))

    try:
        main_admin_ids = [u["_id"] for u in users_col.find({"role": "main_admin"}, {"_id": 1}) if u.get("_id")]
    except Exception:
        main_admin_ids = []

    if main_admin_ids:
        for doc in settings_col.find(
            {"key": "paystack_keys", "admin_id": {"$in": main_admin_ids}},
            sort=[("updated_at", -1), ("created_at", -1), ("_id", -1)],
        ):
            append_doc(doc)

    for doc in settings_col.find(
        {"key": "paystack_keys"},
        sort=[("updated_at", -1), ("created_at", -1), ("_id", -1)],
        limit=25,
    ):
        append_doc(doc)
    return docs


def _preferred_paystack_doc() -> Dict[str, Any]:
    for doc in _candidate_paystack_docs():
        if _has_paystack_value(doc):
            return doc
    latest_doc = (
        settings_col.find_one(
            {"key": "paystack_keys"},
            sort=[("updated_at", -1), ("created_at", -1), ("_id", -1)],
        )
        or {}
    )
    return latest_doc or {}


def _merged_paystack_doc() -> Dict[str, Any]:
    anchor = dict(_preferred_paystack_doc() or {})
    merged = dict(anchor)

    store_ts = None
    deposit_ts = None
    for field in PAYSTACK_FIELDS:
        merged.setdefault(field, "")

    for doc in _candidate_paystack_docs():
        if not _has_paystack_value(doc):
            continue
        for field in PAYSTACK_FIELDS:
            if merged.get(field):
                continue
            value = _clean(doc.get(field))
            if not value:
                continue
            merged[field] = value
            if field.startswith("store_") and not store_ts:
                store_ts = doc.get("store_updated_at") or doc.get("updated_at") or doc.get("created_at")
            if field.startswith("deposit_") and not deposit_ts:
                deposit_ts = doc.get("deposit_updated_at") or doc.get("updated_at") or doc.get("created_at")

    merged["store_updated_at"] = store_ts or anchor.get("store_updated_at") or anchor.get("updated_at")
    merged["deposit_updated_at"] = deposit_ts or anchor.get("deposit_updated_at") or anchor.get("updated_at")
    return merged


def get_paystack_keys_doc(admin_id: Any | None = None) -> Dict[str, Any]:
    return _merged_paystack_doc()


def get_paystack_keys(admin_id: Any | None = None) -> Dict[str, Any]:
    global_doc = _merged_paystack_doc()

    def pick_store(field: str, env_primary: str, env_fallback: str) -> str:
        global_val = _clean(global_doc.get(field)) if global_doc else ""
        if global_val:
            return global_val
        return _clean(os.getenv(env_primary) or os.getenv(env_fallback) or "")

    def pick_global(field: str, env_primary: str, env_fallback: str) -> str:
        global_val = _clean(global_doc.get(field)) if global_doc else ""
        if global_val:
            return global_val
        return _clean(os.getenv(env_primary) or os.getenv(env_fallback) or "")

    store_updated_at = (global_doc or {}).get("store_updated_at") or (global_doc or {}).get("updated_at")

    deposit_updated_at = (global_doc or {}).get("deposit_updated_at") or (global_doc or {}).get("updated_at")

    return {
        "store_public": pick_store("store_public_key", "PAYSTACK_STORE_PUBLIC_KEY", "PAYSTACK_PUBLIC_KEY"),
        "store_secret": pick_store("store_secret_key", "PAYSTACK_STORE_SECRET_KEY", "PAYSTACK_SECRET_KEY"),
        "deposit_public": pick_global("deposit_public_key", "PAYSTACK_DEPOSIT_PUBLIC_KEY", "PAYSTACK_PUBLIC_KEY"),
        "deposit_secret": pick_global("deposit_secret_key", "PAYSTACK_DEPOSIT_SECRET_KEY", "PAYSTACK_SECRET_KEY"),
        "store_updated_at": store_updated_at,
        "deposit_updated_at": deposit_updated_at,
    }


def get_paystack_key_pair(profile: str, admin_id: Any | None = None) -> Tuple[str, str]:
    keys = get_paystack_keys(admin_id=admin_id)
    if (profile or "").strip().lower() == "store":
        return keys.get("store_public", ""), keys.get("store_secret", "")
    return keys.get("deposit_public", ""), keys.get("deposit_secret", "")
