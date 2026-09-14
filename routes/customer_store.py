from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Tuple, Optional
import os, re

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from agent_code_utils import get_or_create_agent_code_for_user
from db import db
from withdraw_requests import update_withdraw_request_status

customer_store_bp = Blueprint("customer_store", __name__)

# Collections
stores_col               = db["stores"]
orders_col               = db["orders"]
users_col                = db["users"]
balances_col             = db["balances"]
balance_logs_col         = db["balance_logs"]
transactions_col         = db["transactions"]
store_payouts_col        = db["store_payouts"]
store_payout_logs        = db["store_payout_logs"]
store_withdraw_requests  = db["store_withdraw_requests"]
store_accounts_col       = db["store_accounts"]

MIN_WITHDRAW_AMOUNT = 20.0
MOMO_WITHDRAW_FEE_RATE = 0.005
STORE_PUBLIC_HOST = os.getenv("STORE_PUBLIC_HOST", "nagmart.store").strip()


# ---------- helpers ----------
def _store_owner_logged_in() -> bool:
    return bool(session.get("role") in {"customer", "agent"} and session.get("user_id"))

def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end

def _fmt_money(x: Any) -> float:
    try:
        return round(float(x or 0), 2)
    except Exception:
        return 0.0

def _ensure_owner_store(user_id: ObjectId, slug: str) -> Optional[Dict[str, Any]]:
    return stores_col.find_one({"owner_id": user_id, "slug": slug, "status": {"$ne": "deleted"}})

def _latest_owner_store(user_id: ObjectId) -> Optional[Dict[str, Any]]:
    return stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)]
    )

def _owner_display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Store Owner"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "Store Owner"

def _owner_wallet_balance(user_id: ObjectId) -> float:
    bal = balances_col.find_one({"user_id": user_id}) or {}
    return _fmt_money(bal.get("amount"))

def _withdraw_fee_for_method(method: str, net_amount: float) -> float:
    if (method or "").strip().lower() == "momo":
        return _fmt_money(net_amount * MOMO_WITHDRAW_FEE_RATE)
    return 0.0

def _gross_withdraw_amount(method: str, net_amount: float) -> float:
    return _fmt_money(net_amount + _withdraw_fee_for_method(method, net_amount))

def _credit_owner_wallet_from_store(owner_id: ObjectId, slug: str, amount: float, reference: str) -> float:
    now = datetime.utcnow()
    bal_doc = balances_col.find_one({"user_id": owner_id}) or {}
    before = _fmt_money(bal_doc.get("amount"))
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


def _request_status_for_method(method: str) -> str:
    return "requested"

def _profit_all_time(slug: str) -> float:
    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }
    pipeline = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "p": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}}}},
    ]
    agg = list(orders_col.aggregate(pipeline))
    return _fmt_money(agg[0]["p"]) if agg else 0.0

