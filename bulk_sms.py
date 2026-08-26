from __future__ import annotations

import math
import re
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from db import db
from admin_paystack_ledger import evaluate_admin_wallet_low_balance
from sms_sender import send_bulk_sms
from social_boosting_pricing import normalize_admin_level
from tenant import is_admin_role, resolve_admin_id_for_user_id


bulk_sms_bp = Blueprint("bulk_sms", __name__)

BULK_SMS_SERVICE_ID = "69e36c82a8e6c7a322926fc8"
SMS_DISCLAIMER_TEXT = (
    "Do not use sender names that impersonate banks, mobile money services, telecom brands, "
    "or other trusted organizations (e.g., MTN Mobile Money). Misleading sender IDs may be "
    "blocked and your account may be suspended. SMS messages may not be delivered, and funds "
    "are non-refundable."
)

services_col = db["services"]
users_col = db["users"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]
transactions_col = db["transactions"]
bulk_sms_deliveries_col = db["bulk_sms_deliveries"]
bulk_sms_delivery_logs_col = db["bulk_sms_delivery_logs"]

_SENDER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,18}[A-Za-z0-9])?$")
_TRUSTED_NAMES_RE = re.compile(
    r"\b("
    r"mtn|mobile\s*money|momo|telecel|vodafone|airteltigo|airtel|tigo|bank|"
    r"gcb|ecobank|absa|stanbic|fidelity|calbank|gtbank|access\s*bank|"
    r"uba|zenith|republic\s*bank|standard\s*chartered"
    r")\b",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.utcnow()


def _to_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, dict):
            for key in ("$numberDouble", "$numberDecimal", "$numberInt", "$numberLong"):
                if key in value:
                    return float(value[key])
        return float(value)
    except Exception:
        return None


def _stage_key(stage_label: Optional[str]) -> str:
    label = (stage_label or "").strip().lower().replace("-", " ").replace("_", " ")
    if label in {"elite", "elite agent"}:
        return "elite_agent"
    if label in {"premium", "premium agent"}:
        return "premium"
    return "normal_agent"


