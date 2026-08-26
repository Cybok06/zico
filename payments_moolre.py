from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional
import uuid

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, request, session, url_for
from pymongo import ASCENDING, DESCENDING

from db import db
from tenant import current_admin_id_from_session, is_admin_role, resolve_admin_id_for_user_id, to_object_id
from moolre_client import (
    create_payment_link,
    get_moolre_config,
    is_successful_moolre_payment,
    normalize_moolre_callback,
    safe_amount_match,
    verify_payment_status,
)
from admin_paystack_ledger import record_admin_paystack_credit


moolre_payments_bp = Blueprint("moolre_payments", __name__)

payment_intents_col = db["payment_intents"]
audit_moolre_col = db["audit_moolre"]
users_col = db["users"]
balances_col = db["balances"]
transactions_col = db["transactions"]
balance_logs_col = db["balance_logs"]
maintenance_payments_col = db["maintenance_payments"]

MOOLRE_FEE_RATE = 0.005
MIN_AGENT_DEPOSIT_GHS = 10.0
MIN_ADMIN_WALLET_DEPOSIT_GHS = 50.0


@moolre_payments_bp.errorhandler(Exception)
def _moolre_json_error(exc: Exception):
    current_app.logger.exception("Moolre payment route failed")
    return jsonify({"success": False, "message": str(exc) or "Payment request failed."}), 500


def _ensure_indexes() -> None:
    try:
        payment_intents_col.create_index([("provider", ASCENDING), ("reference", ASCENDING)], unique=True, background=True)
        payment_intents_col.create_index([("intent_id", ASCENDING)], unique=True, background=True)
        payment_intents_col.create_index([("status", ASCENDING), ("created_at", DESCENDING)], background=True)
        payment_intents_col.create_index([("flow", ASCENDING), ("reference", ASCENDING)], background=True)
    except Exception:
        pass


_ensure_indexes()


def _now() -> datetime:
    return datetime.utcnow()


