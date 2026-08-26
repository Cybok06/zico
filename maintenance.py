from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import math
import os
import requests

from bson import ObjectId
from flask import Blueprint, session, redirect, url_for, flash, request

from db import db
from tenant import to_object_id
from paystack_keys import get_paystack_key_pair
from admin_paystack_ledger import record_admin_paystack_credit


maintenance_bp = Blueprint("maintenance", __name__)

users_col = db["users"]
maintenance_accounts_col = db["maintenance_accounts"]
maintenance_payments_col = db["maintenance_payments"]
transactions_col = db["transactions"]
promo_codes_col = db["promo_codes"]


# --------------------
# Config
# --------------------
CYCLE_DAYS = int(os.getenv("MAINTENANCE_CYCLE_DAYS", "30"))
GRACE_DAYS = int(os.getenv("MAINTENANCE_GRACE_DAYS", "5"))

# Tiered fees (GHS)
MAINTENANCE_FEES = {
    "admin": 20.0,
    "super_admin": 15.0,
    "super_professional": 20.0,
}

# Paystack (fallback to existing hardcoded keys if env not set)
PAYSTACK_PUBLIC_KEY = os.getenv(
    "PAYSTACK_PUBLIC_KEY",
    "pk_live_9bfdd68d9b3205e311a3709b19143081ecaf74ee",
)
PAYSTACK_SECRET_KEY = os.getenv(
    "PAYSTACK_SECRET_KEY",
    "sk_live_e8b4e4a02b170e36ee385b839517ce4f1d0bd92b",
)


# --------------------
# Helpers
# --------------------
def _now() -> datetime:
    return datetime.utcnow()


def _normalize_admin_level(raw: str | None) -> str:
    lvl = (raw or "").strip().lower()
    if lvl in {"super_admin", "superadmin"}:
        return "super_admin"
    if lvl in {"super_professional", "professional_admin", "professional"}:
        return "super_professional"
    return "admin"


def _fee_for_level(level: str | None) -> float:
    lvl = _normalize_admin_level(level)
    return float(MAINTENANCE_FEES.get(lvl, MAINTENANCE_FEES["admin"]))


def _safe_dt(value: Any) -> Optional[datetime]:
    return value if isinstance(value, datetime) else None


def _main_admin_id() -> Optional[ObjectId]:
    doc = users_col.find_one({"role": "main_admin"}, {"_id": 1})
    return doc.get("_id") if doc else None


def _default_paid_through(admin_doc: dict) -> datetime:
    created_at = _safe_dt(admin_doc.get("created_at")) or _now()
    return created_at + timedelta(days=CYCLE_DAYS)


def get_maintenance_status(admin_doc: dict | None) -> Dict[str, Any]:
    now = _now()
    if not admin_doc:
        return {
            "exempt": True,
            "status": "unknown",
            "amount_due": 0.0,
            "is_due": False,
            "is_overdue": False,
            "due_soon": False,
        }

    role = (admin_doc.get("role") or "").strip().lower()
    if role == "main_admin":
        return {
            "exempt": True,
            "status": "exempt",
            "amount_due": 0.0,
            "is_due": False,
            "is_overdue": False,
            "due_soon": False,
            "paid_through": None,
            "next_due_at": None,
            "grace_until": None,
            "last_paid_at": None,
            "days_until_due": None,
            "days_overdue": None,
            "level": "main_admin",
        }

    admin_oid = admin_doc.get("_id")
    acct = maintenance_accounts_col.find_one({"admin_id": admin_oid}, {"paid_through": 1, "last_paid_at": 1}) or {}
    paid_through = _safe_dt(acct.get("paid_through"))
    if not paid_through:
        paid_through = _default_paid_through(admin_doc)

    next_due_at = paid_through
    grace_until = next_due_at + timedelta(days=GRACE_DAYS)

    is_due = now >= next_due_at
    is_overdue = now > grace_until

    days_until_due = max(0, math.ceil((next_due_at - now).total_seconds() / 86400.0)) if not is_due else 0
    days_overdue = max(0, math.ceil((now - grace_until).total_seconds() / 86400.0)) if is_overdue else 0
    grace_elapsed_seconds = 0.0
    if is_due:
        grace_elapsed_seconds = max(0.0, (now - next_due_at).total_seconds())
    grace_total_seconds = max(1.0, (grace_until - next_due_at).total_seconds())
    grace_progress_percent = min(100, max(0, int(round((grace_elapsed_seconds / grace_total_seconds) * 100))))
    grace_days_remaining = max(0, math.ceil((grace_until - now).total_seconds() / 86400.0)) if is_due and not is_overdue else 0
    due_soon = (not is_due) and days_until_due <= 5

    level = _normalize_admin_level(admin_doc.get("admin_level"))
    amount_due = _fee_for_level(level)

    status = "ok"
    if is_overdue:
        status = "overdue"
    elif is_due:
        status = "due"
    elif due_soon:
        status = "due_soon"

    return {
        "exempt": False,
        "status": status,
        "amount_due": amount_due,
        "level": level,
        "paid_through": paid_through,
        "next_due_at": next_due_at,
        "grace_until": grace_until,
        "last_paid_at": _safe_dt(acct.get("last_paid_at")),
        "is_due": is_due,
        "is_overdue": is_overdue,
        "due_soon": due_soon,
        "days_until_due": days_until_due,
        "days_overdue": days_overdue,
        "grace_days_remaining": grace_days_remaining,
        "grace_progress_percent": grace_progress_percent,
    }