def _display_name(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        return "Unknown user"
    parts = [user.get("first_name"), user.get("last_name")]
    full = " ".join(str(p).strip() for p in parts if p).strip()
    return full or user.get("full_name") or user.get("name") or user.get("username") or user.get("email") or "Unknown user"


def _role_allowed_for_order() -> bool:
    return (session.get("role") or "").strip().lower() in {"customer", "agent"}


def _admin_role_allowed() -> bool:
    return (session.get("role") or "").strip().lower() in {"admin", "main_admin"}


def _customer_role_allowed() -> bool:
    return (session.get("role") or "").strip().lower() in {"customer", "agent"}


def find_bulk_sms_service_for_admin(admin_id: Optional[ObjectId]) -> Optional[Dict[str, Any]]:
    base_oid = _to_oid(BULK_SMS_SERVICE_ID)
    if admin_id:
        service = services_col.find_one(
            {
                "admin_id": admin_id,
                "$or": [
                    {"base_service_id": base_oid},
                    {"_id": base_oid},
                    {"name": re.compile(r"^Bulk SMS$", re.IGNORECASE)},
                ],
            }
        )
        if service:
            return service
        return None
    if base_oid:
        return services_col.find_one({"_id": base_oid})
    return None


def sms_price_for_user(service: Optional[Dict[str, Any]], stage_label: Optional[str]) -> Optional[float]:
    if not service:
        return None
    stage_prices = service.get("sms_agent_stage_prices")
    if isinstance(stage_prices, dict):
        key = _stage_key(stage_label)
        price = _to_float(stage_prices.get(key))
        if price is not None:
            return round(price, 4)
    if service.get("admin_id"):
        return None
    for key in ("sms_price_per_number", "price_per_number", "amount"):
        price = _to_float(service.get(key))
        if price is not None:
            return round(price, 4)
    admin_prices = service.get("sms_admin_stage_prices")
    if isinstance(admin_prices, dict):
        for key in ("admin", "super_admin", "super_professional"):
            price = _to_float(admin_prices.get(key))
            if price is not None:
                return round(price, 4)
    return None

def sms_main_base_price(service: Optional[Dict[str, Any]]) -> float:
    if not service:
        return 0.0
    price = _to_float(service.get("sms_base_price_per_number"))
    if price is None:
        price = _to_float(service.get("sms_provider_base_price_per_number"))
    return round(float(price or 0.0), 4)

def sms_admin_price_for_service(service: Optional[Dict[str, Any]], admin_level: Optional[str]) -> Optional[float]:
    if not service:
        return None
    level = normalize_admin_level(admin_level)
    prices = service.get("sms_admin_stage_prices")
    if isinstance(prices, dict):
        price = _to_float(prices.get(level))
        if price is None and level != "admin":
            price = _to_float(prices.get("admin"))
        if price is not None:
            return round(price, 4)
    price = _to_float(service.get("sms_price_per_number"))
    return round(price, 4) if price is not None else None


def bulk_sms_context_for_customer(user_oid: ObjectId) -> Dict[str, Any]:
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    user_doc = users_col.find_one({"_id": user_oid}, {"stage_label": 1}) or {}
    service = find_bulk_sms_service_for_admin(admin_id)
    available = bool(
        service
        and (service.get("agent_visible", True) is not False)
        and (service.get("status") or "OPEN").upper() == "OPEN"
        and (service.get("availability") or "AVAILABLE").upper() == "AVAILABLE"
    )
    price = sms_price_for_user(service, user_doc.get("stage_label")) if available else None
    disabled_reason = ""
    if not service:
        disabled_reason = "Bulk SMS is not configured yet."
    elif not available:
        disabled_reason = "Bulk SMS is currently unavailable."
    elif price is None:
        disabled_reason = "Bulk SMS pricing is not configured yet."
    return {
        "available": bool(available and price is not None),
        "price_per_number": price,
        "service_id": str(service.get("_id")) if service and service.get("_id") else "",
        "disabled_reason": disabled_reason,
        "disclaimer": SMS_DISCLAIMER_TEXT,
    }


def _normalize_recipient(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    original = str(raw or "").strip()
    if not original:
        return None, "Recipient number is required."
    compact = re.sub(r"[\s().-]+", "", original)
    if compact.startswith("+"):
        compact = compact[1:]
    if not compact.isdigit():
        return None, f"{original} contains invalid characters."
    if len(compact) == 10 and compact.startswith("0"):
        compact = "233" + compact[1:]
    elif len(compact) == 9:
        compact = "233" + compact
    elif len(compact) == 12 and compact.startswith("233"):
        pass
    else:
        return None, f"{original} is not a valid Ghana recipient number."
    return compact, None


def _clean_recipients(values: Any) -> Tuple[List[Dict[str, str]], Optional[str]]:
    if isinstance(values, str):
        values = re.findall(
            r"\+?233[\s().-]*\d{2}[\s().-]*\d{3}[\s().-]*\d{4}|0[\s().-]*\d{2}[\s().-]*\d{3}[\s().-]*\d{4}|\b\d{2}[\s().-]*\d{3}[\s().-]*\d{4}\b",
            values,
        )
    if not isinstance(values, list):
        return [], "Recipients must be sent as a list."
    cleaned: List[Dict[str, str]] = []
    seen = set()
    for raw in values:
        normalized, error = _normalize_recipient(raw)
        if error:
            return [], error
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append({"number": normalized, "original": str(raw or "").strip()})
    if not cleaned:
        return [], "Add at least one recipient number."
    if len(cleaned) > 1000:
        return [], "You can submit up to 1000 numbers at once."
    return cleaned, None


def _validate_sender_name(sender_name: Any) -> Tuple[Optional[str], Optional[str]]:
    sender = re.sub(r"\s+", " ", str(sender_name or "").strip())
    if not sender:
        return None, "Sender name is required."
    if len(sender) < 2 or len(sender) > 20 or not _SENDER_RE.match(sender):
        return None, "Sender name must be 2 to 20 characters and use letters, numbers, spaces, dots, underscores, or hyphens."
    if _TRUSTED_NAMES_RE.search(sender):
        return None, "This sender name looks like a trusted organization. Choose a different sender name."
    return sender, None


def validate_sms_message_body(message_body: Any) -> Tuple[Optional[str], Optional[str]]:
    lines = [str(line).rstrip() for line in str(message_body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    message = "\n".join(lines).strip()
    if not message:
        return None, "Message is required."
    if len(message) > 1000:
        return None, "Message must be 1000 characters or fewer."
    return message, None


def _message_preview(message_body: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(message_body or "").strip())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _delivery_recipient_numbers(delivery_doc: Dict[str, Any]) -> List[str]:
    numbers: List[str] = []
    for item in delivery_doc.get("recipients") or []:
        if isinstance(item, dict):
            number = str(item.get("number") or "").strip()
        else:
            number = str(item or "").strip()
        if number:
            numbers.append(number)
    return numbers


def dispatch_bulk_sms_delivery(delivery_doc: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    sender_name = str(delivery_doc.get("sender_name") or "").strip()
    message_body = str(delivery_doc.get("message_body") or "").strip()
    recipients = _delivery_recipient_numbers(delivery_doc)
    send_result = send_bulk_sms(recipients, message_body, sender_id=sender_name or "Zico")

    delivered = bool(send_result.get("success"))
    final_status = "delivered" if delivered else "failed"
    provider_status = str(send_result.get("provider_status") or send_result.get("status") or "").strip()
    provider_message = str(send_result.get("provider_message") or "").strip()
    response_json = send_result.get("response_json")
    response_text = str(send_result.get("response_text") or "").strip()
    request_payload = send_result.get("request_payload") or {}

    log_doc = {
        "delivery_id": delivery_doc.get("_id"),
        "reference": delivery_doc.get("reference") or "",
        "admin_id": delivery_doc.get("admin_id"),
        "user_id": delivery_doc.get("user_id"),
        "source": delivery_doc.get("source") or "bulk_sms",
        "provider": send_result.get("provider") or "arkesel_v2",
        "status": final_status,
        "sender_name": sender_name,
        "message_preview": _message_preview(message_body),
        "recipient_count": len(recipients),
        "provider_http_status": send_result.get("http_status"),
        "provider_status": provider_status,
        "provider_message": provider_message,
        "provider_message_id": send_result.get("provider_message_id") or "",
        "request_payload": request_payload,
        "response_json": response_json,
        "response_text": response_text,
        "error": send_result.get("error") or "",
        "created_at": now,
    }
    log_insert = bulk_sms_delivery_logs_col.insert_one(log_doc)

    delivery_id = delivery_doc.get("_id")
    if delivery_id:
        history_entry = {
            "status": final_status,
            "provider_status": provider_status,
            "provider_message": provider_message,
            "created_at": now,
        }
        set_doc = {
            "status": final_status,
            "delivery_status": final_status,
            "provider": send_result.get("provider") or "arkesel_v2",
            "provider_http_status": send_result.get("http_status"),
            "provider_status": provider_status,
            "provider_message": provider_message,
            "provider_message_id": send_result.get("provider_message_id") or "",
            "provider_request_payload": request_payload,
            "provider_response_json": response_json,
            "provider_response_text": response_text,
            "last_log_id": log_insert.inserted_id,
            "updated_at": now,
        }
        if delivered:
            set_doc["sent_at"] = now
            set_doc["delivered_at"] = now
        else:
            set_doc["failed_at"] = now
        bulk_sms_deliveries_col.update_one(
            {"_id": delivery_id},
            {
                "$set": set_doc,
                "$push": {"status_history": history_entry},
                "$inc": {"log_count": 1},
            },
        )

    send_result["delivery_status"] = final_status
    send_result["logged_at"] = now
    send_result["log_id"] = log_insert.inserted_id
    return send_result


@bulk_sms_bp.route("/api/bulk-sms/pricing")
def bulk_sms_pricing():
    if not _role_allowed_for_order():
        return jsonify(success=False, error="Login required."), 401
    user_oid = _to_oid(session.get("user_id"))
    if not user_oid:
        return jsonify(success=False, error="Login required."), 401
    ctx = bulk_sms_context_for_customer(user_oid)
    return jsonify(success=True, bulk_sms=ctx)


@bulk_sms_bp.route("/api/bulk-sms/orders", methods=["POST"])
def create_bulk_sms_order():
    if not _role_allowed_for_order():
        return jsonify(success=False, error="Login required."), 401
    user_oid = _to_oid(session.get("user_id"))
    if not user_oid:
        return jsonify(success=False, error="Login required."), 401

    payload = request.get_json(silent=True) or {}
    if payload.get("disclaimer_accepted") is not True:
        return jsonify(success=False, error="You must accept the sender-name disclaimer first."), 400

    sender_name, sender_error = _validate_sender_name(payload.get("sender_name"))
    if sender_error:
        return jsonify(success=False, error=sender_error), 400

    message_body, message_error = validate_sms_message_body(payload.get("message") or payload.get("message_body"))
    if message_error:
        return jsonify(success=False, error=message_error), 400

    recipients, recipient_error = _clean_recipients(payload.get("recipients"))
    if recipient_error:
        return jsonify(success=False, error=recipient_error), 400

    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    user_doc = users_col.find_one(
        {"_id": user_oid},
        {"first_name": 1, "last_name": 1, "full_name": 1, "name": 1, "username": 1, "email": 1, "phone": 1, "stage_label": 1},
    ) or {}
    service = find_bulk_sms_service_for_admin(admin_id)
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}
    ctx = bulk_sms_context_for_customer(user_oid)
    if not ctx.get("available"):
        return jsonify(success=False, error=ctx.get("disabled_reason") or "Bulk SMS is unavailable."), 400

    price_per_number = sms_price_for_user(service, user_doc.get("stage_label"))
    if price_per_number is None:
        return jsonify(success=False, error="Bulk SMS pricing is not configured yet."), 400
    admin_price_per_number = sms_admin_price_for_service(service, (admin_doc or {}).get("admin_level"))
    if admin_price_per_number is None:
        admin_price_per_number = price_per_number
    main_base_price_per_number = sms_main_base_price(service)

    recipient_count = len(recipients)
    total_amount = round(float(price_per_number) * recipient_count, 2)
    admin_base_total = round(float(admin_price_per_number) * recipient_count, 2)
    main_base_total = round(float(main_base_price_per_number) * recipient_count, 2)
    main_admin_profit_amount = max(0.0, round(admin_base_total - main_base_total, 2))
    admin_profit_amount = max(0.0, round(total_amount - admin_base_total, 2))
    if total_amount <= 0:
        return jsonify(success=False, error="Bulk SMS pricing is invalid."), 400

    now = _now()
    balance_before_doc = balances_col.find_one({"user_id": user_oid}) or {}
    amount_before = float(balance_before_doc.get("amount", 0) or 0)
    charge = balances_col.update_one(
        {"user_id": user_oid, "amount": {"$gte": total_amount}},
        {"$inc": {"amount": -total_amount}, "$set": {"updated_at": now}, "$setOnInsert": {"admin_id": admin_id}},
        upsert=False,
    )
    if charge.matched_count == 0:
        return jsonify(success=False, error="Insufficient funds."), 400

    balance_doc = balances_col.find_one({"user_id": user_oid}) or {}
    new_balance = float(balance_doc.get("amount", 0) or 0)
    if is_admin_role(session.get("role")):
        try:
            evaluate_admin_wallet_low_balance(user_oid, send_alert=True, run_auto_credit=True)
        except Exception:
            pass
    reference = "BSMS-" + secrets.token_hex(5).upper()
    order_doc = {
        "reference": reference,
        "admin_id": admin_id,
        "user_id": user_oid,
        "user_role": session.get("role") or "customer",
        "customer_name": _display_name(user_doc),
        "customer_username": user_doc.get("username") or "",
        "customer_phone": user_doc.get("phone") or "",
        "service_id": service.get("_id") if service else None,
        "service_name": "Bulk SMS",
        "sender_name": sender_name,
        "message_body": message_body,
        "recipients": recipients,
        "recipient_count": recipient_count,
        "price_per_number": round(float(price_per_number), 4),
        "admin_price_per_number": round(float(admin_price_per_number), 4),
        "main_base_price_per_number": round(float(main_base_price_per_number), 4),
        "total_amount": total_amount,
        "selling_amount": total_amount,
        "amount": total_amount,
        "admin_base_amount": admin_base_total,
        "main_base_amount": main_base_total,
        "base_amount": admin_base_total,
        "main_admin_profit_amount": main_admin_profit_amount,
        "admin_profit_amount": admin_profit_amount,
        "main_admin_profit_total": main_admin_profit_amount,
        "admin_profit_total": admin_profit_amount,
        "store_profit_total": 0.0,
        "profit_amount_total": round(main_admin_profit_amount + admin_profit_amount, 2),
        "profit_amount": admin_profit_amount,
        "currency": balance_doc.get("currency") or "GHS",
        "status": "pending",
        "delivery_status": "pending",
        "disclaimer_accepted": True,
        "created_at": now,
        "updated_at": now,
    }
    inserted = bulk_sms_deliveries_col.insert_one(order_doc)
    order_doc["_id"] = inserted.inserted_id

    send_result = dispatch_bulk_sms_delivery(order_doc)
    delivery_status = send_result.get("delivery_status") or "pending"
    provider_message = str(send_result.get("provider_message") or "").strip()

    balance_logs_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "action": "withdraw",
            "delta": -total_amount,
            "amount_before": amount_before,
            "amount_after": new_balance,
            "currency": balance_doc.get("currency") or "GHS",
            "note": f"Bulk SMS delivery {reference}",
            "actor_id": user_oid,
            "actor_name": _display_name(user_doc),
            "created_at": now,
        }
    )
    transactions_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "amount": total_amount,
            "reference": reference,
            "status": "success",
            "type": "purchase",
            "gateway": "Wallet",
            "currency": balance_doc.get("currency") or "GHS",
            "created_at": now,
            "verified_at": now,
            "meta": {
                "source": "bulk_sms",
                "delivery_id": str(inserted.inserted_id),
                "recipient_count": recipient_count,
                "price_per_number": round(float(price_per_number), 4),
                "admin_price_per_number": round(float(admin_price_per_number), 4),
                "main_base_price_per_number": round(float(main_base_price_per_number), 4),
                "main_admin_profit_amount": main_admin_profit_amount,
                "admin_profit_amount": admin_profit_amount,
                "profit_amount_total": round(main_admin_profit_amount + admin_profit_amount, 2),
                "main_admin_profit_total": main_admin_profit_amount,
                "admin_profit_total": admin_profit_amount,
                "store_profit_total": 0.0,
            },
        }
    )

    return jsonify(
        success=True,
        message=(
            "Bulk SMS sent successfully."
            if delivery_status == "delivered"
            else (provider_message or "Bulk SMS was recorded, but provider delivery failed.")
        ),
        delivery_id=str(inserted.inserted_id),
        reference=reference,
        balance=new_balance,
        total_amount=total_amount,
        delivery_status=delivery_status,
        provider_status=send_result.get("provider_status") or "",
        provider_message=provider_message,
    )


