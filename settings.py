from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from db import db
from datetime import datetime, timedelta
from typing import Optional
from tenant import current_admin_id_from_session, is_admin_role
from maintenance import get_maintenance_status, get_payment_history, get_maintenance_paystack_public_key
from paystack_keys import get_paystack_keys_doc
from admin_paystack_ledger import (
    clear_admin_paystack_payout_settings,
    get_admin_paystack_payout_settings,
    save_admin_paystack_payout_settings,
)
from sms_sender import DEFAULT_SITE_SENDER_NAME, get_sms_settings, normalize_ghana_sms_phone

settings_bp = Blueprint("settings", __name__)
settings_col = db["settings"]
transactions_col = db["transactions"]
users_col = db["users"]
_MOMO_NETWORKS = {"MTN", "TELECEL", "AIRTELTIGO"}


def _paystack_doc():
    return get_paystack_keys_doc()


def _get_momo_settings():
    return settings_col.find_one({"key": "momo_number"}) or {}


def _save_sms_settings(arkesel_api_key: str, site_sender_name: str, order_sms_recipient: str):
    now = datetime.utcnow()
    doc = get_sms_settings()
    update_doc = {
        "key": "sms_settings",
        "arkesel_api_key": arkesel_api_key,
        "site_sender_name": site_sender_name,
        "order_sms_recipient": order_sms_recipient,
        "updated_at": now,
    }
    if doc:
        settings_col.update_one({"_id": doc["_id"]}, {"$set": update_doc})
    else:
        update_doc["created_at"] = now
        settings_col.insert_one(update_doc)


def _save_momo_settings(number: str, name: str):
    now = datetime.utcnow()
    doc = _get_momo_settings()
    update_doc = {
        "key": "momo_number",
        "momo_number": number,
        "momo_name": name,
        "updated_at": now,
    }
    if doc:
        settings_col.update_one({"_id": doc["_id"]}, {"$set": update_doc})
    else:
        update_doc["created_at"] = now
        settings_col.insert_one(update_doc)


def _sum_paystack(profile: str, since: Optional[datetime] = None, admin_id=None) -> float:
    base = {
        "gateway": {"$regex": "paystack", "$options": "i"},
        "status": "success",
    }
    profile_q = {"meta.paystack_profile": profile}
    if profile == "store":
        profile_q = {
            "$or": [
                {"meta.paystack_profile": "store"},
                {"source": {"$in": ["paystack_inline", "store_checkout_paystack"]}},
                {"meta.store_checkout": True},
            ]
        }
    elif profile == "deposit":
        profile_q = {
            "$or": [
                {"meta.paystack_profile": "deposit"},
                {"type": "deposit"},
            ]
        }
    q = {**base, **profile_q}
    if admin_id:
        q["admin_id"] = admin_id
    if since:
        q["created_at"] = {"$gte": since}
    pipeline = [
        {"$match": q},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]
    try:
        rows = list(transactions_col.aggregate(pipeline))
        return float(rows[0].get("total", 0) or 0) if rows else 0.0
    except Exception:
        return 0.0


