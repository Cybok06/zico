from __future__ import annotations

from typing import Any, Optional
import re

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from db import db
from tenant import current_admin_id_from_session
from admin_paystack_ledger import (
    MIN_PAYOUT_REQUEST_GHS,
    PAYOUT_WITHDRAW_FEE_GHS,
    admin_paystack_balances_col,
    admin_paystack_payout_requests_col,
    create_admin_paystack_payout_request,
    get_admin_paystack_balance,
    process_admin_paystack_payout_request,
)


admin_paystack_payouts_bp = Blueprint("admin_paystack_payouts", __name__)
users_col = db["users"]


def _role() -> str:
    return (session.get("role") or "").strip().lower()


def _to_oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _admin_display_name(admin_doc: dict) -> str:
    name = f"{admin_doc.get('first_name', '')} {admin_doc.get('last_name', '')}".strip()
    return name or admin_doc.get("username") or admin_doc.get("email") or "Admin"


def _admin_search_or(search_q: str):
    safe_q = re.escape(search_q)
    regex = {"$regex": safe_q, "$options": "i"}
    return [
        {"first_name": regex},
        {"last_name": regex},
        {"username": regex},
        {"email": regex},
        {"phone": regex},
    ]


@admin_paystack_payouts_bp.route("/admin/paystack-payouts", methods=["GET"])
def admin_paystack_payouts_page():
    role = _role()
    if role not in {"admin", "main_admin"}:
        return redirect(url_for("login.login"))

    is_main_admin = role == "main_admin"
    admin_oid = current_admin_id_from_session(session)
    if not admin_oid and session.get("user_id"):
        admin_oid = _to_oid(session.get("user_id"))
    search_q = (request.args.get("q") or "").strip()

    my_balance = get_admin_paystack_balance(admin_oid)
    my_requests = []
    if admin_oid:
        my_requests = list(
            admin_paystack_payout_requests_col.find({"admin_id": admin_oid}).sort("created_at", -1).limit(50)
        )

    requests = []
    admin_map = {}
    admin_payout_cards = []
    global_totals = {"total_inflow": 0.0, "available_balance": 0.0, "pending_balance": 0.0, "withdrawn_balance": 0.0}
    if is_main_admin:
        sub_admin_query = {"role": "admin"}
        if search_q:
            sub_admin_query["$or"] = _admin_search_or(search_q)
        sub_admins = list(
            users_col.find(
                sub_admin_query,
                {"first_name": 1, "last_name": 1, "username": 1, "email": 1, "phone": 1, "status": 1},
            ).sort([("first_name", 1), ("username", 1)])
        )
        sub_admin_ids = [a["_id"] for a in sub_admins if isinstance(a.get("_id"), ObjectId)]
        admin_map = {a["_id"]: a for a in sub_admins}

        scoped_admin_query = {"admin_id": {"$in": sub_admin_ids}} if sub_admin_ids else {"admin_id": {"$in": []}}
        requests = list(admin_paystack_payout_requests_col.find(scoped_admin_query).sort("created_at", -1).limit(200))
        balance_docs = list(admin_paystack_balances_col.find(scoped_admin_query).sort("updated_at", -1).limit(500))
        admin_ids = [r.get("admin_id") for r in requests if r.get("admin_id")] + [b.get("admin_id") for b in balance_docs if b.get("admin_id")]
        admin_ids = [x for x in admin_ids if x]
        unresolved_admin_ids = [x for x in set(admin_ids) if x not in admin_map]
        if unresolved_admin_ids:
            for u in users_col.find({"_id": {"$in": unresolved_admin_ids}}, {"first_name": 1, "last_name": 1, "username": 1, "email": 1, "phone": 1, "status": 1}):
                admin_map[u["_id"]] = u
        for b in balance_docs:
            global_totals["total_inflow"] += float(b.get("total_inflow", 0) or 0)
            global_totals["available_balance"] += float(b.get("available_balance", 0) or 0)
            global_totals["pending_balance"] += float(b.get("pending_balance", 0) or 0)
            global_totals["withdrawn_balance"] += float(b.get("withdrawn_balance", 0) or 0)

        request_rows_by_admin = {}
        for row in requests:
            aid = row.get("admin_id")
            if not aid:
                continue
            request_rows_by_admin.setdefault(aid, []).append(row)

        balance_by_admin = {b.get("admin_id"): b for b in balance_docs if b.get("admin_id")}
        card_admin_ids = []
        for aid in sub_admin_ids + list(balance_by_admin.keys()) + list(request_rows_by_admin.keys()):
            if aid and aid not in card_admin_ids:
                card_admin_ids.append(aid)

        for aid in card_admin_ids:
            admin_doc = admin_map.get(aid) or {}
            bal = balance_by_admin.get(aid) or {}
            history = request_rows_by_admin.get(aid, [])[:5]
            admin_payout_cards.append({
                "admin_id": aid,
                "admin": admin_doc,
                "admin_name": _admin_display_name(admin_doc),
                "phone": admin_doc.get("phone") or "",
                "email": admin_doc.get("email") or "",
                "status": admin_doc.get("status") or "",
                "total_inflow": float(bal.get("total_inflow", 0) or 0),
                "available_balance": float(bal.get("available_balance", 0) or 0),
                "pending_balance": float(bal.get("pending_balance", 0) or 0),
                "withdrawn_balance": float(bal.get("withdrawn_balance", 0) or 0),
                "history": history,
                "history_count": len(request_rows_by_admin.get(aid, [])),
            })

        admin_payout_cards.sort(key=lambda row: row.get("available_balance", 0), reverse=True)

    for row in my_requests + requests:
        admin_doc = admin_map.get(row.get("admin_id")) or {}
        row["admin_doc"] = admin_doc

    pending_requests = [r for r in requests if (r.get("status") or "").strip().lower() == "pending"]
    settled_requests = [
        r for r in requests
        if (r.get("status") or "").strip().lower() in {"paid", "rejected"}
    ]

    return render_template(
        "admin_paystack_payouts.html",
        is_main_admin=is_main_admin,
        my_balance=my_balance,
        my_requests=my_requests,
        requests=requests,
        pending_requests=pending_requests,
        settled_requests=settled_requests,
        pending_request_count=len(pending_requests),
        admin_payout_cards=admin_payout_cards,
        admin_map=admin_map,
        global_totals=global_totals,
        payout_fee=PAYOUT_WITHDRAW_FEE_GHS,
        min_request=MIN_PAYOUT_REQUEST_GHS,
        search_q=search_q,
    )