def _r2(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _decimal_amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _round_half_up_pesewas(amount_ghs: Any) -> int:
    return int((_decimal_amount(amount_ghs) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fee_inclusive(base_ghs: Any, fee_rate: float = MOOLRE_FEE_RATE) -> Dict[str, Any]:
    base = _decimal_amount(base_ghs)
    base_pes = _round_half_up_pesewas(base)
    fee_pes = int((base * Decimal(str(fee_rate)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    gross_pes = base_pes + fee_pes
    return {
        "net_ghs": _r2(base_pes / 100.0),
        "fee_ghs": _r2(fee_pes / 100.0),
        "gross_ghs": _r2(gross_pes / 100.0),
        "gross_pesewas": gross_pes,
    }


def _safe_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _extract_paid_amount(verify_response: Dict[str, Any]) -> float:
    data = verify_response.get("data") if isinstance(verify_response.get("data"), dict) else verify_response
    for key in ("amount", "amountpaid", "amount_paid", "totalamount"):
        if data.get(key) not in (None, ""):
            amount = _r2(data.get(key))
            if amount > 100000 and amount == int(amount):
                return _r2(amount / 100.0)
            return amount
    return 0.0


def _extract_currency(verify_response: Dict[str, Any]) -> str:
    data = verify_response.get("data") if isinstance(verify_response.get("data"), dict) else verify_response
    return str(data.get("currency") or "GHS").upper()


def _extract_moolre_reference(verify_response: Dict[str, Any]) -> str:
    data = verify_response.get("data") if isinstance(verify_response.get("data"), dict) else verify_response
    return str(data.get("reference") or data.get("transactionid") or data.get("transid") or "").strip()


def _absolute_url(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    try:
        base = request.url_root.rstrip("/")
    except RuntimeError:
        base = "https://azico.site"
    return base + (raw if raw.startswith("/") else f"/{raw}")


def _default_redirect(flow: str, reference: str) -> str:
    return _absolute_url(f"/payments/moolre/redirect?reference={reference}")


def _new_reference(flow: str) -> str:
    prefix = {
        "store_checkout": "MLR-ST",
        "agent_deposit": "MLR-DEP",
        "admin_wallet_deposit": "MLR-ADM",
        "admin_subscription": "MLR-SUB",
    }.get(flow, "MLR")
    return f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"


def _intent_public(intent: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "intent_id": intent.get("intent_id"),
        "reference": intent.get("reference"),
        "authorization_url": intent.get("authorization_url"),
        "status": intent.get("status"),
    }


def _user_email(user_id: Any, fallback: str = "") -> str:
    email = (fallback or "").strip()
    if email:
        return email
    oid = _safe_oid(user_id)
    if not oid:
        return ""
    user = users_col.find_one({"_id": oid}, {"email": 1}) or {}
    return (user.get("email") or "").strip()


def _create_store_intent(payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], tuple[Any, int] | None]:
    from routes.store_page import stores_col, _server_reprice_store_cart, _store_admin_id

    slug = (payload.get("store_slug") or payload.get("slug") or "").strip()
    cart = payload.get("cart") or []
    if not slug or not isinstance(cart, list) or not cart:
        return None, (jsonify({"success": False, "message": "Store slug and cart are required."}), 400)
    store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if not store_doc:
        return None, (jsonify({"success": False, "message": "Store not found."}), 404)
    repriced_cart, base_total = _server_reprice_store_cart(store_doc, cart)
    if base_total <= 0:
        return None, (jsonify({"success": False, "message": "Total amount must be greater than zero."}), 400)
    charge = _fee_inclusive(base_total)
    admin_id = _store_admin_id(store_doc)
    payer = payload.get("payer") if isinstance(payload.get("payer"), dict) else {}
    email = (payer.get("email") or payload.get("email") or _user_email(store_doc.get("owner_id"), "")).strip()
    if not email:
        email = "store-buyer@zishop.site"
    return {
        "flow": "store_checkout",
        "admin_id": admin_id,
        "user_id": _safe_oid(session.get("user_id")) or store_doc.get("owner_id"),
        "store_slug": slug,
        "email": email,
        "expected_amount_ghs": charge["gross_ghs"],
        "net_amount_ghs": charge["net_ghs"],
        "fee_ghs": charge["fee_ghs"],
        "metadata": {
            "cart": _json_safe(repriced_cart),
            "payer": _json_safe(payer),
            "base_total_ghs": charge["net_ghs"],
            "fee_rate": MOOLRE_FEE_RATE,
        },
    }, None


def _create_agent_deposit_intent(payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], tuple[Any, int] | None]:
    if (session.get("role") or "").strip().lower() not in {"agent", "customer"} or not session.get("user_id"):
        return None, (jsonify({"success": False, "message": "Login required."}), 401)
    net = _r2(payload.get("amount") or payload.get("net_amount_ghs"))
    if net < MIN_AGENT_DEPOSIT_GHS:
        return None, (jsonify({"success": False, "message": f"Minimum deposit is GHS {MIN_AGENT_DEPOSIT_GHS:.2f}."}), 400)
    user_oid = _safe_oid(session.get("user_id"))
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    if not admin_id:
        return None, (jsonify({"success": False, "message": "Account is not mapped to an admin."}), 400)
    charge = _fee_inclusive(net)
    return {
        "flow": "agent_deposit",
        "admin_id": admin_id,
        "user_id": user_oid,
        "email": _user_email(user_oid, session.get("email") or ""),
        "expected_amount_ghs": charge["gross_ghs"],
        "net_amount_ghs": charge["net_ghs"],
        "fee_ghs": charge["fee_ghs"],
        "metadata": {"fee_rate": MOOLRE_FEE_RATE},
    }, None


def _create_admin_wallet_intent(payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], tuple[Any, int] | None]:
    if not is_admin_role(session.get("role")) or not session.get("user_id"):
        return None, (jsonify({"success": False, "message": "Admin login required."}), 401)
    net = _r2(payload.get("amount") or payload.get("net_amount_ghs"))
    if net < MIN_ADMIN_WALLET_DEPOSIT_GHS:
        return None, (jsonify({"success": False, "message": f"Minimum deposit is GHS {MIN_ADMIN_WALLET_DEPOSIT_GHS:.2f}."}), 400)
    user_oid = _safe_oid(session.get("user_id"))
    admin_id = current_admin_id_from_session(session) or user_oid
    charge = _fee_inclusive(net)
    return {
        "flow": "admin_wallet_deposit",
        "admin_id": admin_id,
        "user_id": user_oid,
        "email": _user_email(user_oid, session.get("email") or ""),
        "expected_amount_ghs": charge["gross_ghs"],
        "net_amount_ghs": charge["net_ghs"],
        "fee_ghs": charge["fee_ghs"],
        "metadata": {"fee_rate": MOOLRE_FEE_RATE, "wallet_scope": "admin_self_wallet"},
    }, None


def _create_subscription_intent(payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], tuple[Any, int] | None]:
    from maintenance import get_admin_doc, get_maintenance_status

    role = (session.get("role") or "").strip().lower()
    if role not in {"admin", "main_admin"} or not session.get("user_id"):
        return None, (jsonify({"success": False, "message": "Admin login required."}), 401)
    user_oid = _safe_oid(session.get("user_id"))
    admin_doc = get_admin_doc(user_oid)
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        return None, (jsonify({"success": False, "message": "Main admin is exempt from subscriptions."}), 400)
    status = get_maintenance_status(admin_doc)
    amount_due = _r2(status.get("amount_due"))
    if amount_due <= 0:
        return None, (jsonify({"success": False, "message": "Invalid subscription amount."}), 400)
    return {
        "flow": "admin_subscription",
        "admin_id": user_oid,
        "user_id": user_oid,
        "email": _user_email(user_oid, session.get("email") or ""),
        "expected_amount_ghs": amount_due,
        "net_amount_ghs": amount_due,
        "fee_ghs": 0.0,
        "metadata": {"maintenance_fee": True, "admin_subscription": True},
    }, None


@moolre_payments_bp.route("/payments/moolre/create", methods=["POST"])
def create_moolre_payment():
    payload = request.get_json(silent=True) or {}
    flow = (payload.get("flow") or "").strip().lower()
    builders = {
        "store_checkout": _create_store_intent,
        "agent_deposit": _create_agent_deposit_intent,
        "admin_wallet_deposit": _create_admin_wallet_intent,
        "admin_subscription": _create_subscription_intent,
    }
    if flow not in builders:
        return jsonify({"success": False, "message": "Invalid payment flow."}), 400

    base, error = builders[flow](payload)
    if error:
        return error
    assert base is not None

    config = get_moolre_config()
    reference = _new_reference(flow)
    intent_id = uuid.uuid4().hex
    redirect_raw = payload.get("redirect_url") or _default_redirect(flow, reference)
    redirect_url = _absolute_url(redirect_raw)
    if "reference=" not in redirect_url and "externalref=" not in redirect_url:
        sep = "&" if "?" in redirect_url else "?"
        redirect_url = f"{redirect_url}{sep}reference={reference}"
    now = _now()
    metadata = dict(base.get("metadata") or {})
    metadata.update(
        {
            "flow": flow,
            "admin_id": str(base.get("admin_id") or ""),
            "user_id": str(base.get("user_id") or ""),
            "store_slug": base.get("store_slug") or "",
            "intent_id": intent_id,
            "expected_amount_ghs": f"{base['expected_amount_ghs']:.2f}",
            "net_amount_ghs": f"{base['net_amount_ghs']:.2f}",
            "fee_ghs": f"{base['fee_ghs']:.2f}",
        }
    )
    intent_doc = {
        "intent_id": intent_id,
        "provider": "moolre",
        "flow": flow,
        "status": "pending",
        "reference": reference,
        "moolre_reference": "",
        "authorization_url": "",
        "admin_id": base.get("admin_id"),
        "user_id": base.get("user_id"),
        "store_slug": base.get("store_slug") or "",
        "expected_amount_ghs": base["expected_amount_ghs"],
        "net_amount_ghs": base["net_amount_ghs"],
        "fee_ghs": base["fee_ghs"],
        "currency": "GHS",
        "metadata": _json_safe(metadata),
        "redirect_url": redirect_url,
        "created_at": now,
        "updated_at": now,
        "processing_lock": False,
    }
    payment_intents_col.insert_one(intent_doc)

    create_payload = {
        "type": 1,
        "amount": f"{base['expected_amount_ghs']:.2f}",
        "email": base.get("email") or "billing@azico.site",
        "externalref": reference,
        "callback": config["callback_url"],
        "redirect": redirect_url,
        "reusable": "0",
        "currency": "GHS",
        "accountnumber": config["account_number"],
        "metadata": _json_safe(metadata),
    }
    try:
        create_resp = create_payment_link(create_payload)
    except Exception as exc:
        payment_intents_col.update_one(
            {"intent_id": intent_id},
            {"$set": {"status": "failed", "updated_at": _now(), "raw_create_response": {"error": str(exc)}}},
        )
        return jsonify({"success": False, "message": str(exc)}), 500

    data = create_resp.get("data") if isinstance(create_resp.get("data"), dict) else {}
    auth_url = str(data.get("authorization_url") or data.get("url") or "").strip()
    moolre_ref = str(data.get("reference") or "").strip()
    if int(create_resp.get("status") or 0) != 1 or not auth_url:
        payment_intents_col.update_one(
            {"intent_id": intent_id},
            {"$set": {"status": "failed", "updated_at": _now(), "raw_create_response": create_resp}},
        )
        return jsonify({"success": False, "message": create_resp.get("message") or "Unable to create payment link."}), 400
    if not auth_url.startswith(("http://", "https://")):
        payment_intents_col.update_one(
            {"intent_id": intent_id},
            {"$set": {"status": "failed", "updated_at": _now(), "raw_create_response": create_resp}},
        )
        return jsonify({"success": False, "message": "Payment provider returned an invalid checkout link."}), 502

    payment_intents_col.update_one(
        {"intent_id": intent_id},
        {
            "$set": {
                "authorization_url": auth_url,
                "moolre_reference": moolre_ref,
                "raw_create_response": create_resp,
                "updated_at": _now(),
            }
        },
    )
    intent_doc.update({"authorization_url": auth_url, "moolre_reference": moolre_ref})
    return jsonify(_intent_public(intent_doc)), 200


def _audit(intent: Dict[str, Any], verify_ok: bool, verify_response: Dict[str, Any], raw_callback: Any = None, message: str = "") -> None:
    try:
        audit_moolre_col.insert_one(
            {
                "created_at": _now(),
                "flow": intent.get("flow"),
                "reference": intent.get("reference"),
                "intent_id": intent.get("intent_id"),
                "verify_ok": bool(verify_ok),
                "paid_ghs": _extract_paid_amount(verify_response),
                "expected_ghs": intent.get("expected_amount_ghs"),
                "moolre_status": (verify_response.get("data") or {}).get("txstatus") if isinstance(verify_response.get("data"), dict) else verify_response.get("txstatus"),
                "response_message": message or verify_response.get("message") or "",
                "raw_callback": raw_callback,
                "raw_verify_data": verify_response,
            }
        )
    except Exception:
        pass


def _verified_intent(reference: str, raw_callback: Any = None) -> tuple[Optional[Dict[str, Any]], str]:
    intent = payment_intents_col.find_one({"provider": "moolre", "reference": reference})
    if not intent:
        return None, "intent_not_found"
    if intent.get("status") == "success" and intent.get("processed_at"):
        return intent, "already_processed"
    if intent.get("status") == "success" and not intent.get("processed_at"):
        return intent, "verified"

    lock_id = uuid.uuid4().hex
    lock_result = payment_intents_col.update_one(
        {
            "_id": intent["_id"],
            "status": {"$ne": "success"},
            "$or": [{"processing_lock": {"$ne": True}}, {"processing_lock": {"$exists": False}}],
        },
        {"$set": {"processing_lock": True, "processing_lock_id": lock_id, "status": "processing", "updated_at": _now()}},
    )
    if not lock_result.modified_count:
        return payment_intents_col.find_one({"_id": intent["_id"]}), "locked"

    try:
        verify_response = verify_payment_status(reference)
        paid = _extract_paid_amount(verify_response)
        currency = _extract_currency(verify_response)
        account = str((verify_response.get("data") or {}).get("accountnumber") or "").strip() if isinstance(verify_response.get("data"), dict) else ""
        expected_account = get_moolre_config().get("account_number")
        amount_ok = safe_amount_match(paid, intent.get("expected_amount_ghs")) or (
            paid > 100 and safe_amount_match(_r2(paid / 100.0), intent.get("expected_amount_ghs"))
        )
        success = (
            is_successful_moolre_payment(verify_response)
            and str((verify_response.get("data") or {}).get("externalref") or reference).strip() == reference
            and amount_ok
            and currency == "GHS"
            and (not account or account == expected_account)
        )
        _audit(intent, success, verify_response, raw_callback=raw_callback)
        if not success:
            payment_intents_col.update_one(
                {"_id": intent["_id"]},
                {
                    "$set": {
                        "status": "failed",
                        "raw_verify_response": verify_response,
                        "updated_at": _now(),
                        "processing_lock": False,
                    }
                },
            )
            return payment_intents_col.find_one({"_id": intent["_id"]}), "verify_failed"

        update = {
            "status": "success",
            "raw_verify_response": verify_response,
            "moolre_reference": _extract_moolre_reference(verify_response) or intent.get("moolre_reference", ""),
            "paid_at": _now(),
            "updated_at": _now(),
        }
        payment_intents_col.update_one({"_id": intent["_id"]}, {"$set": update})
        intent.update(update)
        return intent, "verified"
    except Exception as exc:
        payment_intents_col.update_one(
            {"_id": intent["_id"]},
            {"$set": {"status": "pending", "processing_lock": False, "updated_at": _now(), "last_error": str(exc)}},
        )
        return payment_intents_col.find_one({"_id": intent["_id"]}), "verify_error"


def finalize_agent_deposit_payment(intent: Dict[str, Any]) -> Dict[str, Any]:
    user_oid = _safe_oid(intent.get("user_id"))
    admin_id = _safe_oid(intent.get("admin_id"))
    reference = intent.get("reference")
    if not user_oid or not admin_id or not reference:
        return {"ok": False, "message": "Invalid deposit intent."}
    if transactions_col.find_one({"reference": reference, "status": "success"}):
        return {"ok": True, "duplicate": True}
    now = _now()
    net = _r2(intent.get("net_amount_ghs"))
    verify_raw = intent.get("raw_verify_response") or {}
    balances_col.update_one(
        {"user_id": user_oid},
        {
            "$inc": {"amount": net},
            "$set": {"updated_at": now, "admin_id": admin_id},
            "$setOnInsert": {"created_at": now, "currency": "GHS"},
        },
        upsert=True,
    )
    txn_res = transactions_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "amount": net,
            "reference": reference,
            "status": "success",
            "type": "deposit",
            "source": "wallet_deposit",
            "gateway": "Moolre",
            "currency": "GHS",
            "raw": verify_raw,
            "verified_at": now,
            "created_at": now,
            "payment_provider": "moolre",
            "payment_reference": reference,
            "payment_gateway": "Moolre",
            "payment_status": "success",
            "payment_verified_at": now,
            "payment_raw": verify_raw,
            "meta": {
                "payment_provider": "moolre",
                "moolre": verify_raw,
                "paid_gross_ghs": intent.get("expected_amount_ghs"),
                "net_credit_ghs": net,
                "fee_ghs": intent.get("fee_ghs"),
                "fee_rate": MOOLRE_FEE_RATE,
                "wallet_owner_user_id": str(user_oid),
                "source": "user_wallet_deposit_fee_0p5_minimum_net_credit",
                "paystack_profile": "deposit",
            },
        }
    )
    try:
        record_admin_paystack_credit(
            admin_id=admin_id,
            amount=net,
            profile="deposit",
            reference=reference,
            transaction_id=txn_res.inserted_id,
            meta={"source": "user_wallet_deposit", "payment_provider": "moolre", "moolre": verify_raw},
        )
    except Exception:
        pass
    return {"ok": True}


def finalize_admin_wallet_deposit_payment(intent: Dict[str, Any]) -> Dict[str, Any]:
    user_oid = _safe_oid(intent.get("user_id"))
    admin_id = _safe_oid(intent.get("admin_id")) or user_oid
    reference = intent.get("reference")
    if not user_oid or not admin_id or not reference:
        return {"ok": False, "message": "Invalid admin wallet intent."}
    if transactions_col.find_one({"reference": reference, "status": "success"}):
        return {"ok": True, "duplicate": True}
    now = _now()
    net = _r2(intent.get("net_amount_ghs"))
    bal_doc = balances_col.find_one({"user_id": user_oid}, {"amount": 1}) or {}
    before = _r2(bal_doc.get("amount"))
    balances_col.update_one(
        {"user_id": user_oid},
        {
            "$inc": {"amount": net},
            "$set": {"updated_at": now, "admin_id": admin_id},
            "$setOnInsert": {"created_at": now, "currency": "GHS"},
        },
        upsert=True,
    )
    log_res = balance_logs_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "action": "deposit",
            "delta": net,
            "amount_before": before,
            "amount_after": _r2(before + net),
            "currency": "GHS",
            "note": "Admin self deposit via Moolre",
            "actor_id": user_oid,
            "actor_name": "Moolre Payment",
            "created_at": now,
        }
    )
    verify_raw = intent.get("raw_verify_response") or {}
    transactions_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "amount": net,
            "reference": reference,
            "status": "success",
            "type": "deposit",
            "source": "admin_self_wallet",
            "gateway": "Moolre",
            "currency": "GHS",
            "raw": verify_raw,
            "verified_at": now,
            "created_at": now,
            "balance_log_id": log_res.inserted_id,
            "payment_provider": "moolre",
            "payment_reference": reference,
            "payment_gateway": "Moolre",
            "payment_status": "success",
            "payment_verified_at": now,
            "payment_raw": verify_raw,
            "meta": {
                "payment_provider": "moolre",
                "moolre": verify_raw,
                "paid_gross_ghs": intent.get("expected_amount_ghs"),
                "net_credit_ghs": net,
                "fee_ghs": intent.get("fee_ghs"),
                "fee_rate": MOOLRE_FEE_RATE,
                "wallet_owner_user_id": str(user_oid),
                "source": "admin_self_wallet_deposit_fee_0p5_minimum_net_credit",
                "paystack_profile": "deposit",
            },
        }
    )
    return {"ok": True}