def is_admin_overdue(admin_doc: dict | None) -> bool:
    status = get_maintenance_status(admin_doc)
    return bool(status.get("is_overdue"))


def get_admin_doc(admin_id: Any) -> Optional[dict]:
    oid = to_object_id(admin_id)
    if not oid:
        return None
    return users_col.find_one({"_id": oid}, {"_id": 1, "role": 1, "admin_level": 1, "created_at": 1})


def get_maintenance_status_for_admin_id(admin_id: Any) -> Dict[str, Any]:
    return get_maintenance_status(get_admin_doc(admin_id))


def get_maintenance_paystack_public_key() -> str:
    public_key, _secret_key = get_paystack_key_pair("deposit")
    return public_key or PAYSTACK_PUBLIC_KEY


def _provider_label(provider: str) -> str:
    key = (provider or "").strip().lower()
    if key == "moolre":
        return "Moolre"
    if key == "promo_code":
        return "Promo Code"
    return "Paystack"


def record_maintenance_payment(
    admin_doc: dict,
    reference: str,
    paid_gross_ghs: float,
    paystack_data: dict,
    provider: str = "paystack",
) -> Dict[str, Any]:
    now = _now()
    admin_oid = admin_doc.get("_id")
    if not isinstance(admin_oid, ObjectId):
        raise ValueError("Invalid admin id")

    acct = maintenance_accounts_col.find_one({"admin_id": admin_oid}, {"paid_through": 1}) or {}
    paid_through = _safe_dt(acct.get("paid_through"))
    if not paid_through:
        paid_through = _default_paid_through(admin_doc)

    period_start = now
    period_end = period_start + timedelta(days=CYCLE_DAYS)

    level = _normalize_admin_level(admin_doc.get("admin_level"))
    amount_due = _fee_for_level(level)

    payment_doc = {
        "admin_id": admin_oid,
        "amount_due": float(amount_due),
        "amount_paid": float(paid_gross_ghs),
        "currency": "GHS",
        "reference": reference,
        "status": "success",
        "paid_at": now,
        "period_start": period_start,
        "period_end": period_end,
        "admin_level": level,
        "provider": provider,
        "raw": paystack_data,
        "created_at": now,
        "updated_at": now,
    }
    maintenance_payments_col.insert_one(payment_doc)

    maintenance_accounts_col.update_one(
        {"admin_id": admin_oid},
        {"$set": {
            "admin_id": admin_oid,
            "paid_through": period_end,
            "last_paid_at": now,
            "status": "paid",
            "updated_at": now,
        },
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )

    # Also log into transactions for audit (optional but useful)
    transactions_col.insert_one(
        {
            "user_id": admin_oid,
            "admin_id": admin_oid,
            "amount": float(amount_due),
            "reference": reference,
            "status": "success",
            "type": "maintenance_fee",
            "source": "admin_subscription",
            "gateway": _provider_label(provider),
            "payment_provider": provider,
            "payment_reference": reference,
            "payment_gateway": _provider_label(provider),
            "payment_status": "success",
            "payment_verified_at": now,
            "payment_raw": paystack_data,
            "currency": "GHS",
            "created_at": now,
            "verified_at": now,
            "meta": {
                "source": "admin_subscription",
                "payment_provider": provider,
                "moolre": paystack_data if provider == "moolre" else {},
                "paystack_profile": "subscription",
                "maintenance_fee": True,
                "paid_gross_ghs": float(paid_gross_ghs),
                "period_start": period_start,
                "period_end": period_end,
                "admin_level": level,
            },
        }
    )

    main_admin_oid = _main_admin_id()
    if main_admin_oid:
        try:
            record_admin_paystack_credit(
                admin_id=main_admin_oid,
                amount=float(amount_due),
                profile="subscription",
                reference=reference,
                meta={
                    "source": "admin_subscription",
                    "paying_admin_id": admin_oid,
                    "amount_due": float(amount_due),
                    "paid_gross_ghs": float(paid_gross_ghs),
                    "paystack_credit_ghs": float(amount_due),
                    "gateway_overage_ghs": max(0.0, round(float(paid_gross_ghs) - float(amount_due), 2)),
                    "period_start": period_start,
                    "period_end": period_end,
                    "admin_level": level,
                },
            )
        except Exception:
            pass

    return payment_doc


def redeem_maintenance_promo_code(admin_doc: dict, code: str, redeemed_by: Any = None) -> Dict[str, Any]:
    now = _now()
    admin_oid = admin_doc.get("_id")
    if not isinstance(admin_oid, ObjectId):
        raise ValueError("Invalid admin id")
    if (admin_doc.get("role") or "").strip().lower() == "main_admin":
        raise ValueError("Main admin is exempt from subscriptions.")

    normalized_code = str(code or "").strip().upper()
    if not normalized_code:
        raise ValueError("Promo code is required.")

    code_doc = promo_codes_col.find_one({"code": normalized_code})
    if not code_doc:
        raise ValueError("Promo code not found.")
    if (code_doc.get("status") or "").strip().lower() == "used":
        raise ValueError("Promo code has already been used.")

    reference = f"PROMO-{normalized_code}"
    if maintenance_payments_col.find_one({"reference": reference, "status": "success"}):
        raise ValueError("Promo code has already been redeemed.")
    if transactions_col.find_one({"reference": reference, "status": "success"}):
        raise ValueError("Promo code has already been redeemed.")

    claimed = promo_codes_col.update_one(
        {"_id": code_doc["_id"], "status": {"$ne": "used"}},
        {
            "$set": {
                "status": "used",
                "used_at": now,
                "used_by_admin_id": admin_oid,
                "used_by_role": admin_doc.get("role") or "admin",
                "used_by_username": admin_doc.get("username") or "",
                "used_by_business_name": admin_doc.get("business_name") or "",
                "updated_at": now,
                "redeemed_by": redeemed_by,
            }
        },
    )
    if not claimed.modified_count:
        raise ValueError("Promo code has already been used.")

    return record_maintenance_payment(
        admin_doc,
        reference,
        float(code_doc.get("amount") or _fee_for_level(admin_doc.get("admin_level"))),
        {
            "promo_code": normalized_code,
            "promo_code_id": str(code_doc.get("_id")),
            "redeemed_at": now.isoformat(),
        },
        provider="promo_code",
    )


def get_payment_history(admin_id: Any, limit: int = 25) -> List[dict]:
    oid = to_object_id(admin_id)
    if not oid:
        return []
    return list(
        maintenance_payments_col.find({"admin_id": oid})
        .sort([("paid_at", -1)])
        .limit(int(limit))
    )


def _verify_paystack_reference(reference: str) -> dict:
    _pk, secret_key = get_paystack_key_pair("deposit")
    secret = secret_key or PAYSTACK_SECRET_KEY
    headers = {"Authorization": f"Bearer {secret}"}
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    resp = requests.get(url, headers=headers, timeout=20)
    return resp.json()


# --------------------
# Routes
# --------------------
@maintenance_bp.route("/admin/billing/verify")
def verify_maintenance_payment():
    role = (session.get("role") or "").strip().lower()
    if role not in {"admin", "main_admin"} or not session.get("user_id"):
        flash("Unauthorized.", "danger")
        return redirect(url_for("login.login"))

    reference = (request.args.get("reference") or "").strip()
    if not reference:
        flash("Invalid payment reference.", "danger")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))

    admin_oid = to_object_id(session.get("user_id"))
    admin_doc = get_admin_doc(admin_oid)
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        flash("Main admin is exempt from maintenance fees.", "info")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))

    # Prevent double-use of Paystack reference
    if maintenance_payments_col.find_one({"reference": reference, "status": "success"}):
        flash("Payment already verified.", "info")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))
    if transactions_col.find_one({"reference": reference, "status": "success"}):
        flash("Payment reference already used.", "warning")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))

    try:
        result = _verify_paystack_reference(reference)
        ok = result.get("status") and result.get("data", {}).get("status") == "success"
        if not ok:
            msg = result.get("message") or result.get("data", {}).get("gateway_response") or "Verification failed."
            flash(f"Payment verification failed: {msg}", "danger")
            return redirect(url_for("admin_profile.admin_profile", tab="billing"))

        data = result["data"]
        paid_gross_ghs = float((data.get("amount", 0) or 0) / 100.0)
        currency = data.get("currency", "GHS")
        if currency != "GHS" or paid_gross_ghs <= 0:
            flash("Invalid payment currency/amount.", "danger")
            return redirect(url_for("admin_profile.admin_profile", tab="billing"))

        status = get_maintenance_status(admin_doc)
        amount_due = float(status.get("amount_due") or 0.0)
        if paid_gross_ghs + 0.01 < amount_due:
            flash(f"Amount paid (GHS {paid_gross_ghs:.2f}) is less than the required fee (GHS {amount_due:.2f}).", "danger")
            return redirect(url_for("admin_profile.admin_profile", tab="billing"))

        record_maintenance_payment(admin_doc, reference, paid_gross_ghs, data)
        session.pop("maintenance_locked", None)
        flash("Monthly subscription paid successfully. Access restored.", "success")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))

    except Exception as e:
        flash(f"Could not verify payment: {e}", "danger")
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))
