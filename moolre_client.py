from __future__ import annotations

import os
from typing import Any, Dict

import requests


DEFAULT_BASE_URL = "https://api.moolre.com"
SANDBOX_BASE_URL = "https://sandbox.moolre.com"


def _clean(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def get_moolre_config() -> Dict[str, Any]:
    sandbox = _clean(os.getenv("MOOLRE_SANDBOX")).lower() in {"1", "true", "yes", "on"}
    base_url = _clean(os.getenv("MOOLRE_BASE_URL"))
    if sandbox and not base_url:
        base_url = SANDBOX_BASE_URL
    return {
        "api_user": _clean(os.getenv("MOOLRE_API_USER")),
        "public_key": _clean(os.getenv("MOOLRE_PUBLIC_KEY")),
        "account_number": _clean(os.getenv("MOOLRE_ACCOUNT_NUMBER")),
        "base_url": (base_url or DEFAULT_BASE_URL).rstrip("/"),
        "callback_url": _clean(os.getenv("MOOLRE_CALLBACK_URL"))
        or "https://azico.site/payments/moolre/callback",
        "sandbox": sandbox,
    }


def _headers(config: Dict[str, Any]) -> Dict[str, str]:
    return {
        "X-API-USER": config.get("api_user", ""),
        "X-API-PUBKEY": config.get("public_key", ""),
        "Content-Type": "application/json",
    }


def _require_config(config: Dict[str, Any]) -> None:
    missing = [
        key
        for key in ("api_user", "public_key", "account_number")
        if not _clean(config.get(key))
    ]
    if missing:
        raise RuntimeError("Moolre is not configured: missing " + ", ".join(missing))


def create_payment_link(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = get_moolre_config()
    _require_config(config)
    body = dict(payload or {})
    body.setdefault("type", 1)
    body.setdefault("currency", "GHS")
    body.setdefault("reusable", "0")
    body.setdefault("accountnumber", config["account_number"])
    body.setdefault("callback", config["callback_url"])
    url = f"{config['base_url']}/embed/link"
    resp = requests.post(url, headers=_headers(config), json=body, timeout=25)
    try:
        data = resp.json()
    except Exception:
        data = {"status": 0, "message": resp.text}
    data["_http_status"] = resp.status_code
    return data


def verify_payment_status(externalref: str) -> Dict[str, Any]:
    config = get_moolre_config()
    _require_config(config)
    body = {
        "type": 1,
        "idtype": "1",
        "id": externalref,
        "accountnumber": config["account_number"],
    }
    url = f"{config['base_url']}/open/transact/status"
    resp = requests.post(url, headers=_headers(config), json=body, timeout=25)
    try:
        data = resp.json()
    except Exception:
        data = {"status": 0, "message": resp.text}
    data["_http_status"] = resp.status_code
    return data


def normalize_moolre_callback(payload: Dict[str, Any]) -> Dict[str, Any]:
    src = payload or {}
    data = src.get("data") if isinstance(src.get("data"), dict) else src
    reference = (
        data.get("externalref")
        or data.get("externalRef")
        or data.get("external_ref")
        or data.get("reference")
        or data.get("ref")
        or src.get("externalref")
        or src.get("reference")
        or ""
    )
    return {
        "reference": str(reference or "").strip(),
        "moolre_reference": str(data.get("reference") or data.get("transactionid") or data.get("transid") or "").strip(),
        "data": data,
        "raw": src,
    }


def is_successful_moolre_payment(data: Dict[str, Any]) -> bool:
    root = data or {}
    tx = root.get("data") if isinstance(root.get("data"), dict) else root
    try:
        status_ok = int(root.get("status") or 0) == 1
    except Exception:
        status_ok = False
    try:
        tx_ok = int(tx.get("txstatus") or tx.get("status") or 0) == 1
    except Exception:
        tx_ok = False
    return status_ok and tx_ok


def _amount(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def safe_amount_match(paid: Any, expected: Any) -> bool:
    return _amount(paid) + 0.01 >= _amount(expected)