def finalize_admin_subscription_payment(intent: Dict[str, Any]) -> Dict[str, Any]:
    from maintenance import get_admin_doc, record_maintenance_payment

    admin_oid = _safe_oid(intent.get("user_id"))
    reference = intent.get("reference")
    admin_doc = get_admin_doc(admin_oid)
    if not admin_doc or not reference:
        return {"ok": False, "message": "Invalid subscription intent."}
    if maintenance_payments_col.find_one({"reference": reference, "status": "success"}):
        return {"ok": True, "duplicate": True}
    if transactions_col.find_one({"reference": reference, "status": "success"}):
        return {"ok": True, "duplicate": True}
    record_maintenance_payment(
        admin_doc,
        reference,
        _r2(intent.get("expected_amount_ghs")),
        intent.get("raw_verify_response") or {},
        provider="moolre",
    )
    return {"ok": True}


def finalize_store_checkout_payment(intent: Dict[str, Any]) -> Dict[str, Any]:
    from routes.store_page import _store_checkout_handler

    metadata = intent.get("metadata") or {}
    body = {
        "cart": metadata.get("cart") or [],
        "method": "moolre",
        "payment": {
            "provider": "moolre",
            "reference": intent.get("reference"),
            "moolre_reference": intent.get("moolre_reference"),
            "expected_amount_ghs": intent.get("expected_amount_ghs"),
            "net_amount_ghs": intent.get("net_amount_ghs"),
            "fee_ghs": intent.get("fee_ghs"),
            "raw": intent.get("raw_verify_response") or {},
            "payer": metadata.get("payer") or {},
        },
        # Legacy compatibility: old store code still names this paystack.
        "paystack": {"reference": intent.get("reference") or ""},
    }
    response = _store_checkout_handler(intent.get("store_slug") or "", body)
    try:
        flask_response, status_code = response
        data = flask_response.get_json(silent=True) or {}
    except Exception:
        data, status_code = {}, 500
    if status_code and int(status_code) >= 400:
        return {"ok": False, "message": data.get("message") or "Store checkout failed.", "data": data}
    return {"ok": True, "data": data, "redirect_url": data.get("redirect_url") or (url_for("stores.store_invoice_view", order_id=data.get("order_id")) if data.get("order_id") else "")}