def _delivery_query() -> Dict[str, Any]:
    role = (session.get("role") or "").strip().lower()
    query: Dict[str, Any] = {}
    if role == "admin":
        admin_oid = _to_oid(session.get("user_id"))
        if admin_oid:
            query["admin_id"] = admin_oid
    status = (request.args.get("status") or "").strip().lower()
    if status:
        query["status"] = status
    q = (request.args.get("q") or "").strip()
    if q:
        rx = re.compile(re.escape(q), re.IGNORECASE)
        query["$or"] = [
            {"reference": rx},
            {"sender_name": rx},
            {"customer_name": rx},
            {"customer_username": rx},
            {"message_body": rx},
            {"recipients.number": rx},
            {"recipients.original": rx},
        ]
    return query


def _format_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%d %b %Y, %I:%M %p")
    return ""


@bulk_sms_bp.route("/admin/bulk-sms-deliveries")
def admin_bulk_sms_deliveries():
    if not _admin_role_allowed():
        return redirect(url_for("login.login"))

    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = int(request.args.get("per_page", 20) or 20)
    per_page = min(max(per_page, 10), 100)
    query = _delivery_query()
    total = bulk_sms_deliveries_col.count_documents(query)
    pages = max(1, math.ceil(total / per_page))
    if page > pages:
        page = pages
    skip = (page - 1) * per_page

    deliveries = list(
        bulk_sms_deliveries_col.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
    )

    admin_ids = list({d.get("admin_id") for d in deliveries if isinstance(d.get("admin_id"), ObjectId)})
    admins = {
        a["_id"]: _display_name(a)
        for a in users_col.find({"_id": {"$in": admin_ids}}, {"first_name": 1, "last_name": 1, "full_name": 1, "name": 1, "username": 1, "email": 1})
    } if admin_ids else {}

    for delivery in deliveries:
        delivery["_id_str"] = str(delivery.get("_id"))
        delivery["created_at_fmt"] = _format_dt(delivery.get("created_at"))
        delivery["admin_name"] = admins.get(delivery.get("admin_id"), "Main admin" if session.get("role") == "main_admin" else "Admin")
        delivery["message_preview"] = _message_preview(delivery.get("message_body"))
        delivery["provider_summary"] = delivery.get("provider_message") or delivery.get("provider_status") or ""
        delivery["recipients_preview"] = ", ".join((r.get("number") or "") for r in (delivery.get("recipients") or [])[:3])
        if int(delivery.get("recipient_count") or 0) > 3:
            delivery["recipients_preview"] += f" +{int(delivery.get('recipient_count') or 0) - 3} more"

    preserved = {}
    for key in ("q", "status", "per_page"):
        val = (request.args.get(key) or "").strip()
        if val:
            preserved[key] = val
    preserved_query = urlencode(preserved)

    return render_template(
        "admin_bulk_sms_deliveries.html",
        deliveries=deliveries,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        q=request.args.get("q", ""),
        status_filter=request.args.get("status", ""),
        preserved_query=preserved_query,
        is_main_admin=(session.get("role") == "main_admin"),
    )


