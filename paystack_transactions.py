from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Dict, List, Optional

from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import db
from tenant import current_admin_id_from_session

paystack_transactions_bp = Blueprint("paystack_transactions", __name__)

transactions_col = db["transactions"]
orders_col = db["orders"]
users_col = db["users"]


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _to_oid(val: Any) -> Optional[ObjectId]:
    if isinstance(val, ObjectId):
        return val
    if not val:
        return None
    try:
        return ObjectId(str(val))
    except Exception:
        return None


def _parse_range(range_preset: str, start_date: str, end_date: str) -> tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    if range_preset == "today":
        return today, today + timedelta(days=1)

    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        except Exception:
            start_dt = None
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        except Exception:
            end_dt = None
    return start_dt, end_dt


def _profile_label(txn: Dict[str, Any]) -> str:
    meta = txn.get("meta") or {}
    profile = (meta.get("paystack_profile") or "").strip().lower()
    if not profile and (txn.get("type") == "deposit"):
        profile = "deposit"
    if profile == "subscription" or (txn.get("source") or "").strip().lower() == "admin_subscription":
        return "Subscription"
    if profile == "deposit":
        return "Deposit"
    return "Store"


def _source_label(txn: Dict[str, Any]) -> str:
    meta = txn.get("meta") or {}
    profile = (meta.get("paystack_profile") or "").strip().lower()
    source = (txn.get("source") or meta.get("source") or "").strip().lower()
    txn_type = (txn.get("type") or "").strip().lower()

    if source == "admin_subscription" or profile == "subscription" or txn_type == "maintenance_fee":
        return "admin_subscription"

    if source == "admin_self_wallet" or source.startswith("admin_self_wallet_"):
        return "admin_deposit"

    if profile == "deposit" or txn_type == "deposit":
        return "agent_deposit"

    if (
        profile == "store"
        or source in {"paystack_inline", "store_checkout_paystack"}
        or meta.get("store_checkout") is True
    ):
        return "agent_store"

    return source or (txn.get("gateway") or "-")


def _money(val: Any, default: float = 0.0) -> float:
    try:
        return round(float(val or 0.0), 2)
    except Exception:
        return default


def _person_text(doc: Dict[str, Any]) -> str:
    return " ".join(
        str(doc.get(k) or "")
        for k in ("first_name", "last_name", "username", "email", "phone")
    ).strip().lower()


def _matches_text(haystack: str, needle: str) -> bool:
    return not needle or needle.lower() in (haystack or "").lower()


def _user_search_query(search_q: str) -> Dict[str, Any]:
    safe_q = re.escape(search_q)
    regex = {"$regex": safe_q, "$options": "i"}
    return {
        "$or": [
            {"first_name": regex},
            {"last_name": regex},
            {"username": regex},
            {"email": regex},
            {"phone": regex},
        ]
    }


def _fee_free_amount(txn: Dict[str, Any]) -> float:
    meta = txn.get("meta") or {}
    if meta.get("store_checkout") is True or _profile_label(txn) == "Store":
        for key in ("expected_order_total_ghs", "paystack_credit_ghs"):
            if meta.get(key) not in (None, ""):
                return _money(meta.get(key))
        if txn.get("charged_amount") not in (None, ""):
            return _money(txn.get("charged_amount"))
        fee = _money(meta.get("paystack_fee_ghs") or txn.get("paystack_fee_amount"))
        if fee:
            return max(0.0, _money(txn.get("amount")) - fee)
    return _money(txn.get("amount"))


def _order_to_paystack_row(order: Dict[str, Any]) -> Dict[str, Any]:
    amount = _money(order.get("charged_amount") if order.get("charged_amount") is not None else order.get("total_amount"))
    return {
        "_id": order.get("_id"),
        "user_id": order.get("user_id") or order.get("store_owner_id"),
        "admin_id": order.get("admin_id"),
        "amount": amount,
        "display_amount": amount,
        "reference": order.get("paystack_reference"),
        "status": "success",
        "type": "store",
        "source": "paystack_inline",
        "gateway": "Paystack",
        "created_at": order.get("created_at"),
        "verified_at": order.get("created_at"),
        "display_date": order.get("created_at"),
        "profile_label": "Store",
        "source_label": "agent_store",
        "meta": {
            "store_checkout": True,
            "store_slug": order.get("store_slug"),
            "paystack_fee_ghs": order.get("paystack_fee_amount"),
            "paid_total_ghs": order.get("paystack_charged_amount"),
            "expected_order_total_ghs": amount,
        },
        "_from_order": True,
    }