def _dispatch_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    if intent.get("processed_at"):
        return {"ok": True, "duplicate": True}
    flow = intent.get("flow")
    handlers = {
        "store_checkout": finalize_store_checkout_payment,
        "agent_deposit": finalize_agent_deposit_payment,
        "admin_wallet_deposit": finalize_admin_wallet_deposit_payment,
        "admin_subscription": finalize_admin_subscription_payment,
    }
    handler = handlers.get(flow)
    if not handler:
        return {"ok": False, "message": "Unknown payment flow."}
    result = handler(intent)
    if result.get("ok"):
        update = {"processed_at": _now(), "processing_lock": False, "updated_at": _now()}
        if result.get("redirect_url"):
            update["result_redirect_url"] = result.get("redirect_url")
        if result.get("data"):
            update["result"] = _json_safe(result.get("data"))
        payment_intents_col.update_one({"_id": intent["_id"]}, {"$set": update})
    else:
        payment_intents_col.update_one(
            {"_id": intent["_id"]},
            {"$set": {"processing_lock": False, "updated_at": _now(), "last_error": result.get("message") or "Processing failed"}},
        )
    return result


def _verify_and_dispatch(reference: str, raw_callback: Any = None) -> tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
    intent, state = _verified_intent(reference, raw_callback=raw_callback)
    if not intent:
        return None, state, {"ok": False}
    if state == "already_processed":
        return intent, state, {"ok": True, "duplicate": True}
    if state != "verified":
        return intent, state, {"ok": False}
    result = _dispatch_intent(intent)
    return payment_intents_col.find_one({"_id": intent["_id"]}) or intent, "processed" if result.get("ok") else "process_failed", result


