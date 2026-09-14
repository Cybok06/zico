from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from bson import ObjectId

from db import db
from social_boosting_pricing import (
    SOCIAL_BOOSTING_IMAGE_URL,
    apply_default_offer_fields,
)


TARGET_SERVICE_ID = "69dfade8c9890c62a77db55d"
FIELD_NAME = "services_offers"
SOCIAL_MEDIA_PLATFORMS = (
    ("TikTok", ("tiktok", "tik tok")),
    ("Instagram", ("instagram",)),
    ("Facebook", ("facebook",)),
    ("Telegram", ("telegram",)),
    ("YouTube", ("youtube", "you tube")),
    ("WhatsApp", ("whatsapp", "whats app")),
    ("Potato Chat", ("potato chat",)),
)


def _load_exosupplier_runtime():
    """Load ExoSupplierRuntime from try.py without importing a reserved module name."""
    try_path = Path(__file__).with_name("try.py")
    spec = importlib.util.spec_from_file_location("exo_try_runtime", try_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {try_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExoSupplierRuntime


def _detect_social_media(service: Dict[str, Any]) -> str:
    haystack = " ".join([
        str(service.get("category") or ""),
        str(service.get("name") or ""),
    ]).lower()

    for label, aliases in SOCIAL_MEDIA_PLATFORMS:
        if any(alias in haystack for alias in aliases):
            return label
    return "Other"


def _normalize_offer(service: Dict[str, Any]) -> Dict[str, Any]:
    return apply_default_offer_fields({
        "service": service.get("service"),
        "name": service.get("name") or "",
        "social_media": _detect_social_media(service),
        "type": service.get("type") or "",
        "rate": service.get("rate") or "",
        "min": service.get("min"),
        "max": service.get("max"),
        "dripfeed": bool(service.get("dripfeed")),
        "refill": bool(service.get("refill")),
        "cancel": bool(service.get("cancel")),
        "category": service.get("category") or "",
    })


def fetch_services_offers() -> List[Dict[str, Any]]:
    runtime_cls = _load_exosupplier_runtime()
    client = runtime_cls()
    services = client.get_services()
    return [_normalize_offer(service) for service in services]


def sync_services_offers(service_id: str = TARGET_SERVICE_ID) -> Dict[str, Any]:
    try:
        service_oid = ObjectId(service_id)
    except Exception as exc:
        raise ValueError(f"Invalid Mongo service id: {service_id}") from exc

    services_col = db["services"]
    target = services_col.find_one({"_id": service_oid}, {"_id": 1, "name": 1})
    if not target:
        raise RuntimeError(f"Service document not found: {service_id}")

    offers = fetch_services_offers()
    now = datetime.utcnow()
    result = services_col.update_one(
        {"_id": service_oid},
        {
            "$set": {
                FIELD_NAME: offers,
                "image_url": SOCIAL_BOOSTING_IMAGE_URL,
                "services_offers_count": len(offers),
                "services_offers_provider": "exosupplier",
                "services_offers_synced_at": now,
                "updated_at": now,
            }
        },
    )

    return {
        "service_id": service_id,
        "service_name": target.get("name") or "",
        "offers_count": len(offers),
        "matched_count": result.matched_count,
        "modified_count": result.modified_count,
    }


if __name__ == "__main__":
    target_id = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARGET_SERVICE_ID", TARGET_SERVICE_ID)

    try:
        summary = sync_services_offers(target_id)
        print("Synced ExoSupplier services into MongoDB.")
        print(f"Service: {summary['service_name']} ({summary['service_id']})")
        print(f"Array field: {FIELD_NAME}")
        print(f"Offers inserted: {summary['offers_count']}")
        print(f"Mongo matched: {summary['matched_count']}, modified: {summary['modified_count']}")
    except Exception as exc:
        print("Sync failed:", exc)
        raise
