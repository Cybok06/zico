from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Tuple

from bson import ObjectId

from db import db

store_withdraw_requests_col = db["store_withdraw_requests"]
transactions_col = db["transactions"]
store_accounts_col = db["store_accounts"]
balances_col = db["balances"]
balance_logs_col = db["balance_logs"]

ALLOWED_WITHDRAW_STATUSES = {
    "requested",
    "pending",
    "processing",
    "paid",
    "success",
    "failed",
    "rejected",
    "canceled",
}


def _normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "cancelled":
        s = "canceled"
    return s


def _fmt_money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _request_scope(req_doc: Dict[str, Any]) -> Dict[str, Any]:
    admin_id = req_doc.get("admin_id")
    return {"admin_id": admin_id} if admin_id else {}


def _request_method(req_doc: Dict[str, Any], tx_doc: Dict[str, Any] | None) -> str:
    method = (req_doc.get("method") or "").strip().lower()
    if method:
        return method
    meta = (tx_doc or {}).get("meta") or {}
    return (meta.get("method") or meta.get("payout_method") or "momo").strip().lower()


def _request_store_slug(req_doc: Dict[str, Any], tx_doc: Dict[str, Any] | None) -> str:
    slug = str(req_doc.get("store_slug") or "").strip()
    if slug:
        return slug
    meta = (tx_doc or {}).get("meta") or {}
    return str(meta.get("store_slug") or "").strip()


def _request_amounts(req_doc: Dict[str, Any], tx_doc: Dict[str, Any] | None) -> tuple[float, float]:
    amount = _fmt_money(req_doc.get("amount"))
    meta = (tx_doc or {}).get("meta") or {}
    debit_amount = _fmt_money(req_doc.get("debit_amount"))
    if debit_amount <= 0:
        debit_amount = _fmt_money(meta.get("debit_amount"))
    if debit_amount <= 0:
        debit_amount = amount
    return amount, debit_amount


def _credit_owner_wallet(owner_id: Any, slug: str, amount: float, reference: str) -> float:
    now = datetime.utcnow()
    before_doc = balances_col.find_one({"user_id": owner_id}) or {}
    before = _fmt_money(before_doc.get("amount"))
    after = _fmt_money(before + amount)
    balances_col.update_one(
        {"user_id": owner_id},
        {
            "$inc": {"amount": amount},
            "$set": {"updated_at": now},
            "$setOnInsert": {"created_at": now, "currency": "GHS"},
        },
        upsert=True,
    )
    balance_logs_col.insert_one(
        {
            "user_id": owner_id,
            "action": "deposit",
            "delta": amount,
            "amount_before": before,
            "amount_after": after,
            "currency": "GHS",
            "note": f"Store profit wallet withdrawal {reference}",
            "actor_id": owner_id,
            "actor_name": "Store Wallet Payout",
            "created_at": now,
            "meta": {"store_slug": slug, "source": "store_wallet_withdrawal"},
        }
    )
    return after


def _apply_profit_debit(
    req_doc: Dict[str, Any],
    tx_doc: Dict[str, Any] | None,
    actor_id: Any,
    method: str,
    debit_amount: float,
    now: datetime,
) -> tuple[bool, str | None]:
    store_slug = _request_store_slug(req_doc, tx_doc)
    if not store_slug or debit_amount <= 0:
        return True, None

    updated = store_accounts_col.update_one(
        {
            "store_slug": store_slug,
            "total_profit_balance": {"$gte": debit_amount},
        },
        {
            "$inc": {"total_profit_balance": -debit_amount},
            "$set": {"updated_at": now},
            "$push": {
                "history": {
                    "event": "withdrawal_settled",
                    "reference": req_doc.get("reference"),
                    "method": method,
                    "amount": _fmt_money(req_doc.get("amount")),
                    "debit_amount": debit_amount,
                    "status": "paid" if method == "momo" else "success",
                    "created_at": now,
                    "processed_by": actor_id or "admin",
                }
            },
        },
    )
    if updated.matched_count == 0:
        return False, "Insufficient store profit balance to complete this withdrawal."
    return True, None


def _apply_profit_refund(
    req_doc: Dict[str, Any],
    tx_doc: Dict[str, Any] | None,
    actor_id: Any,
    method: str,
    debit_amount: float,
    status_norm: str,
    now: datetime,
) -> None:
    store_slug = _request_store_slug(req_doc, tx_doc)
    if not store_slug or debit_amount <= 0:
        return

    store_accounts_col.update_one(
        {"store_slug": store_slug},
        {
            "$inc": {"total_profit_balance": debit_amount},
            "$set": {"updated_at": now},
            "$push": {
                "history": {
                    "event": "withdrawal_refund",
                    "reference": req_doc.get("reference"),
                    "method": method,
                    "amount": debit_amount,
                    "status": status_norm,
                    "created_at": now,
                    "processed_by": actor_id or "admin",
                }
            },
        },
    )


