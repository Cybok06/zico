"""BundlePortal v2 transport and catalogue helpers (no database side effects)."""
from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

NETWORKS = {"mtn", "mtn_2", "mtn_3", "telecel", "airteltigo", "ishare"}
ROUTE_CHOICES = {
    "bundleportal_mtn": ("mtn", "Bundle Portal MTN"),
    "bundleportal_mtn2": ("mtn_2", "Bundle Portal MTN2"),
    "bundleportal_mtn3": ("mtn_3", "Bundle Portal MTN3"),
    "bundleportal_telecel": ("telecel", "Bundle Portal Telecel"),
    "bundleportal_ishare": ("airteltigo", "Bundle Portal AT iShare"),
}


def route_choices(service):
    name = str((service or {}).get("name") or "").lower()
    if "bigtime" in name or "big time" in name:
        return {}
    network = resolve_network(service, {}, None)
    if "mtn" in name or network in {"mtn", "mtn_2", "mtn_3"}:
        keys = ("bundleportal_mtn", "bundleportal_mtn2", "bundleportal_mtn3")
    elif "ishare" in name or "i share" in name:
        keys = ("bundleportal_ishare",)
    elif "telecel" in name or "vodafone" in name or network == "telecel":
        keys = ("bundleportal_telecel",)
    else:
        keys = ()
    return {key: ROUTE_CHOICES[key] for key in keys}


def selected_route(service):
    if service.get("provider") != "bundleportal":
        return service.get("provider") or "portal02"
    network = resolve_network(service, {})
    for key, (route, _) in route_choices(service).items():
        if route == network or (route == "airteltigo" and network == "ishare"):
            return key
    return "bundleportal"
_catalogue = {}


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("233") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif len(digits) == 9:
        digits = "0" + digits
    return digits


def resolve_network(service, item, fallback=None):
    for source in (service or {}, item or {}):
        for key in ("bundleportal_network", "provider_network", "service_network", "network", "network_name", "name", "serviceName"):
            value = str(source.get(key) or "").strip().lower()
            slug = re.sub(r"[ -]+", "_", value)
            if slug == "mtn_1":
                return "mtn"
            if slug in NETWORKS:
                return slug
            route = re.search(r"\bmtn[_ -]([123])\b", value)
            if route:
                return "mtn" if route[1] == "1" else "mtn_" + route[1]
    labels = " ".join(str(source.get(key) or "") for source in (service or {}, item or {})
                      for key in ("service_network", "network", "network_name", "name", "serviceName")).lower()
    if "ishare" in labels or "i share" in labels:
        return "ishare"
    if "bigtime" in labels or "big time" in labels:
        return "airteltigo"
    if "telecel" in labels or "vodafone" in labels:
        return "telecel"
    if "mtn" in labels:
        return "mtn"
    if "airteltigo" in labels or "airtel tigo" in labels or re.search(r"\bat\b", labels):
        return "airteltigo"
    return fallback


def build_line_and_job(base_line, *, value_obj, network, reference, service_id, line_index):
    """Build a checkout line without spending or authorizing unpaid work."""
    phone = normalize_phone(base_line.get("phone"))
    size = package_size(value_obj, base_line)
    line = {**base_line, "phone": phone, "provider": "bundleportal", "provider_network": network,
            "provider_gig": size, "provider_request_order_id": reference,
            "provider_reference": None, "provider_order_id": None,
            "line_status": "processing", "api_status": "queued",
            "api_response": {"note": "Queued for BundlePortal API call"}}
    if not re.fullmatch(r"0\d{9}", phone) or network not in NETWORKS or size is None:
        line.update(api_status="skipped_missing_fields",
                    api_response={"message": "BundlePortal recipient, network or size is invalid; manual review is required."})
        return line, None
    job = {"provider": "bundleportal", "provider_request_order_id": reference, "phone": phone,
           "provider_network": network, "provider_gig": size, "service_id": service_id,
           "line_index": line_index}
    return line, job