@paystack_transactions_bp.route("/admin/paystack-transactions")
def paystack_transactions_page():
    if session.get("role") not in {"admin", "main_admin"}:
        return redirect(url_for("login.login"))

    is_main_admin = _is_main_admin()
    admin_oid = current_admin_id_from_session(session)
    if not is_main_admin and not admin_oid and session.get("user_id"):
        admin_oid = _to_oid(session.get("user_id"))

    profile = (request.args.get("profile") or "all").strip().lower()
    allowed_profiles = {"all", "store", "deposit", "subscription"} if is_main_admin else {"all", "store", "deposit"}
    if profile not in allowed_profiles:
        profile = "all"

    range_preset = (request.args.get("range") or "").strip().lower()
    if range_preset not in {"today", "custom", ""}:
        range_preset = ""

    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    start_dt, end_dt = _parse_range(range_preset, start_date, end_date)

    admin_filter = (request.args.get("admin_id") or "").strip()
    admin_filter_oid = _to_oid(admin_filter) if admin_filter else None
    search_q = (request.args.get("q") or "").strip()

    base = {
        "status": "success",
        "source": {"$nin": ["admin_paystack_payout", "store_order"]},
        "meta.paystack_payout": {"$ne": True},
        "$or": [
            {"gateway": {"$regex": "paystack", "$options": "i"}},
            {"source": {"$regex": "paystack", "$options": "i"}},
            {"meta.paystack_profile": {"$in": ["store", "deposit", "subscription"]}},
            {"source": "admin_subscription"},
        ],
    }

    if profile == "all":
        profile_q = {
            "$or": [
                {"meta.paystack_profile": {"$in": ["store", "deposit"] + (["subscription"] if is_main_admin else [])}},
                {"type": "deposit"},
                {"source": {"$in": ["paystack_inline", "store_checkout_paystack"]}},
                {"meta.store_checkout": True},
            ] + ([{"source": "admin_subscription"}, {"type": "maintenance_fee"}] if is_main_admin else [])
        }
    elif profile == "deposit":
        profile_q = {"$or": [{"meta.paystack_profile": "deposit"}, {"type": "deposit"}]}
    elif profile == "subscription":
        profile_q = {
            "$or": [
                {"meta.paystack_profile": "subscription"},
                {"source": "admin_subscription"},
                {"type": "maintenance_fee"},
            ]
        }
    else:
        profile_q = {
            "$or": [
                {"meta.paystack_profile": "store"},
                {"source": {"$in": ["paystack_inline", "store_checkout_paystack"]}},
                {"meta.store_checkout": True},
            ]
        }

    query_parts: List[Dict[str, Any]] = [base, profile_q]

    if is_main_admin:
        if admin_filter_oid:
            query_parts.append({"admin_id": admin_filter_oid})
    elif admin_oid:
        query_parts.append({"admin_id": admin_oid})

    matched_user_ids: List[ObjectId] = []
    matched_admin_ids: List[ObjectId] = []
    if search_q:
        user_match_q = _user_search_query(search_q)
        if is_main_admin:
            admin_match_q = {"$and": [{"role": "admin"}, user_match_q]}
            matched_admin_ids = [u["_id"] for u in users_col.find(admin_match_q, {"_id": 1}) if u.get("_id")]
        else:
            matched_admin_ids = []
        customer_match_q: Dict[str, Any] = user_match_q
        matched_user_ids = [u["_id"] for u in users_col.find(customer_match_q, {"_id": 1}) if u.get("_id")]
        search_parts = [{"user_id": {"$in": matched_user_ids}}]
        if matched_admin_ids:
            search_parts.append({"admin_id": {"$in": matched_admin_ids}})
        safe_q = re.escape(search_q)
        search_parts.extend([
            {"reference": {"$regex": safe_q, "$options": "i"}},
            {"source": {"$regex": safe_q, "$options": "i"}},
            {"meta.store_slug": {"$regex": safe_q, "$options": "i"}},
            {"meta.payer_phone": {"$regex": safe_q, "$options": "i"}},
            {"meta.payer_email": {"$regex": safe_q, "$options": "i"}},
        ])
        query_parts.append({"$or": search_parts})

    if start_dt or end_dt:
        date_filter: Dict[str, Any] = {}
        if start_dt:
            date_filter["$gte"] = start_dt
        if end_dt:
            date_filter["$lt"] = end_dt
        if date_filter:
            query_parts.append({"verified_at": date_filter})

    query = {"$and": query_parts}

    order_query_parts: List[Dict[str, Any]] = []
    if profile in {"all", "store"}:
        order_query_parts.append(
            {
                "paid_from": "paystack_inline",
                "paystack_reference": {"$exists": True, "$nin": [None, ""]},
            }
        )
        if is_main_admin:
            if admin_filter_oid:
                order_query_parts.append({"admin_id": admin_filter_oid})
        elif admin_oid:
            order_query_parts.append({"admin_id": admin_oid})
        if search_q:
            safe_q = re.escape(search_q)
            order_search_parts = [
                {"paystack_reference": {"$regex": safe_q, "$options": "i"}},
                {"store_slug": {"$regex": safe_q, "$options": "i"}},
                {"customer_phone": {"$regex": safe_q, "$options": "i"}},
                {"payer_phone": {"$regex": safe_q, "$options": "i"}},
            ]
            if matched_user_ids:
                order_search_parts.append({"user_id": {"$in": matched_user_ids}})
            if matched_admin_ids:
                order_search_parts.append({"admin_id": {"$in": matched_admin_ids}})
            order_query_parts.append({"$or": order_search_parts})
        if start_dt or end_dt:
            order_date_filter: Dict[str, Any] = {}
            if start_dt:
                order_date_filter["$gte"] = start_dt
            if end_dt:
                order_date_filter["$lt"] = end_dt
            if order_date_filter:
                order_query_parts.append({"created_at": order_date_filter})

    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)
    per_page = 25

    all_transactions = list(transactions_col.find(query).sort([("verified_at", -1), ("created_at", -1)]))
    seen_refs = {str(t.get("reference") or "").strip() for t in all_transactions if (t.get("reference") or "").strip()}

    if order_query_parts:
        order_query = {"$and": order_query_parts}
        for order in orders_col.find(order_query).sort("created_at", -1):
            ref = str(order.get("paystack_reference") or "").strip()
            if ref and ref in seen_refs:
                continue
            all_transactions.append(_order_to_paystack_row(order))
            if ref:
                seen_refs.add(ref)

    all_user_ids = [t.get("user_id") for t in all_transactions if t.get("user_id")]
    all_admin_ids = [t.get("admin_id") for t in all_transactions if t.get("admin_id")]
    all_user_map: Dict[ObjectId, Dict[str, Any]] = {}
    all_admin_map: Dict[ObjectId, Dict[str, Any]] = {}
    if all_user_ids:
        for u in users_col.find({"_id": {"$in": list(set(all_user_ids))}}):
            all_user_map[u["_id"]] = u
    if all_admin_ids:
        for a in users_col.find({"_id": {"$in": list(set(all_admin_ids))}}):
            all_admin_map[a["_id"]] = a

    filtered_transactions = []
    search_lc = search_q.lower()
    for txn in all_transactions:
        txn["display_amount"] = _fee_free_amount(txn)
        txn["display_date"] = txn.get("verified_at") or txn.get("created_at")
        txn["profile_label"] = txn.get("profile_label") or _profile_label(txn)
        txn["source_label"] = txn.get("source_label") or _source_label(txn)
        txn["user"] = all_user_map.get(txn.get("user_id"), {}) or {}
        txn["admin"] = all_admin_map.get(txn.get("admin_id"), {}) or {}
        if search_q:
            meta = txn.get("meta") or {}
            combined = " ".join(
                str(x or "")
                for x in (
                    txn.get("reference"),
                    txn.get("source"),
                    txn.get("source_label"),
                    meta.get("store_slug"),
                    meta.get("payer_phone"),
                    meta.get("payer_email"),
                    _person_text(txn["user"]),
                    _person_text(txn["admin"]),
                )
            ).lower()
            if search_lc not in combined:
                continue
        filtered_transactions.append(txn)

    all_transactions = filtered_transactions
    all_transactions.sort(key=lambda t: t.get("display_date") or datetime.min, reverse=True)
    total_count = len(all_transactions)
    total_amount = round(sum(_money(t.get("display_amount")) for t in all_transactions), 2)
    total_pages = max((total_count + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page
    transactions = all_transactions[skip:skip + per_page]

    for txn in transactions:
        txn["user"] = txn.get("user") or {}
        txn["admin"] = txn.get("admin") or {}
        txn["profile_label"] = txn.get("profile_label") or _profile_label(txn)
        txn["source_label"] = txn.get("source_label") or _source_label(txn)
        txn["display_date"] = txn.get("display_date") or txn.get("verified_at") or txn.get("created_at")

    admins = []
    if is_main_admin:
        admin_query = {"role": "admin"}
        if search_q:
            admin_query.update(_user_search_query(search_q))
        admins = list(users_col.find(admin_query, {"first_name": 1, "last_name": 1, "username": 1, "email": 1}).sort("first_name", 1))

    return render_template(
        "paystack_transactions.html",
        is_main_admin=is_main_admin,
        profile=profile,
        range_preset=range_preset,
        start_date=start_date,
        end_date=end_date,
        admin_filter=admin_filter,
        search_q=search_q,
        admins=admins,
        transactions=transactions,
        total_amount=round(total_amount, 2),
        total_count=total_count,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