def update_withdraw_request_status(
    req_id: str,
    new_status: str,
    actor_id: Any = None,
    note: str | None = None,
) -> Tuple[bool, Dict[str, Any], int]:
    if not req_id:
        return False, {"message": "Invalid request id"}, 400

    try:
        oid = ObjectId(req_id)
    except Exception:
        return False, {"message": "Invalid request id"}, 400

    status_norm = _normalize_status(new_status)
    if status_norm not in ALLOWED_WITHDRAW_STATUSES:
        return False, {"message": "Invalid status"}, 400

    req_doc = store_withdraw_requests_col.find_one({"_id": oid})
    if not req_doc:
        return False, {"message": "Request not found"}, 404

    tx_doc = transactions_col.find_one(
        {
            "type": "store_withdrawal",
            "reference": req_doc.get("reference"),
            "meta.store_slug": req_doc.get("store_slug"),
            **_request_scope(req_doc),
        }
    )

    old_status = _normalize_status(req_doc.get("status") or "pending")
    note_in = (note or "").strip()
    if old_status == status_norm and note_in == (req_doc.get("note") or ""):
        return True, {"message": "No changes", "no_change": True}, 200
    if old_status in {"paid", "success"} and status_norm not in {"paid", "success"}:
        return False, {"message": "Cannot downgrade a completed withdrawal"}, 400

    now = datetime.utcnow()
    method = _request_method(req_doc, tx_doc)
    amount, debit_amount = _request_amounts(req_doc, tx_doc)
    current_meta = (tx_doc or {}).get("meta") or {}
    profit_debited_flag = req_doc.get("profit_debited")
    if profit_debited_flag is None:
        profit_debited_flag = current_meta.get("profit_debited")
    wallet_credited_flag = bool(req_doc.get("wallet_credited") or current_meta.get("wallet_credited"))
    profit_refunded_flag = bool(req_doc.get("profit_refunded") or req_doc.get("refunded") or current_meta.get("refunded"))

    request_status = status_norm
    update_fields: Dict[str, Any] = {
        "status": request_status,
        "updated_at": now,
        "updated_by": actor_id or "admin",
    }
    if note_in:
        update_fields["note"] = note_in
    if status_norm in {"paid", "success"}:
        if profit_debited_flag is False:
            ok, err = _apply_profit_debit(req_doc, tx_doc, actor_id, method, debit_amount, now)
            if not ok:
                return False, {"message": err or "Unable to debit store profit balance"}, 400
            profit_debited_flag = True
        elif profit_debited_flag is None:
            profit_debited_flag = True

        if method == "wallet" and not wallet_credited_flag:
            owner_id = req_doc.get("owner_id")
            if not owner_id:
                return False, {"message": "Missing withdrawal owner"}, 400
            _credit_owner_wallet(owner_id, _request_store_slug(req_doc, tx_doc), amount, str(req_doc.get("reference") or ""))
            wallet_credited_flag = True

        update_fields["profit_debited"] = True
        if method == "wallet":
            update_fields["wallet_credited"] = True
        request_status = "paid"
        update_fields["status"] = request_status
        update_fields["paid_at"] = now
        update_fields["paid_by"] = actor_id or "admin"
    elif status_norm in {"rejected", "failed", "canceled"}:
        if profit_debited_flag is not False and not profit_refunded_flag:
            _apply_profit_refund(req_doc, tx_doc, actor_id, method, debit_amount, status_norm, now)
            profit_refunded_flag = True
            update_fields["profit_refunded"] = True
            update_fields["refunded_amount"] = debit_amount
            update_fields["refunded_at"] = now

    history_event = {
        "when": now,
        "actor_id": actor_id or "admin",
        "from_status": old_status,
        "to_status": request_status,
    }
    if note_in:
        history_event["note"] = note_in

    store_withdraw_requests_col.update_one(
        {"_id": oid},
        {"$set": update_fields, "$push": {"history": history_event}},
    )

    if tx_doc:
        tx_status = request_status
        if method == "wallet" and request_status == "paid":
            tx_status = "success"
        elif request_status == "processing":
            tx_status = "pending"

        tx_set: Dict[str, Any] = {
            "status": tx_status,
            "meta.processed_by": actor_id or "admin",
        }
        if note_in:
            tx_set["meta.admin_note"] = note_in
        if request_status in {"paid", "failed", "rejected", "canceled"} or tx_status == "success":
            tx_set["verified_at"] = now
        elif request_status in {"requested", "pending", "processing"}:
            tx_set["verified_at"] = None
        if request_status == "paid":
            tx_set["gateway"] = "Internal" if method == "wallet" else "MoMo"
            tx_set["meta.profit_debited"] = True
            if method == "wallet":
                tx_set["meta.wallet_credited"] = True
        elif status_norm in {"rejected", "failed", "canceled"} and profit_refunded_flag:
            tx_set["meta.refunded"] = True
        transactions_col.update_one({"_id": tx_doc["_id"]}, {"$set": tx_set})

    return True, {"message": f"Marked {request_status}", "status": request_status}, 200
