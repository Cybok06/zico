from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import requests
import json
import uuid

from db import db
from tenant import resolve_admin_id_for_user_id, current_admin_id_from_session, is_admin_role
from paystack_keys import get_paystack_key_pair
from admin_paystack_ledger import (
    evaluate_admin_wallet_low_balance,
    get_admin_paystack_balance,
    get_admin_wallet_auto_credit_settings,
    record_admin_paystack_credit,
    save_admin_wallet_auto_credit_settings,
)
from sms_sender import resolve_system_sender_id, send_sms, normalize_ghana_sms_phone

deposit_bp = Blueprint("deposit", __name__)
balances_col = db["balances"]
transactions_col = db["transactions"]
users_col = db["users"]
balance_logs_col = db["balance_logs"]
manual_topups_col = db["manual_wallet_topups"]
settings_col = db["settings"]

def _get_deposit_paystack_keys(admin_id=None):
    return get_paystack_key_pair("deposit", admin_id=admin_id)


def _get_momo_settings():
    return settings_col.find_one({"key": "momo_number"}) or {}


def _get_agent_manual_deposit_settings(admin_id=None):
    admin_oid = _to_oid(admin_id)
    if not admin_oid:
        return {}
    admin_doc = users_col.find_one(
        {"_id": admin_oid},
        {
            "agent_manual_deposit_name": 1,
            "agent_manual_deposit_number": 1,
            "agent_manual_deposit_network": 1,
            "agent_manual_deposit_updated_at": 1,
        },
    ) or {}
    return {
        "name": (admin_doc.get("agent_manual_deposit_name") or "").strip(),
        "number": (admin_doc.get("agent_manual_deposit_number") or "").strip(),
        "network": (admin_doc.get("agent_manual_deposit_network") or "").strip().upper(),
        "updated_at": admin_doc.get("agent_manual_deposit_updated_at"),
    }

DEPOSIT_FEE_RATE = 0.005
MIN_DEPOSIT_GHS = 10.0
ADMIN_WALLET_MIN_DEPOSIT_GHS = 50.0


def _r2(x: float) -> float:
    return round(float(x or 0), 2)