def package_size(value, item):
    value = value if isinstance(value, dict) else {}
    for key in ("size_gb", "gb", "gb_size", "package_size", "volume_gb"):
        if value.get(key) not in (None, ""):
            try:
                size = float(value[key])
                return size if math.isfinite(size) and size > 0 else None
            except (TypeError, ValueError):
                return None
    label = str(item.get("value") or "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", label, re.I)
    if match:
        # Azico's store and dashboard express bundle MB using 1000 MB per GB.
        size = float(match[1]) / (1000 if match[2].upper() == "MB" else 1)
        return size if size > 0 else None
    # Match Azico's existing store/dashboard volume labels; validate in catalogue.
    try:
        size = float(value.get("volume"))
        size = size / 1000 if size > 50 else size
        return size if math.isfinite(size) and size > 0 else None
    except (TypeError, ValueError):
        return None


def retry_after(header, payload):
    delays = [0.0]
    for value in (header, payload.get("retry_after")):
        try:
            delay = float(value)
            if math.isfinite(delay):
                delays.append(delay)
        except (TypeError, ValueError):
            try:
                delays.append((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return max(delays)


def call(action, **fields):
    key = os.getenv("BUNDLEPORTAL_API_KEY", "").strip()
    if not key:
        return {"success": False, "code": "configuration", "message": "BundlePortal API key is not configured.", "http_status": 401}
    url = os.getenv("BUNDLEPORTAL_BASE_URL", "https://api.bundleportal.com/v2").rstrip("/")
    if url != "https://api.bundleportal.com/v2":
        return {"success": False, "code": "configuration", "message": "Set BUNDLEPORTAL_BASE_URL to https://api.bundleportal.com/v2.", "http_status": 400}
    try:
        response = requests.post(url, headers={"x-api-key": key, "Content-Type": "application/json"},
                                 json={"action": action, **fields}, timeout=(5, float(os.getenv("BUNDLEPORTAL_TIMEOUT", "45"))))
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            payload = {"success": False, "code": "invalid_response", "message": "Provider response could not be confirmed."}
        if payload.get("success") and not isinstance(payload.get("data"), dict):
            payload = {"success": False, "code": "invalid_response", "message": "Provider response could not be confirmed."}
        payload["http_status"] = response.status_code
        payload["retry_after"] = retry_after(response.headers.get("Retry-After"), payload)
        if not response.ok:
            payload["success"] = False
        return payload
    except requests.RequestException:
        return {"success": False, "code": "network_error", "message": "Provider response could not be confirmed.", "http_status": 599}


def preflight(phone, network, size, order_id):
    if network not in NETWORKS or not re.fullmatch(r"0\d{9}", phone) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(order_id or "")):
        return {"success": False, "code": "validation", "message": "Invalid BundlePortal network, recipient or order reference.", "http_status": 400}
    try:
        if not math.isfinite(float(size)) or float(size) <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return {"success": False, "code": "validation", "message": "Invalid bundle size.", "http_status": 400}
    verified = call("verify_number", network=network, recipient=phone)
    if not verified.get("success"):
        return verified
    data = verified.get("data") or {}
    if data.get("can_order") is not True:
        allowed = data.get("allowed") is True
        return {"success": False, "code": "pending_order" if allowed else "not_allowlisted",
                "message": data.get("allowlist_message") or ("An earlier order is still being delivered." if allowed else "This number is awaiting approval."),
                "http_status": 409 if allowed else 403, "data": data}
    cached = _catalogue.get(network)
    if not cached or cached[0] < time.monotonic():
        result = call("get_bundles", network=network)
        if not result.get("success"):
            return result
        bundles = (result.get("data") or {}).get("bundles", [])
        if not isinstance(bundles, list):
            return {"success": False, "code": "invalid_response", "http_status": 502, "message": "Provider catalogue could not be confirmed."}
        _catalogue[network] = (time.monotonic() + 60, bundles)
    else:
        bundles = cached[1]
    for bundle in bundles:
        try:
            if float(bundle["size_gb"]) == float(size):
                return None
        except (KeyError, TypeError, ValueError):
            continue
    return {"success": False, "code": "validation", "message": "This size is not available on the selected BundlePortal route.", "http_status": 400}


def outcome(payload, spending=False):
    if payload.get("success") is True:
        status = (payload.get("data") or {}).get("status", "processing")
        return status if status in {"processing", "cached", "completed", "failed"} else "processing"
    code, http = payload.get("code"), payload.get("http_status", 500)
    if code == "not_allowlisted":
        return "pending_approval"
    if code in {"network_error", "invalid_response"} or http >= 500:
        return "unconfirmed" if spending else "retry_pending"
    if http in {409, 429}:
        return "retry_pending"
    if http in {401, 402, 403} or code == "configuration":
        return "review_required"
    return "failed"