@admin_paystack_payouts_bp.route("/admin/paystack-payouts/request", methods=["POST"])
def admin_paystack_payout_request():
    role = _role()
    if role != "admin":
        flash("Only sub-admin accounts can request Paystack payouts.", "danger")
        return redirect(url_for("admin_paystack_payouts.admin_paystack_payouts_page"))

    admin_oid = current_admin_id_from_session(session) or _to_oid(session.get("user_id"))
    amount = request.form.get("amount")
    method = request.form.get("method")
    note = request.form.get("note") or ""
    result = create_admin_paystack_payout_request(admin_oid, amount, method, note=note)
    flash(result.get("message") or ("Payout request submitted." if result.get("ok") else "Unable to submit payout request."), "success" if result.get("ok") else "danger")
    return redirect(url_for("admin_paystack_payouts.admin_paystack_payouts_page"))


@admin_paystack_payouts_bp.route("/admin/paystack-payouts/<request_id>/status", methods=["POST"])
def admin_paystack_payout_update_status(request_id: str):
    role = _role()
    if role != "main_admin":
        flash("Only main admin can update payout requests.", "danger")
        return redirect(url_for("admin_paystack_payouts.admin_paystack_payouts_page"))

    action = (request.form.get("action") or "").strip().lower()
    note = (request.form.get("note") or "").strip()
    result = process_admin_paystack_payout_request(request_id, action, actor_id=session.get("user_id"), note=note)
    flash(result.get("message") or ("Request updated." if result.get("ok") else "Unable to update request."), "success" if result.get("ok") else "danger")
    return redirect(url_for("admin_paystack_payouts.admin_paystack_payouts_page"))