@moolre_payments_bp.route("/payments/moolre/callback", methods=["POST"])
def moolre_callback():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict(flat=True) or request.values.to_dict(flat=True)
    normalized = normalize_moolre_callback(payload or {})
    reference = normalized.get("reference")
    if not reference:
        return jsonify({"success": True, "message": "No reference supplied."}), 200
    payment_intents_col.update_one(
        {"provider": "moolre", "reference": reference},
        {"$set": {"raw_callback": payload, "updated_at": _now()}},
    )
    _verify_and_dispatch(reference, raw_callback=payload)
    return jsonify({"success": True}), 200


@moolre_payments_bp.route("/payments/moolre/redirect", methods=["GET"])
def moolre_redirect():
    reference = (request.args.get("reference") or request.args.get("externalref") or "").strip()
    if not reference:
        return redirect(url_for("index.landing"))
    intent, state, _result = _verify_and_dispatch(reference)
    intent = intent or payment_intents_col.find_one({"provider": "moolre", "reference": reference}) or {}
    flow = intent.get("flow")
    if intent.get("result_redirect_url"):
        return redirect(intent["result_redirect_url"])
    if flow == "store_checkout" and (intent.get("result") or {}).get("order_id"):
        return redirect(url_for("stores.store_invoice_view", order_id=intent["result"]["order_id"]))
    if flow == "agent_deposit":
        return redirect(url_for("customer_dashboard.customer_dashboard"))
    if flow == "admin_wallet_deposit":
        return redirect(url_for("deposit.admin_wallet_page"))
    if flow == "admin_subscription":
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))
    return jsonify({"success": bool(intent), "state": state, "reference": reference}), 200


@moolre_payments_bp.route("/payments/moolre/status/<reference>", methods=["GET"])
def moolre_status(reference: str):
    ref = (reference or "").strip()
    intent = payment_intents_col.find_one({"provider": "moolre", "reference": ref})
    if not intent:
        return jsonify({"success": False, "message": "Payment intent not found."}), 404
    return jsonify(
        {
            "success": True,
            "reference": intent.get("reference"),
            "intent_id": intent.get("intent_id"),
            "flow": intent.get("flow"),
            "status": intent.get("status"),
            "processed": bool(intent.get("processed_at")),
            "redirect_url": intent.get("result_redirect_url") or "",
            "result": _json_safe(intent.get("result") or {}),
        }
    ), 200