def _decimal_amount(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _round_half_up_pesewas(amount_ghs) -> int:
    return int(
        (_decimal_amount(amount_ghs) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _fee_inclusive_pesewas(net_amount_ghs: float, fee_rate: float = DEPOSIT_FEE_RATE) -> tuple[int, int, int]:
    net = _decimal_amount(net_amount_ghs)
    net_pes = _round_half_up_pesewas(net)
    fee_pes = int(
        (net * _decimal_amount(fee_rate) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    return net_pes, fee_pes, net_pes + fee_pes


def _strict_deposit_amounts(
    metadata: dict,
    paid_pesewas: int,
    min_amount_ghs: float = MIN_DEPOSIT_GHS,
) -> dict:
    try:
        meta_net = _r2(float((metadata or {}).get("net_amount_ghs")))
    except Exception:
        return {"ok": False, "message": "Payment metadata is missing the deposit amount."}

    try:
        meta_rate = float((metadata or {}).get("fee_rate"))
    except Exception:
        meta_rate = DEPOSIT_FEE_RATE

    if abs(meta_rate - DEPOSIT_FEE_RATE) > 0.000001:
        return {"ok": False, "message": "Invalid Paystack fee rate."}

    if meta_net < min_amount_ghs:
        return {"ok": False, "message": f"Minimum deposit is GHS {min_amount_ghs:.2f}."}

    net_pes, fee_pes, expected_pes = _fee_inclusive_pesewas(meta_net)
    paid_pes = int(paid_pesewas or 0)
    if paid_pes + 1 < expected_pes:
        return {
            "ok": False,
            "message": "Payment amount is below the required 0.5% fee-inclusive total.",
            "required": _r2(expected_pes / 100.0),
            "paid": _r2(paid_pes / 100.0),
        }

    gateway_overage_pes = max(0, paid_pes - expected_pes)
    return {
        "ok": True,
        "net_credit_ghs": _r2(net_pes / 100.0),
        "fee_ghs": _r2(fee_pes / 100.0),
        "gross_ghs": _r2(expected_pes / 100.0),
        "paid_gross_ghs": _r2(paid_pes / 100.0),
        "gateway_overage_ghs": _r2(gateway_overage_pes / 100.0),
        "fee_rate": DEPOSIT_FEE_RATE,
    }


def _to_oid(value):
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _actor_name() -> str:
    return (
        session.get("username")
        or session.get("email")
        or "admin"
    )


def _make_manual_reference() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"MAN-{ts}-{uuid.uuid4().hex[:6].upper()}"


def _notify_admin_of_agent_manual_deposit(admin_id: ObjectId, agent_doc: dict, amount: float, reference: str) -> None:
    try:
        admin_doc = users_col.find_one({"_id": admin_id}, {"phone": 1, "role": 1}) or {}
        msisdn = normalize_ghana_sms_phone(admin_doc.get("phone") or "")
        if not msisdn:
            return
        agent_name = (
            agent_doc.get("username")
            or f"{(agent_doc.get('first_name') or '').strip()} {(agent_doc.get('last_name') or '').strip()}".strip()
            or "Agent"
        )
        sender_id = resolve_system_sender_id(
            admin_id=admin_id,
            recipient_role=admin_doc.get("role") or "admin",
            recipient_user_id=admin_id,
        )
        send_sms(
            msisdn,
            f"{agent_name} submitted a manual wallet top up of GHS {amount:.2f}. Ref: {reference}. Please confirm in Wallet Manager.",
            sender_id=sender_id,
        )
    except Exception:
        pass


def _verify_paystack_reference(reference: str, admin_id=None):
    _, secret = _get_deposit_paystack_keys(admin_id=admin_id)
    if not secret:
        return {"status": False, "message": "Paystack not configured."}
    headers = {"Authorization": f"Bearer {secret}"}
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    resp = requests.get(url, headers=headers, timeout=20)
    result = resp.json()
    print("Paystack Verification Response:", json.dumps(result, indent=2))
    return result


@deposit_bp.route("/deposit")
def deposit_page():
    if session.get("role") not in {"customer", "agent"} or "user_id" not in session:
        return redirect(url_for("login.login"))

    email = session.get("email")
    if not email:
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        email = user.get("email", "") if user else ""

    admin_id = resolve_admin_id_for_user_id(users_col, session.get("user_id"))
    role = (session.get("role") or "").strip().lower()
    agent_manual_deposit = _get_agent_manual_deposit_settings(admin_id)
    return render_template(
        "deposit.html",
        user_id=session["user_id"],
        email=email,
        paystack_pk=_get_deposit_paystack_keys(admin_id=admin_id)[0],
        deposit_fee_rate=DEPOSIT_FEE_RATE,
        min_deposit=MIN_DEPOSIT_GHS,
        user_role=role,
        agent_manual_deposit=agent_manual_deposit,
        agent_manual_deposit_enabled=(
            role == "agent"
            and bool(agent_manual_deposit.get("name"))
            and bool(agent_manual_deposit.get("number"))
            and bool(agent_manual_deposit.get("network"))
        ),
    )


@deposit_bp.route("/deposit/manual-request", methods=["POST"])
def deposit_manual_request():
    role = (session.get("role") or "").strip().lower()
    if role not in {"agent", "customer"} or "user_id" not in session:
        return redirect(url_for("login.login"))

    user_oid = _to_oid(session.get("user_id"))
    admin_id = resolve_admin_id_for_user_id(users_col, session.get("user_id"))
    if not user_oid or not admin_id:
        flash("Unable to submit manual deposit request.", "danger")
        return redirect(url_for("deposit.deposit_page"))

    if role != "agent":
        flash("Manual deposit requests are currently available for agents only.", "warning")
        return redirect(url_for("deposit.deposit_page"))

    agent_manual_deposit = _get_agent_manual_deposit_settings(admin_id)
    if not (agent_manual_deposit.get("name") and agent_manual_deposit.get("number") and agent_manual_deposit.get("network")):
        flash("Your admin has not configured manual deposit details yet.", "warning")
        return redirect(url_for("deposit.deposit_page"))

    amount_raw = (request.form.get("amount") or "").strip()
    reference = (request.form.get("reference") or "").strip()
    payment_phone = (request.form.get("payment_phone") or "").strip()

    try:
        amount = _r2(float(amount_raw))
    except Exception:
        amount = 0.0

    if amount < MIN_DEPOSIT_GHS:
        flash(f"Minimum manual deposit is GHS {MIN_DEPOSIT_GHS:.2f}.", "warning")
        return redirect(url_for("deposit.deposit_page"))

    if not reference:
        reference = _make_manual_reference()

    if manual_topups_col.find_one({"reference": reference}):
        flash("Reference already used. Please try again.", "warning")
        return redirect(url_for("deposit.deposit_page"))

    user = users_col.find_one({"_id": user_oid}, {"phone": 1, "username": 1, "first_name": 1, "last_name": 1}) or {}
    now = datetime.utcnow()
    manual_topups_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "wallet_owner_user_id": user_oid,
            "amount": amount,
            "phone": payment_phone or (user.get("phone") or ""),
            "reference": reference,
            "status": "pending",
            "source": "agent_wallet_manual",
            "created_at": now,
            "updated_at": now,
            "requested_by": {
                "user_id": user_oid,
                "name": (
                    user.get("username")
                    or f"{(user.get('first_name') or '').strip()} {(user.get('last_name') or '').strip()}".strip()
                    or session.get("username")
                    or "agent"
                ),
            },
            "meta": {
                "receiver_name": agent_manual_deposit.get("name"),
                "receiver_number": agent_manual_deposit.get("number"),
                "receiver_network": agent_manual_deposit.get("network"),
            },
        }
    )
    _notify_admin_of_agent_manual_deposit(admin_id, user, amount, reference)

    flash("Manual deposit request submitted. Your admin can now confirm and credit your wallet.", "success")
    return redirect(url_for("deposit.deposit_page"))


@deposit_bp.route("/verify_transaction")
def verify_transaction():
    reference = request.args.get("reference", type=str)
    user_id = session.get("user_id")

    if not reference or not user_id:
        flash("âŒ Invalid deposit request", "danger")
        return redirect(url_for("customer_dashboard.customer_dashboard"))

    admin_id = resolve_admin_id_for_user_id(users_col, user_id)
    if not admin_id:
        flash("âŒ Your account is not mapped to an admin.", "danger")
        return redirect(url_for("customer_dashboard.customer_dashboard"))

    try:
        result = _verify_paystack_reference(reference, admin_id=admin_id)

        ok = result.get("status") and result.get("data", {}).get("status") == "success"
        if not ok:
            fail_msg = result.get("message") or result.get("data", {}).get("gateway_response") or "Verification failed."
            flash(f"âŒ Payment verification failed: {fail_msg}", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        data = result["data"]

        paid_pesewas = int(data.get("amount", 0) or 0)
        paid_gross_ghs = _r2(paid_pesewas / 100.0)
        currency = data.get("currency", "GHS")
        channel = data.get("channel", "")
        paid_ref = data.get("reference")
        metadata = data.get("metadata") or {}

        if paid_gross_ghs <= 0 or currency != "GHS":
            flash("âŒ Invalid payment amount/currency.", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        if transactions_col.find_one({"reference": paid_ref, "status": "success"}):
            flash("âœ… Deposit already verified earlier.", "success")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        fee_rate = float(metadata.get("fee_rate", DEPOSIT_FEE_RATE) or 0.0)

        meta_net = metadata.get("net_amount_ghs")
        try:
            net_credit_ghs = _r2(float(meta_net)) if meta_net is not None else None
        except Exception:
            net_credit_ghs = None

        if net_credit_ghs is None:
            net_credit_ghs = _r2(paid_gross_ghs / (1.0 + fee_rate))

        if net_credit_ghs < MIN_DEPOSIT_GHS:
            flash(f"âŒ Minimum deposit is GHS {MIN_DEPOSIT_GHS:.2f}.", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        if net_credit_ghs < 0:
            net_credit_ghs = 0.0
        if net_credit_ghs > paid_gross_ghs:
            net_credit_ghs = paid_gross_ghs

        fee_ghs = _r2(paid_gross_ghs - net_credit_ghs)
        strict_amounts = _strict_deposit_amounts(metadata, paid_pesewas)
        if not strict_amounts.get("ok"):
            flash(f"Invalid deposit payment: {strict_amounts.get('message')}", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))
        net_credit_ghs = strict_amounts["net_credit_ghs"]
        fee_ghs = strict_amounts["fee_ghs"]
        fee_rate = strict_amounts["fee_rate"]
        required_gross_ghs = strict_amounts["gross_ghs"]
        gateway_overage_ghs = strict_amounts["gateway_overage_ghs"]
        user_oid = ObjectId(user_id)
        wallet_owner_user_id = user_oid

        balances_col.update_one(
            {"user_id": wallet_owner_user_id},
            {
                "$inc": {"amount": net_credit_ghs},
                "$set": {"updated_at": datetime.utcnow(), "admin_id": admin_id},
                "$setOnInsert": {"created_at": datetime.utcnow(), "currency": "GHS"},
            },
            upsert=True,
        )

        txn_res = transactions_col.insert_one(
            {
                "user_id": user_oid,
                "admin_id": admin_id,
                "amount": net_credit_ghs,
                "reference": paid_ref,
                "status": "success",
                "type": "deposit",
                "gateway": "Paystack",
                "currency": currency,
                "channel": channel,
                "raw": data,
                "verified_at": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "meta": {
                    "paid_gross_ghs": paid_gross_ghs,
                    "required_gross_ghs": required_gross_ghs,
                    "net_credit_ghs": net_credit_ghs,
                    "fee_ghs": fee_ghs,
                    "fee_rate": fee_rate,
                    "gateway_overage_ghs": gateway_overage_ghs,
                    "wallet_owner_user_id": str(wallet_owner_user_id),
                    "source": "user_wallet_deposit_fee_0p5_minimum_net_credit",
                    "paystack_profile": "deposit",
                },
            }
        )
        try:
            record_admin_paystack_credit(
                admin_id=admin_id,
                amount=net_credit_ghs,
                profile="deposit",
                reference=paid_ref,
                transaction_id=txn_res.inserted_id,
                meta={
                    "source": "user_wallet_deposit",
                    "user_id": str(user_id),
                    "channel": channel,
                    "currency": currency,
                    "paid_gross_ghs": paid_gross_ghs,
                    "net_credit_ghs": net_credit_ghs,
                    "paystack_credit_ghs": net_credit_ghs,
                    "required_gross_ghs": required_gross_ghs,
                    "platform_fee_ghs": fee_ghs,
                    "gateway_overage_ghs": gateway_overage_ghs,
                },
            )
        except Exception:
            pass

        flash(f"Deposit successful! Credited your wallet with GHS {net_credit_ghs:.2f}.", "success")

    except Exception as e:
        print("Paystack Exception:", str(e))
        flash("âŒ Could not verify payment. Please try again.", "danger")

    return redirect(url_for("customer_dashboard.customer_dashboard"))


@deposit_bp.route("/admin/wallet")
def admin_wallet_page():
    role = (session.get("role") or "").strip().lower()
    if not is_admin_role(role) or "user_id" not in session:
        return redirect(url_for("login.login"))

    user_oid = _to_oid(session.get("user_id"))
    if not user_oid:
        return redirect(url_for("login.login"))

    email = (session.get("email") or "").strip()
    user = users_col.find_one({"_id": user_oid}, {"email": 1, "username": 1, "first_name": 1, "last_name": 1, "role": 1}) or {}
    if not email:
        email = (user.get("email") or "").strip()

    bal_doc = balances_col.find_one({"user_id": user_oid}, {"amount": 1, "currency": 1})
    balance = _r2((bal_doc or {}).get("amount", 0))
    currency = (bal_doc or {}).get("currency", "GHS")

    admin_id = current_admin_id_from_session(session) or user_oid
    wallet_status = evaluate_admin_wallet_low_balance(admin_id, send_alert=True, run_auto_credit=True)
    balance = _r2(wallet_status.get("balance", balance))
    try:
        print("[admin_wallet_page_debug]", {
            "role": role,
            "session_user_id": str(session.get("user_id") or ""),
            "session_admin_id": str(session.get("admin_id") or ""),
            "wallet_page_user_oid": str(user_oid),
            "resolved_admin_id": str(admin_id or ""),
            "balance_doc_id": str((bal_doc or {}).get("_id") or ""),
            "balance": balance,
        })
    except Exception:
        pass
    auto_credit_settings = wallet_status.get("settings") or get_admin_wallet_auto_credit_settings(admin_id)
    paystack_payout_balance = wallet_status.get("paystack_balance") or get_admin_paystack_balance(admin_id)
    momo_doc = _get_momo_settings()
    return render_template(
        "admin_wallet.html",
        user_id=str(user_oid),
        email=email,
        paystack_pk=_get_deposit_paystack_keys(admin_id=admin_id)[0],
        deposit_fee_rate=DEPOSIT_FEE_RATE,
        min_deposit=ADMIN_WALLET_MIN_DEPOSIT_GHS,
        balance=balance,
        currency=currency,
        wallet_low=bool(wallet_status.get("low")),
        wallet_low_limit=_r2(wallet_status.get("limit", 50)),
        auto_credit_settings=auto_credit_settings,
        auto_credit_result=wallet_status.get("auto_credit") or {},
        paystack_payout_balance=paystack_payout_balance,
        wallet_owner=user.get("username")
        or f"{(user.get('first_name') or '').strip()} {(user.get('last_name') or '').strip()}".strip()
        or "Admin",
        is_main_admin=role == "main_admin",
        momo_number=momo_doc.get("momo_number", ""),
        momo_name=momo_doc.get("momo_name", ""),
    )


@deposit_bp.route("/admin/wallet/auto-credit", methods=["POST"])
def admin_wallet_auto_credit():
    role = (session.get("role") or "").strip().lower()
    if not is_admin_role(role) or "user_id" not in session:
        return redirect(url_for("login.login"))

    admin_id = current_admin_id_from_session(session)
    if not admin_id:
        flash("Invalid admin wallet request.", "danger")
        return redirect(url_for("deposit.admin_wallet_page"))

    enabled = request.form.get("enabled")
    low_balance_limit = request.form.get("low_balance_limit")
    topup_amount = request.form.get("topup_amount")
    result = save_admin_wallet_auto_credit_settings(admin_id, enabled, low_balance_limit, topup_amount)
    flash(result.get("message", "Auto-credit settings saved."), "success" if result.get("ok") else "warning")
    if result.get("ok"):
        status = evaluate_admin_wallet_low_balance(admin_id, send_alert=True, run_auto_credit=True)
        auto_credit = status.get("auto_credit") or {}
        if auto_credit.get("ok"):
            flash(auto_credit.get("message", "Wallet auto-credit completed."), "success")
        elif bool(status.get("low")) and (auto_credit.get("message")):
            flash(auto_credit.get("message"), "info")
    return redirect(url_for("deposit.admin_wallet_page"))


@deposit_bp.route("/admin/wallet/manual-topup", methods=["POST"])
def admin_wallet_manual_topup():
    role = (session.get("role") or "").strip().lower()
    if not is_admin_role(role) or "user_id" not in session:
        return redirect(url_for("login.login"))

    user_oid = _to_oid(session.get("user_id"))
    admin_id = current_admin_id_from_session(session) or user_oid
    if not user_oid or not admin_id:
        flash("Invalid admin wallet request.", "danger")
        return redirect(url_for("deposit.admin_wallet_page"))

    amount_raw = (request.form.get("amount") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    reference = (request.form.get("reference") or "").strip()

    try:
        amount = _r2(float(amount_raw))
    except Exception:
        amount = 0.0

    if amount <= 0:
        flash("Enter a valid amount for manual top up.", "warning")
        return redirect(url_for("deposit.admin_wallet_page"))
    if amount < ADMIN_WALLET_MIN_DEPOSIT_GHS:
        flash(f"Minimum manual top up is GHS {ADMIN_WALLET_MIN_DEPOSIT_GHS:.2f}.", "warning")
        return redirect(url_for("deposit.admin_wallet_page"))
    if not phone:
        flash("Phone number is required for manual top up.", "warning")
        return redirect(url_for("deposit.admin_wallet_page"))

    if not reference:
        reference = _make_manual_reference()

    if manual_topups_col.find_one({"reference": reference}):
        flash("Reference already used. Please refresh and try again.", "warning")
        return redirect(url_for("deposit.admin_wallet_page"))

    now = datetime.utcnow()
    manual_topups_col.insert_one(
        {
            "user_id": user_oid,
            "admin_id": admin_id,
            "wallet_owner_user_id": user_oid,
            "amount": amount,
            "phone": phone,
            "reference": reference,
            "status": "pending",
            "source": "admin_wallet_manual",
            "created_at": now,
            "updated_at": now,
            "requested_by": {
                "user_id": user_oid,
                "name": _actor_name(),
            },
        }
    )

    flash("Manual top up submitted. Pending main admin approval.", "success")
    return redirect(url_for("deposit.admin_wallet_page"))


@deposit_bp.route("/admin/verify_wallet_deposit")
def verify_admin_wallet_deposit():
    role = (session.get("role") or "").strip().lower()
    user_id = session.get("user_id")
    reference = request.args.get("reference", type=str)

    if not is_admin_role(role) or not user_id:
        flash("Invalid admin wallet request.", "danger")
        return redirect(url_for("login.login"))

    user_oid = _to_oid(user_id)
    admin_id = current_admin_id_from_session(session) or user_oid
    if not reference or not user_oid or not admin_id:
        flash("Invalid admin wallet request.", "danger")
        return redirect(url_for("deposit.admin_wallet_page"))

    try:
        result = _verify_paystack_reference(reference, admin_id=admin_id)
        ok = result.get("status") and result.get("data", {}).get("status") == "success"
        if not ok:
            fail_msg = result.get("message") or result.get("data", {}).get("gateway_response") or "Verification failed."
            flash(f"Payment verification failed: {fail_msg}", "danger")
            return redirect(url_for("deposit.admin_wallet_page"))

        data = result["data"]
        paid_pesewas = int(data.get("amount", 0) or 0)
        paid_gross_ghs = _r2(paid_pesewas / 100.0)
        currency = data.get("currency", "GHS")
        channel = data.get("channel", "")
        paid_ref = data.get("reference")
        metadata = data.get("metadata") or {}

        if paid_gross_ghs <= 0 or currency != "GHS":
            flash("Invalid payment amount/currency.", "danger")
            return redirect(url_for("deposit.admin_wallet_page"))

        if transactions_col.find_one({"reference": paid_ref, "status": "success"}):
            flash("Deposit already verified earlier.", "success")
            return redirect(url_for("deposit.admin_wallet_page"))

        fee_rate = float(metadata.get("fee_rate", DEPOSIT_FEE_RATE) or 0.0)
        meta_net = metadata.get("net_amount_ghs")
        try:
            net_credit_ghs = _r2(float(meta_net)) if meta_net is not None else None
        except Exception:
            net_credit_ghs = None

        if net_credit_ghs is None:
            net_credit_ghs = _r2(paid_gross_ghs / (1.0 + fee_rate))

        if net_credit_ghs < ADMIN_WALLET_MIN_DEPOSIT_GHS:
            flash(f"Minimum deposit is GHS {ADMIN_WALLET_MIN_DEPOSIT_GHS:.2f}.", "danger")
            return redirect(url_for("deposit.admin_wallet_page"))

        if net_credit_ghs < 0:
            net_credit_ghs = 0.0
        if net_credit_ghs > paid_gross_ghs:
            net_credit_ghs = paid_gross_ghs

        fee_ghs = _r2(paid_gross_ghs - net_credit_ghs)
        strict_amounts = _strict_deposit_amounts(
            metadata,
            paid_pesewas,
            min_amount_ghs=ADMIN_WALLET_MIN_DEPOSIT_GHS,
        )
        if not strict_amounts.get("ok"):
            flash(f"Invalid deposit payment: {strict_amounts.get('message')}", "danger")
            return redirect(url_for("deposit.admin_wallet_page"))
        net_credit_ghs = strict_amounts["net_credit_ghs"]
        fee_ghs = strict_amounts["fee_ghs"]
        fee_rate = strict_amounts["fee_rate"]
        required_gross_ghs = strict_amounts["gross_ghs"]
        gateway_overage_ghs = strict_amounts["gateway_overage_ghs"]
        now = datetime.utcnow()
        current_bal_doc = balances_col.find_one({"user_id": user_oid}, {"amount": 1})
        old_amount = _r2((current_bal_doc or {}).get("amount", 0.0))

        balances_col.update_one(
            {"user_id": user_oid},
            {
                "$inc": {"amount": net_credit_ghs},
                "$set": {"updated_at": now, "admin_id": admin_id},
                "$setOnInsert": {"created_at": now, "currency": "GHS"},
            },
            upsert=True,
        )
        new_amount = _r2(old_amount + net_credit_ghs)

        log_res = balance_logs_col.insert_one(
            {
                "user_id": user_oid,
                "admin_id": admin_id,
                "action": "deposit",
                "delta": float(net_credit_ghs),
                "amount_before": float(old_amount),
                "amount_after": float(new_amount),
                "currency": "GHS",
                "note": "Admin self deposit via Paystack",
                "actor_id": user_oid,
                "actor_name": _actor_name(),
                "created_at": now,
            }
        )

        transactions_col.insert_one(
            {
                "user_id": user_oid,
                "admin_id": admin_id,
                "amount": net_credit_ghs,
                "reference": paid_ref,
                "status": "success",
                "type": "deposit",
                "source": "admin_self_wallet",
                "gateway": "Paystack",
                "currency": currency,
                "channel": channel,
                "raw": data,
                "verified_at": now,
                "created_at": now,
                "balance_log_id": log_res.inserted_id,
                "meta": {
                    "paid_gross_ghs": paid_gross_ghs,
                    "required_gross_ghs": required_gross_ghs,
                    "net_credit_ghs": net_credit_ghs,
                    "fee_ghs": fee_ghs,
                    "fee_rate": fee_rate,
                    "gateway_overage_ghs": gateway_overage_ghs,
                    "wallet_owner_user_id": str(user_oid),
                    "source": "admin_self_wallet_deposit_fee_0p5_minimum_net_credit",
                    "paystack_profile": "deposit",
                },
            }
        )

        flash(f"Deposit successful. Credited your wallet with GHS {net_credit_ghs:.2f}.", "success")
    except Exception as e:
        print("Paystack Exception (admin wallet):", str(e))
        flash("Could not verify payment. Please try again.", "danger")

    return redirect(url_for("deposit.admin_wallet_page"))