@settings_bp.route("/admin/settings", methods=["GET", "POST"])
def manage_api():
    if not is_admin_role(session.get("role")):
        return redirect(url_for("login.login"))

    role = (session.get("role") or "").strip().lower()
    is_main_admin = role == "main_admin"
    admin_id = current_admin_id_from_session(session) if not is_main_admin else None

    paystack_doc = _paystack_doc()

    sms_doc = get_sms_settings()

    active_tab = (request.args.get("tab") or ("paystack" if is_main_admin else "payments")).strip().lower()
    allowed_tabs = {"paystack", "billing", "momo", "sms"} if is_main_admin else {"billing", "payments"}
    if active_tab not in allowed_tabs:
        active_tab = "paystack" if is_main_admin else "payments"

    if request.method == "POST":
        form_type = (request.form.get("form_type") or "paystack").strip().lower()
        if form_type == "agent_manual_deposit_settings":
            if is_main_admin:
                flash("Only admin accounts can update agent manual deposit details.", "danger")
                return redirect(url_for("settings.manage_api", tab="billing"))

            user_oid = current_admin_id_from_session(session)
            if not user_oid:
                flash("Admin account not found.", "danger")
                return redirect(url_for("settings.manage_api", tab="payments"))

            if request.form.get("clear_agent_manual_deposit_settings"):
                users_col.update_one(
                    {"_id": user_oid},
                    {"$unset": {
                        "agent_manual_deposit_name": "",
                        "agent_manual_deposit_number": "",
                        "agent_manual_deposit_network": "",
                        "agent_manual_deposit_updated_at": "",
                    }},
                )
                flash("Agent manual deposit details cleared.", "success")
                return redirect(url_for("settings.manage_api", tab="payments"))

            momo_name = (request.form.get("agent_manual_deposit_name") or "").strip()
            momo_number = (request.form.get("agent_manual_deposit_number") or "").strip()
            momo_network = (request.form.get("agent_manual_deposit_network") or "").strip().upper()
            if not momo_name or not momo_number or momo_network not in _MOMO_NETWORKS:
                flash("MoMo name, number, and network are required for agent manual deposit details.", "danger")
                return redirect(url_for("settings.manage_api", tab="payments"))

            users_col.update_one(
                {"_id": user_oid},
                {"$set": {
                    "agent_manual_deposit_name": momo_name,
                    "agent_manual_deposit_number": momo_number,
                    "agent_manual_deposit_network": momo_network,
                    "agent_manual_deposit_updated_at": datetime.utcnow(),
                }},
            )
            flash("Agent manual deposit details saved.", "success")
            return redirect(url_for("settings.manage_api", tab="payments"))

        if form_type == "paystack_payout_settings":
            if is_main_admin:
                flash("Only admin accounts can update payout settings here.", "danger")
                return redirect(url_for("settings.manage_api", tab="billing"))

            user_oid = current_admin_id_from_session(session)
            if not user_oid:
                flash("Admin account not found.", "danger")
                return redirect(url_for("settings.manage_api", tab="payments"))

            if request.form.get("clear_payout_settings"):
                clear_admin_paystack_payout_settings(user_oid)
                flash("Paystack payout details cleared.", "success")
                return redirect(url_for("settings.manage_api", tab="payments"))

            recipient_name = (request.form.get("payout_recipient_name") or "").strip()
            msisdn = (request.form.get("payout_msisdn") or "").strip()
            network = (request.form.get("payout_network") or "").strip().upper()
            if not recipient_name or not msisdn or network not in _MOMO_NETWORKS:
                flash("Recipient name, MoMo number, and network are required.", "danger")
                return redirect(url_for("settings.manage_api", tab="payments"))

            save_admin_paystack_payout_settings(user_oid, recipient_name, msisdn, network)
            flash("Paystack payout details saved.", "success")
            return redirect(url_for("settings.manage_api", tab="payments"))

        if form_type == "momo":
            if role != "main_admin":
                flash("Only main admin can update MoMo number.", "danger")
                return redirect(url_for("settings.manage_api", tab="billing"))
            momo_number = (request.form.get("momo_number") or "").strip()
            momo_name = (request.form.get("momo_name") or "").strip()
            if not momo_number or not momo_name:
                flash("MoMo number and name are required.", "warning")
                return redirect(url_for("settings.manage_api", tab="momo"))
            _save_momo_settings(momo_number, momo_name)
            flash("MoMo details updated.", "success")
            return redirect(url_for("settings.manage_api", tab="momo"))

        if form_type == "sms":
            if role != "main_admin":
                flash("Only main admin can update SMS settings.", "danger")
                return redirect(url_for("settings.manage_api", tab="billing"))

            arkesel_api_key = (request.form.get("arkesel_api_key") or "").strip()
            site_sender_name = (request.form.get("site_sender_name") or "").strip()
            if not arkesel_api_key:
                flash("Arkasel API key is required.", "warning")
                return redirect(url_for("settings.manage_api", tab="sms"))
            if not site_sender_name:
                site_sender_name = DEFAULT_SITE_SENDER_NAME

            order_sms_recipient_raw = (request.form.get("order_sms_recipient") or "").strip()
            order_sms_recipient = order_sms_recipient_raw
            if order_sms_recipient_raw:
                normalized_order_sms = normalize_ghana_sms_phone(order_sms_recipient_raw)
                if not normalized_order_sms:
                    flash("Order SMS number must be a valid Ghana phone number, e.g. 0530393625.", "warning")
                    return redirect(url_for("settings.manage_api", tab="sms"))
                order_sms_recipient = "0" + normalized_order_sms[3:]

            _save_sms_settings(arkesel_api_key, site_sender_name, order_sms_recipient)
            flash("SMS settings updated.", "success")
            return redirect(url_for("settings.manage_api", tab="sms"))

        if role != "main_admin":
            flash("Only main admin can update Paystack keys.", "danger")
            return redirect(url_for("settings.manage_api", tab="billing"))

        store_public = (request.form.get("store_public_key") or "").strip()
        store_secret = (request.form.get("store_secret_key") or "").strip()
        deposit_public = (request.form.get("deposit_public_key") or "").strip()
        deposit_secret = (request.form.get("deposit_secret_key") or "").strip()

        missing = []
        if not store_public:
            missing.append("Store Public Key")
        if not store_secret:
            missing.append("Store Secret Key")
        if not deposit_public:
            missing.append("Deposit Public Key")
        if not deposit_secret:
            missing.append("Deposit Secret Key")
        if missing:
            flash("Missing required keys: " + ", ".join(missing), "danger")
            return redirect(url_for("settings.manage_api", tab="paystack"))

        now = datetime.utcnow()
        update_doc = {
            "key": "paystack_keys",
            "store_public_key": store_public,
            "store_secret_key": store_secret,
            "deposit_public_key": deposit_public,
            "deposit_secret_key": deposit_secret,
            "updated_at": now,
        }
        if paystack_doc:
            changed_store = (
                store_public != paystack_doc.get("store_public_key")
                or store_secret != paystack_doc.get("store_secret_key")
            )
            changed_deposit = (
                deposit_public != paystack_doc.get("deposit_public_key")
                or deposit_secret != paystack_doc.get("deposit_secret_key")
            )
            if changed_store:
                update_doc["store_updated_at"] = now
            if changed_deposit:
                update_doc["deposit_updated_at"] = now
            settings_col.update_one({"_id": paystack_doc["_id"]}, {"$set": update_doc})
        else:
            update_doc["created_at"] = now
            update_doc["store_updated_at"] = now
            update_doc["deposit_updated_at"] = now
            settings_col.insert_one(update_doc)

        flash("Paystack keys updated successfully.", "success")
        return redirect(url_for("settings.manage_api", tab="paystack"))

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=6)

    metrics_admin_id = None if is_main_admin else admin_id
    store_metrics = {
        "today": _sum_paystack("store", since=today_start, admin_id=metrics_admin_id),
        "week": _sum_paystack("store", since=week_start, admin_id=metrics_admin_id),
        "total": _sum_paystack("store", admin_id=metrics_admin_id),
    }
    deposit_metrics = {
        "today": _sum_paystack("deposit", since=today_start, admin_id=metrics_admin_id),
        "week": _sum_paystack("deposit", since=week_start, admin_id=metrics_admin_id),
        "total": _sum_paystack("deposit", admin_id=metrics_admin_id),
    }

    user_doc = None
    try:
        from bson import ObjectId
        uid = session.get("user_id")
        if uid:
            user_doc = users_col.find_one({"_id": ObjectId(uid)}) or {}
    except Exception:
        user_doc = {}

    payout_settings = {}
    if not is_main_admin:
        payout_settings = get_admin_paystack_payout_settings(current_admin_id_from_session(session))

    maintenance_status = get_maintenance_status(user_doc or {})
    maintenance_history = []
    try:
        if user_doc and user_doc.get("_id"):
            maintenance_history = get_payment_history(user_doc.get("_id"), limit=25)
    except Exception:
        maintenance_history = []

    momo_doc = _get_momo_settings()

    return render_template(
        "settings.html",
        store_public_key=paystack_doc.get("store_public_key", ""),
        store_secret_key=paystack_doc.get("store_secret_key", ""),
        deposit_public_key=paystack_doc.get("deposit_public_key", ""),
        deposit_secret_key=paystack_doc.get("deposit_secret_key", ""),
        store_updated_at=paystack_doc.get("store_updated_at"),
        deposit_updated_at=paystack_doc.get("deposit_updated_at"),
        store_metrics=store_metrics,
        deposit_metrics=deposit_metrics,
        admin_scope_label="Global Keys" if is_main_admin else "Billing & Maintenance",
        active_tab=active_tab,
        maintenance_status=maintenance_status,
        maintenance_history=maintenance_history,
        paystack_pk=get_maintenance_paystack_public_key(),
        momo_number=momo_doc.get("momo_number", ""),
        momo_name=momo_doc.get("momo_name", ""),
        arkesel_api_key=sms_doc.get("arkesel_api_key", ""),
        site_sender_name=sms_doc.get("site_sender_name", DEFAULT_SITE_SENDER_NAME),
        order_sms_recipient=sms_doc.get("order_sms_recipient", "0530393625"),
        sms_updated_at=sms_doc.get("updated_at"),
        is_main_admin=is_main_admin,
        user=user_doc or {},
        payout_recipient_name=payout_settings.get("recipient_name", ""),
        payout_msisdn=payout_settings.get("msisdn", ""),
        payout_network=payout_settings.get("network", ""),
        payout_settings_updated_at=payout_settings.get("updated_at"),
        payout_settings_set=bool((payout_settings.get("recipient_name") or "").strip() or (payout_settings.get("msisdn") or "").strip()),
        agent_manual_deposit_name=(user_doc or {}).get("agent_manual_deposit_name", ""),
        agent_manual_deposit_number=(user_doc or {}).get("agent_manual_deposit_number", ""),
        agent_manual_deposit_network=(user_doc or {}).get("agent_manual_deposit_network", ""),
        agent_manual_deposit_updated_at=(user_doc or {}).get("agent_manual_deposit_updated_at"),
        agent_manual_deposit_set=bool(((user_doc or {}).get("agent_manual_deposit_name") or "").strip() and ((user_doc or {}).get("agent_manual_deposit_number") or "").strip() and ((user_doc or {}).get("agent_manual_deposit_network") or "").strip()),
    )