@bulk_sms_bp.route("/customer/bulk-sms-deliveries")
def customer_bulk_sms_deliveries():
    if not _customer_role_allowed():
        return redirect(url_for("login.login"))

    user_oid = _to_oid(session.get("user_id"))
    if not user_oid:
        return redirect(url_for("login.login"))

    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = int(request.args.get("per_page", 10) or 10)
    per_page = min(max(per_page, 10), 50)
    query: Dict[str, Any] = {"user_id": user_oid}

    status = (request.args.get("status") or "").strip().lower()
    if status:
        query["status"] = status

    q = (request.args.get("q") or "").strip()
    if q:
        rx = re.compile(re.escape(q), re.IGNORECASE)
        query["$or"] = [
            {"reference": rx},
            {"sender_name": rx},
            {"message_body": rx},
            {"recipients.number": rx},
            {"recipients.original": rx},
        ]

    total = bulk_sms_deliveries_col.count_documents(query)
    pages = max(1, math.ceil(total / per_page))
    if page > pages:
        page = pages
    skip = (page - 1) * per_page

    deliveries = list(
        bulk_sms_deliveries_col.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
    )
    for delivery in deliveries:
        delivery["_id_str"] = str(delivery.get("_id"))
        delivery["created_at_fmt"] = _format_dt(delivery.get("created_at"))
        delivery["message_preview"] = _message_preview(delivery.get("message_body"))
        delivery["provider_summary"] = delivery.get("provider_message") or delivery.get("provider_status") or ""
        delivery["recipients_preview"] = ", ".join((r.get("number") or "") for r in (delivery.get("recipients") or [])[:3])
        if int(delivery.get("recipient_count") or 0) > 3:
            delivery["recipients_preview"] += f" +{int(delivery.get('recipient_count') or 0) - 3} more"

    preserved = {}
    for key in ("q", "status", "per_page"):
        val = (request.args.get(key) or "").strip()
        if val:
            preserved[key] = val

    return render_template(
        "customer_bulk_sms_deliveries.html",
        deliveries=deliveries,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        q=request.args.get("q", ""),
        status_filter=request.args.get("status", ""),
        preserved_query=urlencode(preserved),
    )