def _withdrawn_so_far(slug: str) -> float:
    pipeline = [
        {"$match": {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}},
        {"$group": {"_id": None, "amt": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return _fmt_money(agg[0]["amt"]) if agg else 0.0

def _withdrawable(slug: str) -> float:
    acct = store_accounts_col.find_one({"store_slug": slug}, {"total_profit_balance": 1}) or {}
    return _fmt_money(acct.get("total_profit_balance"))


def _open_withdraw_request_total(owner_id: ObjectId, slug: str) -> float:
    pipeline = [
        {
            "$match": {
                "owner_id": owner_id,
                "store_slug": slug,
                "status": {"$in": ["requested", "pending", "processing"]},
            }
        },
        {
            "$group": {
                "_id": None,
                "amt": {"$sum": {"$toDouble": {"$ifNull": ["$debit_amount", "$amount"]}}},
            }
        },
    ]
    agg = list(store_withdraw_requests.aggregate(pipeline))
    return _fmt_money(agg[0].get("amt")) if agg else 0.0

def _get_auto_withdraw_settings(slug: str) -> Dict[str, Any]:
    acct = store_accounts_col.find_one(
        {"store_slug": slug},
        {"auto_withdraw_enabled": 1, "auto_withdraw_amount": 1, "auto_withdraw_method": 1},
    ) or {}
    method = (acct.get("auto_withdraw_method") or "momo").strip().lower()
    if method != "momo":
        method = "momo"
    return {
        "enabled": bool(acct.get("auto_withdraw_enabled")),
        "amount": _fmt_money(acct.get("auto_withdraw_amount")),
        "method": method,
    }

def _maybe_auto_withdraw(owner_id: ObjectId, slug: str) -> Optional[Dict[str, Any]]:
    settings = _get_auto_withdraw_settings(slug)
    if not settings.get("enabled"):
        return None

    amount = _fmt_money(settings.get("amount"))
    method = str(settings.get("method") or "momo").strip().lower()
    if method != "momo":
        method = "momo"
    if amount < MIN_WITHDRAW_AMOUNT:
        return None

    max_allowed = _withdrawable(slug)
    debit_amount = _gross_withdraw_amount(method, amount)
    available_to_request = _fmt_money(max_allowed - _open_withdraw_request_total(owner_id, slug))
    if max_allowed < debit_amount - 1e-9:
        return None
    if max_allowed < MIN_WITHDRAW_AMOUNT - 1e-9:
        return None
    if available_to_request < debit_amount - 1e-9:
        return None

    pending = store_withdraw_requests.find_one({
        "owner_id": owner_id,
        "store_slug": slug,
        "status": {"$in": ["requested", "pending", "processing"]},
    })
    if pending:
        return None

    payout_snapshot = None
    if method == "momo":
        payout = store_payouts_col.find_one(
            {"owner_id": owner_id, "store_slug": slug},
            {"recipient_name": 1, "msisdn": 1, "network": 1}
        ) or {}
        if not (payout.get("msisdn") and payout.get("recipient_name")):
            return None
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }

    doc_id = ObjectId()
    now = datetime.utcnow()
    reference = _make_reference("WDR", doc_id)
    wallet_balance_after = None
    if method == "wallet":
        updated = store_accounts_col.update_one(
            {"store_slug": slug, "total_profit_balance": {"$gte": debit_amount}},
            {
                "$inc": {"total_profit_balance": -debit_amount},
                "$set": {"updated_at": now},
                "$push": {
                    "history": {
                        "event": "withdrawal_request",
                        "reference": reference,
                        "method": method,
                        "amount": amount,
                        "fee_amount": 0.0,
                        "debit_amount": debit_amount,
                        "status": "paid",
                        "created_at": now,
                    }
                },
            },
        )
        if updated.matched_count == 0:
            return None
        wallet_balance_after = _credit_owner_wallet_from_store(owner_id, slug, amount, reference)
        transactions_col.insert_one({
            "user_id": owner_id,
            "amount": amount,
            "reference": reference,
            "status": "success",
            "type": "store_withdrawal",
            "gateway": "Internal",
            "currency": "GHS",
            "created_at": now,
            "verified_at": now,
            "meta": {
                "store_slug": slug,
                "method": "wallet",
                "profit_debited": True,
                "wallet_credited": True,
                "fee_amount": 0.0,
                "debit_amount": debit_amount,
                "net_amount": amount,
                "note": "Automatic store profit withdrawal to wallet",
            },
        })

    doc = {
        "_id": doc_id,
        "reference": reference,
        "owner_id": owner_id,
        "store_slug": slug,
        "amount": amount,
        "net_amount": amount,
        "fee_rate": MOMO_WITHDRAW_FEE_RATE if method == "momo" else 0.0,
        "fee_amount": _withdraw_fee_for_method(method, amount),
        "debit_amount": debit_amount,
        "method": method,
        "payout_snapshot": payout_snapshot,
        "status": "paid" if method == "wallet" else "requested",
        "note": "auto_withdraw",
        "profit_debited": bool(method == "wallet"),
        "profit_refunded": False,
        "created_at": now,
        "updated_at": now,
    }
    if method == "wallet":
        doc["paid_at"] = now
        doc["wallet_credited"] = True
    store_withdraw_requests.insert_one(doc)
    out = {"id": str(doc_id), "reference": doc["reference"], "method": method, "status": doc["status"]}
    if wallet_balance_after is not None:
        out["wallet_balance"] = wallet_balance_after
    return out

def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    if isinstance(dt, str):
        return dt[:16]
    return ""

def _make_reference(prefix: str, oid: ObjectId) -> str:
    d = datetime.utcnow().strftime("%Y%m%d")
    tail = str(oid)[-6:].upper()
    return f"{prefix}-{d}-{tail}"

def _store_public_url(slug: Optional[str]) -> str:
    slug_s = str(slug or "").strip().strip("/")
    if not slug_s:
        return ""
    base = (request.url_root or "").rstrip("/")
    if not base:
        host = (STORE_PUBLIC_HOST or "").strip()
        if host:
            base = f"https://{host}"
    return f"{base}/s/{slug_s}" if base else f"/s/{slug_s}"

def _admin_guard() -> bool:
    return bool(session.get("user_id")) and (session.get("role") in ("admin", "superadmin"))

def _clean_phone(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

def _extract_order_phones(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Orders created by store_page.py store phone at items[].phone
    We derive:
      - phone_primary
      - phone_count
      - phone_summary
    """
    phones: List[str] = []

    top_phone = _clean_phone(order_doc.get("phone") or order_doc.get("customer_phone"))
    if top_phone:
        phones.append(top_phone)

    items = order_doc.get("items") or []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            p = _clean_phone(it.get("phone"))
            if p:
                phones.append(p)

    uniq: List[str] = []
    seen = set()
    for p in phones:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)

    phone_primary = uniq[0] if uniq else ""
    phone_count = len(uniq)

    if not uniq:
        summary = ""
    else:
        show = uniq[:4]
        summary = ", ".join(show)
        if len(uniq) > 4:
            summary += f" (+{len(uniq)-4} more)"

    return {
        "phone_primary": phone_primary or None,
        "phone_count": phone_count,
        "phone_summary": summary or None,
    }

def _gather_dashboard(slug: str) -> Dict[str, Any]:
    today = datetime.utcnow().date()
    d0, d1 = _day_range(today)
    y0, y1 = _day_range(today - timedelta(days=1))

    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }

    pipeline_totals = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": None,
            "total_sales": {"$sum": {"$ifNull": ["$total_amount", 0]}},
            "total_profit": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}},
            "orders_count": {"$sum": 1},
        }},
    ]
    agg_tot = list(orders_col.aggregate(pipeline_totals))
    all_time_sales  = _fmt_money(agg_tot[0].get("total_sales") if agg_tot else 0)
    all_time_profit = _fmt_money(agg_tot[0].get("total_profit") if agg_tot else 0)
    orders_count    = int(agg_tot[0].get("orders_count") if agg_tot else 0)

    pipeline_today_profit = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "profit_today": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}}}},
    ]
    agg_today = list(orders_col.aggregate(pipeline_today_profit))
    profit_today = _fmt_money(agg_today[0].get("profit_today") if agg_today else 0)

    pipeline_sales_today = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$group": {"_id": None, "sales_today": {"$sum": {"$ifNull": ["$total_amount", 0]}}}},
    ]
    pipeline_sales_yesterday = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": y0, "$lt": y1}}},
        {"$group": {"_id": None, "sales_yesterday": {"$sum": {"$ifNull": ["$total_amount", 0]}}}},
    ]
    agg_sales_today = list(orders_col.aggregate(pipeline_sales_today))
    agg_sales_yesterday = list(orders_col.aggregate(pipeline_sales_yesterday))
    sales_today = _fmt_money(agg_sales_today[0].get("sales_today") if agg_sales_today else 0)
    sales_yesterday = _fmt_money(agg_sales_yesterday[0].get("sales_yesterday") if agg_sales_yesterday else 0)
    if abs(sales_yesterday) < 1e-9:
        sales_change_pct = 100.0 if sales_today > 0 else 0.0
    else:
        sales_change_pct = ((sales_today - sales_yesterday) / abs(sales_yesterday)) * 100.0

    pipeline_top_offers = [
        {"$match": {"store_slug": slug}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": {
                "service": {"$ifNull": ["$items.serviceName", "Unknown Service"]},
                "label":   {"$ifNull": ["$items.value", "-"]},
            },
            "count":   {"$sum": 1},
            "revenue": {"$sum": {"$ifNull": ["$items.amount", 0]}},
        }},
        {"$sort": {"count": -1, "revenue": -1}},
        {"$limit": 8},
    ]
    top_offers_raw = list(orders_col.aggregate(pipeline_top_offers))
    top_offers = [{
        "service": x["_id"]["service"],
        "label":   x["_id"]["label"],
        "count":   int(x.get("count", 0)),
        "revenue": _fmt_money(x.get("revenue", 0)),
    } for x in top_offers_raw]

    recent_orders_cur = (
        orders_col.find({"store_slug": slug})
        .sort("created_at", -1)
        .limit(10)
    )

    recent_orders: List[Dict[str, Any]] = []
    for o in recent_orders_cur:
        items = o.get("items") or []
        store_profit_total = 0.0
        found_store_profit = False
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                sp = it.get("store_profit_amount")
                if sp is not None:
                    found_store_profit = True
                    store_profit_total += _fmt_money(sp)
        if not found_store_profit:
            store_profit_total = _fmt_money(o.get("profit_amount_total", 0))

        phone_info = _extract_order_phones(o)
        recent_orders.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "total_amount":        _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": store_profit_total,
            "charged_amount":      _fmt_money(o.get("charged_amount", 0)),
            "items_count": len(o.get("items") or []),
            "created_at": o.get("created_at"),
            **phone_info,
        })

    return {
        "today": today,
        "all_time_sales": all_time_sales,
        "profit_today": profit_today,
        "all_time_profit": all_time_profit,
        "orders_count": orders_count,
        "top_offers": top_offers,
        "recent_orders": recent_orders,
        "withdrawable": _withdrawable(slug),
        "sales_today": sales_today,
        "sales_yesterday": sales_yesterday,
        "sales_change_pct": round(sales_change_pct, 2),
    }


# ---------- Pages ----------
@customer_store_bp.route("/customer/store", methods=["GET"])
def customer_store_home():
    if not _store_owner_logged_in():
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    owner = users_col.find_one(
        {"_id": owner_id},
        {"full_name": 1, "name": 1, "username": 1, "email": 1, "stage_label": 1, "admin_id": 1},
    )
    agent_code_doc = get_or_create_agent_code_for_user(owner_id, admin_id=(owner or {}).get("admin_id"))
    agent_code = {
        "agent_code": (agent_code_doc or {}).get("agent_code") or "",
        "status": ((agent_code_doc or {}).get("status") or "active").strip().lower(),
    }
    store_doc = _latest_owner_store(owner_id)

    if not store_doc:
        today = datetime.utcnow().date()
        return render_template(
            "customer_store.html",
            store=None,
            owner_name=_owner_display_name(owner),
            owner_stage=(owner.get("stage_label") if owner else None),
            all_time_sales=0.00,
            profit_today=0.00,
            all_time_profit=0.00,
            orders_count=0,
            top_offers=[],
            recent_orders=[],
            withdrawable=0.00,
            wallet_balance=_owner_wallet_balance(owner_id),
            today_str=today.strftime("%b %d, %Y"),
            sales_today=0.00,
            sales_yesterday=0.00,
            sales_change_pct=0.00,
            slug=None,
            store_host=STORE_PUBLIC_HOST,
            store_url="",
            agent_code=agent_code,
        )

    slug = store_doc.get("slug")
    _maybe_auto_withdraw(owner_id, slug)
    k = _gather_dashboard(slug)

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        owner_stage=(owner.get("stage_label") if owner else None),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        sales_today=k["sales_today"],
        sales_yesterday=k["sales_yesterday"],
        sales_change_pct=k["sales_change_pct"],
        slug=slug,
        store_host=STORE_PUBLIC_HOST,
        store_url=_store_public_url(slug),
        agent_code=agent_code,
    )

@customer_store_bp.route("/customer/store/<slug>", methods=["GET"])
def customer_store_dashboard(slug: str):
    if not _store_owner_logged_in():
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return redirect(url_for("customer_store.customer_store_home"))

    _maybe_auto_withdraw(owner_id, slug)
    k = _gather_dashboard(slug)
    owner = users_col.find_one(
        {"_id": owner_id},
        {"full_name": 1, "name": 1, "username": 1, "email": 1, "stage_label": 1, "admin_id": 1},
    )
    agent_code_doc = get_or_create_agent_code_for_user(owner_id, admin_id=(owner or {}).get("admin_id"))
    agent_code = {
        "agent_code": (agent_code_doc or {}).get("agent_code") or "",
        "status": ((agent_code_doc or {}).get("status") or "active").strip().lower(),
    }

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        owner_stage=(owner.get("stage_label") if owner else None),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        sales_today=k["sales_today"],
        sales_yesterday=k["sales_yesterday"],
        sales_change_pct=k["sales_change_pct"],
        slug=slug,
        store_url=_store_public_url(slug),
        agent_code=agent_code,
    )


# ---------- Customer APIs ----------
@customer_store_bp.route("/api/customer/store/<slug>/payout_snapshot", methods=["GET"])
def api_customer_store_payout_snapshot(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    payout = store_payouts_col.find_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"recipient_name": 1, "msisdn": 1, "network": 1}
    ) or {}
    return jsonify({"success": True, "payout": {
        "recipient_name": payout.get("recipient_name"),
        "msisdn": payout.get("msisdn"),
        "network": payout.get("network"),
    }})

@customer_store_bp.route("/api/customer/store/<slug>/withdrawals", methods=["GET"])
def api_customer_store_withdrawals(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except Exception:
        limit = 20
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    skip = (page - 1) * limit

    cur = transactions_col.find(
        {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}
    ).sort("created_at", -1).skip(skip).limit(limit)

    items = []
    for t in cur:
        meta = (t.get("meta") or {})
        note = meta.get("note") or ""
        method = meta.get("method")
        if method == "momo":
            note = note or "Paid to MoMo"
        elif method == "wallet":
            note = note or "Credited to wallet"

        items.append({
            "reference": t.get("reference"),
            "amount": _fmt_money(t.get("amount")),
            "net_amount": _fmt_money(meta.get("net_amount") or t.get("amount")),
            "fee_amount": _fmt_money(meta.get("fee_amount")),
            "debit_amount": _fmt_money(meta.get("debit_amount") or t.get("amount")),
            "method": method,
            "created_at": _iso(t.get("created_at")),
            "verified_at": _iso(t.get("verified_at")),
            "note": note or "Paid",
        })

    total = transactions_col.count_documents({"type": "store_withdrawal", "status": "success", "meta.store_slug": slug})
    return jsonify({"success": True, "items": items, "page": page, "limit": limit, "total": total})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/requests", methods=["GET"])
def api_customer_store_withdraw_requests(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    cur = store_withdraw_requests.find(
        {"owner_id": owner_id, "store_slug": slug},
        sort=[("created_at", -1)]
    ).limit(limit)

    items = []
    for r in cur:
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "amount": _fmt_money(r.get("amount")),
            "net_amount": _fmt_money(r.get("net_amount") or r.get("amount")),
            "fee_amount": _fmt_money(r.get("fee_amount")),
            "debit_amount": _fmt_money(r.get("debit_amount") or r.get("amount")),
            "fee_rate": float(r.get("fee_rate") or 0),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/customer/store/<slug>/auto-withdraw", methods=["GET", "POST"])
def api_customer_store_auto_withdraw(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    if request.method == "GET":
        settings = _get_auto_withdraw_settings(slug)
        return jsonify({"success": True, "settings": settings})

    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    amount = _fmt_money(payload.get("amount"))
    method = str(payload.get("method") or "momo").strip().lower()

    if method != "momo":
        return jsonify({"success": False, "message": "Store profit withdrawals are MoMo only."}), 400
    if enabled and amount < MIN_WITHDRAW_AMOUNT:
        return jsonify({"success": False, "message": f"Minimum auto-withdraw amount is GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400

    store_accounts_col.update_one(
        {"store_slug": slug},
        {"$set": {
            "store_slug": slug,
            "auto_withdraw_enabled": enabled,
            "auto_withdraw_amount": amount,
            "auto_withdraw_method": method,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    auto_result = _maybe_auto_withdraw(owner_id, slug)
    return jsonify({"success": True, "settings": _get_auto_withdraw_settings(slug), "auto_request": auto_result})

@customer_store_bp.route("/api/customer/store/<slug>/auto-withdraw/run", methods=["POST"])
def api_customer_store_auto_withdraw_run(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    auto_result = _maybe_auto_withdraw(owner_id, slug)
    return jsonify({"success": True, "auto_request": auto_result})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/request", methods=["POST"])
def api_customer_store_request_withdraw(slug: str):
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return jsonify({"success": False, "message": "Store not found"}), 404

    payload = request.get_json(silent=True) or {}
    amount = _fmt_money(payload.get("amount"))
    method = str(payload.get("method") or "momo").strip().lower()

    if method not in {"momo", "wallet"}:
        return jsonify({"success": False, "message": "Withdrawal method must be Wallet or MoMo."}), 400
    if amount <= 0:
        return jsonify({"success": False, "message": "Enter a valid amount"}), 400
    if amount < MIN_WITHDRAW_AMOUNT:
        return jsonify({"success": False, "message": f"Minimum withdrawal amount is GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400

    max_allowed = _withdrawable(slug)
    fee_amount = _withdraw_fee_for_method(method, amount)
    debit_amount = _gross_withdraw_amount(method, amount)
    available_to_request = _fmt_money(max_allowed - _open_withdraw_request_total(owner_id, slug))
    if max_allowed < MIN_WITHDRAW_AMOUNT - 1e-9:
        return jsonify({"success": False, "message": f"Profit balance must be at least GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400
    if method == "wallet" and debit_amount - max_allowed > 1e-9:
        return jsonify({
            "success": False,
            "message": f"Amount plus fee exceeds profit balance (need GHS {debit_amount:.2f}, available GHS {max_allowed:.2f})",
        }), 400
    if method == "momo" and debit_amount - available_to_request > 1e-9:
        return jsonify({
            "success": False,
            "message": f"Open requests already cover part of your balance. Need GHS {debit_amount:.2f}, available for new requests GHS {available_to_request:.2f}.",
        }), 400

    payout_snapshot = None
    if method == "momo":
        payout = store_payouts_col.find_one(
            {"owner_id": owner_id, "store_slug": slug},
            {"recipient_name": 1, "msisdn": 1, "network": 1}
        ) or {}
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }
        if not (payout_snapshot.get("recipient_name") and payout_snapshot.get("msisdn")):
            return jsonify({
                "success": False,
                "message": "Set your MoMo payout name and number before requesting a MoMo withdrawal.",
            }), 400

    recent_pending = store_withdraw_requests.find_one({
        "owner_id": owner_id,
        "store_slug": slug,
        "method": method,
        "status": {"$in": ["requested", "pending"]},
        "amount": amount,
        "created_at": {"$gte": datetime.utcnow() - timedelta(minutes=2)}
    })
    if recent_pending:
        return jsonify({"success": True, "message": "Request already submitted", "id": str(recent_pending["_id"])})

    doc_id = ObjectId()
    now = datetime.utcnow()
    reference = _make_reference("WDR", doc_id)
    request_status = _request_status_for_method(method)
    admin_id = store.get("admin_id")

    wallet_balance = _owner_wallet_balance(owner_id)
    if method == "wallet":
        updated = store_accounts_col.update_one(
            {"store_slug": slug, "total_profit_balance": {"$gte": debit_amount}},
            {
                "$inc": {"total_profit_balance": -debit_amount},
                "$set": {"updated_at": now},
                "$push": {
                    "history": {
                        "event": "withdrawal_request",
                        "reference": reference,
                        "method": method,
                        "amount": amount,
                        "fee_amount": 0.0,
                        "debit_amount": debit_amount,
                        "status": "paid",
                        "created_at": now,
                    }
                },
            },
        )
        if updated.matched_count == 0:
            return jsonify({"success": False, "message": "Insufficient store profit balance"}), 400
        wallet_balance = _credit_owner_wallet_from_store(owner_id, slug, amount, reference)

    doc = {
        "_id": doc_id,
        "reference": reference,
        "owner_id": owner_id,
        "admin_id": admin_id,
        "store_slug": slug,
        "amount": amount,
        "net_amount": amount,
        "fee_rate": MOMO_WITHDRAW_FEE_RATE if method == "momo" else 0.0,
        "fee_amount": fee_amount,
        "debit_amount": debit_amount,
        "method": method,
        "payout_snapshot": payout_snapshot,
        "status": "paid" if method == "wallet" else request_status,
        "note": "",
        "profit_debited": bool(method == "wallet"),
        "profit_refunded": False,
        "created_at": now,
        "updated_at": now,
    }
    if method == "wallet":
        doc["paid_at"] = now
        doc["wallet_credited"] = True
    store_withdraw_requests.insert_one(doc)

    transactions_col.insert_one({
        "user_id": owner_id,
        **({"admin_id": admin_id} if admin_id else {}),
        "amount": amount,
        "reference": reference,
        "status": "success" if method == "wallet" else request_status,
        "type": "store_withdrawal",
        "gateway": "MoMo" if method == "momo" else "Internal",
        "currency": "GHS",
        "created_at": now,
        "verified_at": now if method == "wallet" else None,
        "meta": {
            "store_slug": slug,
            "method": method,
            "profit_debited": bool(method == "wallet"),
            "wallet_credited": bool(method == "wallet"),
            "fee_rate": MOMO_WITHDRAW_FEE_RATE if method == "momo" else 0.0,
            "fee_amount": fee_amount,
            "debit_amount": debit_amount,
            "net_amount": amount,
            "payout_snapshot": payout_snapshot or {},
            "note": (
                "Store owner requested MoMo withdrawal."
                if method == "momo"
                else "Store owner withdrew store profit to wallet instantly."
            ),
        },
    })

    return jsonify({
        "success": True,
        "id": str(doc_id),
        "reference": doc["reference"],
        "method": method,
        "status": doc["status"],
        "amount": amount,
        "net_amount": amount,
        "fee_amount": fee_amount,
        "debit_amount": debit_amount,
        "withdrawable_left": _withdrawable(slug),
        "wallet_balance": wallet_balance,
        "message": (
            f"Wallet withdrawal completed. GHS {amount:.2f} has been credited to your wallet."
            if method == "wallet"
            else f"MoMo request submitted. It is waiting for admin confirmation. GHS {debit_amount:.2f} will be deducted only after approval."
        ),
    })


@customer_store_bp.route("/api/customer/store/<slug>/orders/search", methods=["GET"])
def api_customer_store_orders_search(slug: str):
    """
    Default returns latest 10.
    If q provided, searches:
      - order_id (regex)
      - items.phone (regex)
      - phone / customer_phone (regex)

    Critical Fix:
      - No projection collision (do NOT project 'items' and 'items.phone' together)
      - Use aggregation to return a LIGHT items array with only {phone}
    """
    if not _store_owner_logged_in():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    q_raw = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    q_digits = re.sub(r"\D+", "", q_raw or "").strip()

    match: Dict[str, Any] = {"store_slug": slug}

    if q_raw:
        rx_any = {"$regex": re.escape(q_raw), "$options": "i"}

        or_terms: List[Dict[str, Any]] = [
            {"order_id": rx_any},
            {"items.phone": rx_any},
            {"phone": rx_any},
            {"customer_phone": rx_any},
        ]

        if q_digits and len(q_digits) >= 6:
            rx_d = {"$regex": re.escape(q_digits), "$options": "i"}
            or_terms.extend([
                {"items.phone": rx_d},
                {"phone": rx_d},
                {"customer_phone": rx_d},
            ])

        match["$or"] = or_terms

    try:
        order_profit_expr = {
            "$let": {
                "vars": {
                    "items_profit": {
                        "$sum": {
                            "$map": {
                                "input": {"$ifNull": ["$items", []]},
                                "as": "it",
                                "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                            }
                        }
                    },
                    "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
                },
                "in": {
                    "$cond": [
                        {"$gt": ["$$items_profit", 0]},
                        "$$items_profit",
                        "$$legacy_profit",
                    ]
                },
            }
        }
        pipeline = [
            {"$match": match},
            {"$sort": {"created_at": -1}},
            {"$limit": int(limit)},
            {"$addFields": {"store_profit_sum": order_profit_expr}},
            {
                "$project": {
                    "_id": 0,
                    "order_id": 1,
                    "status": 1,
                    "total_amount": 1,
                    "profit_amount_total": "$store_profit_sum",
                    "charged_amount": 1,
                    "created_at": 1,
                    "phone": 1,
                    "customer_phone": 1,

                    "items_count": {"$size": {"$ifNull": ["$items", []]}},

                    # Light items for phone extraction
                    "items": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"phone": {"$ifNull": ["$$it.phone", None]}}
                        }
                    },
                }
            },
        ]
        docs = list(orders_col.aggregate(pipeline))
    except Exception as e:
        return jsonify({"success": False, "message": f"Search failed: {str(e)}"}), 500

    items: List[Dict[str, Any]] = []
    for o in docs:
        phone_info = _extract_order_phones(o)
        items.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "total_amount": _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": _fmt_money(o.get("profit_amount_total", 0)),
            "charged_amount": _fmt_money(o.get("charged_amount", 0)),
            "items_count": int(o.get("items_count") or 0),
            "created_at": _iso(o.get("created_at")),
            **phone_info,
        })

    return jsonify({"success": True, "items": items})


# ---------- Admin APIs ----------
@customer_store_bp.route("/api/admin/store/withdraw/requests", methods=["GET"])
def api_admin_store_withdraw_requests():
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    status = (request.args.get("status") or "").strip().lower()
    slug = (request.args.get("slug") or "").strip()
    owner_id_raw = (request.args.get("owner_id") or "").strip()
    q = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50

    query: Dict[str, Any] = {}
    if status in ("requested", "pending", "paid", "rejected", "canceled"):
        query["status"] = status
    if slug:
        query["store_slug"] = slug
    if owner_id_raw:
        try:
            query["owner_id"] = ObjectId(owner_id_raw)
        except Exception:
            return jsonify({"success": False, "message": "Invalid owner_id"}), 400
    if q:
        query["reference"] = {"$regex": re.escape(q), "$options": "i"}

    cur = store_withdraw_requests.find(query).sort("created_at", -1).limit(limit)

    items = []
    for r in cur:
        payout = r.get("payout_snapshot") or {}
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "store_slug": r.get("store_slug"),
            "owner_id": str(r.get("owner_id")),
            "amount": _fmt_money(r.get("amount")),
            "net_amount": _fmt_money(r.get("net_amount") or r.get("amount")),
            "fee_amount": _fmt_money(r.get("fee_amount")),
            "debit_amount": _fmt_money(r.get("debit_amount") or r.get("amount")),
            "fee_rate": float(r.get("fee_rate") or 0),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
            "payout_snapshot": payout,
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/admin/store/withdraw/<request_id>/status", methods=["POST"])
def api_admin_store_withdraw_update_status(request_id: str):
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    payload = request.get_json(silent=True) or {}
    new_status = str(payload.get("status") or "").strip().lower()
    note = str(payload.get("note") or "").strip()

    ok, payload, code = update_withdraw_request_status(
        req_id=request_id,
        new_status=new_status,
        actor_id=session.get("user_id") or "admin",
        note=note,
    )
    if ok:
        return jsonify({"success": True, **payload}), code
    return jsonify({"success": False, "message": payload.get("message")}), code


# ---------- Payout settings ----------
@customer_store_bp.route("/customer/store/<slug>/payout", methods=["GET"])
def customer_store_payout_page(slug: str):
    if not _store_owner_logged_in():
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    payout = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    hist = list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1).limit(100))

    return render_template(
        "customer_store_payout.html",
        store=store,
        current=payout,
        history=hist,
        withdrawable=_withdrawable(slug),
        wallet_balance=_owner_wallet_balance(owner_id),
    )

@customer_store_bp.route("/customer/store/<slug>/payout", methods=["POST"])
def customer_store_payout_save(slug: str):
    if not _store_owner_logged_in():
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    name = (request.form.get("recipient_name") or "").strip()
    phone = (request.form.get("msisdn") or "").strip()
    network = (request.form.get("network") or "").strip().upper()

    valid_nets = {"MTN", "VODAFONE", "AIRTELTIGO"}
    if network not in valid_nets:
        return render_template(
            "customer_store_payout.html",
            store=store,
            current={"recipient_name": name, "msisdn": phone, "network": network},
            history=list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1)),
            error="Select a valid network.",
            withdrawable=_withdrawable(slug),
            wallet_balance=_owner_wallet_balance(owner_id),
        )

    def _normalize_phone(raw: str) -> str:
        p = raw.replace(" ", "").replace("-", "").replace("+", "")
        if p.startswith("0") and len(p) == 10:
            p = "233" + p[1:]
        if p.startswith("233") and len(p) == 12:
            return p
        return raw.strip()

    phone_norm = _normalize_phone(phone)

    prev = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    doc = {
        "owner_id": owner_id,
        "store_slug": slug,
        "recipient_name": name,
        "msisdn": phone_norm,
        "network": network,
        "updated_at": datetime.utcnow(),
        "created_at": prev.get("created_at") or datetime.utcnow(),
    }
    store_payouts_col.update_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"$set": doc},
        upsert=True
    )

    changes: Dict[str, Dict[str, Any]] = {}
    for k in ("recipient_name", "msisdn", "network"):
        old_v = prev.get(k)
        new_v = doc.get(k)
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}
    if changes:
        store_payout_logs.insert_one({
            "owner_id": owner_id,
            "store_slug": slug,
            "changes": changes,
            "created_at": datetime.utcnow(),
        })

    return redirect(url_for("customer_store.customer_store_payout_page", slug=slug))
