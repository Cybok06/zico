import random
from datetime import datetime
from typing import Any, Dict, List

from bson.objectid import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from checker_pricing import admin_stage_price, checker_base_cost, customer_stage_price, get_checker_pricing_doc, normalize_checker_type
from db import db
from profit_ledger import apply_profit_split, normalize_profit_line, profit_totals
from social_boosting_pricing import normalize_admin_level
from sms_sender import normalize_ghana_sms_phone, resolve_admin_sender_name, send_sms
from tenant import resolve_admin_id_for_user_id


purchase_checker_bp = Blueprint("purchase_checker", __name__)

checker_stock_col = db["wassce_checker"]
balances_col = db["balances"]
users_col = db["users"]
purchase_history_col = db["purchase_history"]
stores_col = db["stores"]
orders_col = db["orders"]


CHECKER_TYPES = ("wassce", "bece")


def _viewer_context(user_oid: ObjectId):
    user_doc = users_col.find_one({"_id": user_oid}, {"stage_label": 1}) or {}
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) if admin_id else {}
    admin_level = normalize_admin_level((admin_doc or {}).get("admin_level"))
    stage_label = (user_doc or {}).get("stage_label") or "Normal Agent"
    return admin_id, admin_level, stage_label


def _owner_store(user_id: ObjectId) -> Dict[str, Any] | None:
    return stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def _inventory_summary() -> Dict[str, Dict[str, float]]:
    summary: Dict[str, Dict[str, float]] = {}
    for checker_type in CHECKER_TYPES:
        pricing_doc = get_checker_pricing_doc(checker_type)
        summary[checker_type] = {
            "available": checker_stock_col.count_documents({"type": checker_type, "status": "not_sold"}),
            "cost_price": round(float(checker_base_cost(pricing_doc)), 2),
        }
    return summary


def _store_checker_config(store: Dict[str, Any] | None, inventory: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    cfg = (store or {}).get("checker_product") or {}
    by_type = cfg.get("types") or {}
    items: List[Dict[str, Any]] = []
    for checker_type in CHECKER_TYPES:
        row = by_type.get(checker_type) or {}
        raw_price = row.get("price")
        try:
            selling_price = round(float(raw_price), 2) if raw_price not in (None, "") else 0.0
        except Exception:
            selling_price = 0.0
        items.append(
            {
                "type": checker_type,
                "label": checker_type.upper(),
                "enabled": bool(row.get("enabled")),
                "selling_price": selling_price,
                "cost_price": round(float((inventory.get(checker_type) or {}).get("cost_price") or 0), 2),
                "available": int((inventory.get(checker_type) or {}).get("available") or 0),
            }
        )
    return {
        "enabled": bool(cfg.get("enabled")),
        "items": items,
    }


def _ussd_checker_stats(store: Dict[str, Any] | None) -> Dict[str, Any]:
    if not store or not store.get("slug"):
        return {"total_purchased": 0, "total_profit": 0.0}
    pipeline = [
        {"$match": {"store_slug": store.get("slug"), "source": "ussd_results_checker"}},
        {
            "$group": {
                "_id": None,
                "total_purchased": {"$sum": 1},
                "total_profit": {"$sum": {"$toDouble": {"$ifNull": ["$profit_amount", 0]}}},
            }
        },
    ]
    row = next(iter(purchase_history_col.aggregate(pipeline)), None)
    if not row:
        return {"total_purchased": 0, "total_profit": 0.0}
    return {
        "total_purchased": int(row.get("total_purchased") or 0),
        "total_profit": round(float(row.get("total_profit") or 0), 2),
    }


def _checker_price(checker: dict, admin_id, admin_level, stage_label):
    pricing_doc = get_checker_pricing_doc(checker.get("type"))
    return customer_stage_price(
        pricing_doc,
        admin_id=admin_id,
        admin_level=admin_level,
        stage_label=stage_label,
        legacy_amount=checker.get("amount"),
    )


def _checker_base_cost(checker_type: str) -> float:
    return checker_base_cost(get_checker_pricing_doc(checker_type))


def _clear_dashboard_cache_safely():
    try:
        from admin_dashboard import clear_dashboard_cache

        clear_dashboard_cache()
    except Exception:
        pass


def _checker_profit_layers(checker_type: str, admin_id, admin_level, stage_label, selling_amount):
    pricing_doc = get_checker_pricing_doc(checker_type)
    main_base = checker_base_cost(pricing_doc)
    admin_price = admin_stage_price(pricing_doc, admin_level, legacy_amount=main_base) or main_base
    selling = float(selling_amount or 0.0)
    return {
        "main_base_amount": round(float(main_base or 0.0), 2),
        "admin_base_amount": round(float(admin_price or 0.0), 2),
        "selling_amount": round(float(selling or 0.0), 2),
    }


def _store_checker_price(store_slug: str, checker_type: str):
    slug = str(store_slug or "").strip()
    normalized_type = normalize_checker_type(checker_type)
    if not slug or not normalized_type:
        return None
    store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}}, {"checker_product": 1})
    if not store_doc:
        return None
    cfg = (store_doc.get("checker_product") or {})
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    types_cfg = cfg.get("types") or {}
    if not isinstance(types_cfg, dict):
        return None
    raw = types_cfg.get(normalized_type) or {}
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    try:
        price = round(float(raw.get("price") or 0), 2)
    except Exception:
        price = 0.0
    return price if price > 0 else None


def _delivery_sms_message(checker: dict, sender_name: str) -> str:
    checker_type = str(checker.get("type") or "").upper() or "RESULT CHECKER"
    body = str(checker.get("message") or "").strip()
    sender_label = sender_name or "Azico"
    return f"{checker_type} via {sender_label}\n{body}" if body else f"{checker_type} via {sender_label}"


@purchase_checker_bp.route("/purchase_checker", methods=["GET", "POST"])
def purchase_checker():
    if "user_id" not in session:
        return redirect(url_for("login.login"))

    user_id = ObjectId(session["user_id"])
    admin_id, admin_level, stage_label = _viewer_context(user_id)
    balance_doc = balances_col.find_one({"user_id": user_id})
    balance = float(balance_doc["amount"]) if balance_doc and balance_doc.get("amount") is not None else 0.0
    store_doc = _owner_store(user_id) if session.get("role") == "customer" else None
    inventory = _inventory_summary()

    if request.method == "POST" and request.form.get("action") == "save_ussd_settings":
        if not store_doc:
            flash("Create a store first before enabling Results Checker on USSD.", "warning")
            return redirect(url_for("purchase_checker.purchase_checker"))

        overall_enabled = (request.form.get("ussd_enabled") or "").strip().lower() in {"1", "true", "on", "yes", "enabled"}
        types_cfg: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []
        for checker_type in CHECKER_TYPES:
            enabled = (request.form.get(f"enabled_{checker_type}") or "").strip().lower() in {"1", "true", "on", "yes", "enabled"}
            raw_price = (request.form.get(f"selling_price_{checker_type}") or "").strip()
            try:
                selling_price = round(float(raw_price or 0), 2)
            except Exception:
                selling_price = -1
            cost_price = round(float((inventory.get(checker_type) or {}).get("cost_price") or 0), 2)
            available = int((inventory.get(checker_type) or {}).get("available") or 0)
            if enabled:
                if available <= 0:
                    errors.append(f"{checker_type.upper()} has no available inventory.")
                if selling_price <= 0:
                    errors.append(f"Enter a valid selling price for {checker_type.upper()}.")
                if cost_price > 0 and selling_price < cost_price:
                    errors.append(f"{checker_type.upper()} selling price cannot be below cost price.")
            types_cfg[checker_type] = {
                "enabled": bool(enabled),
                "price": max(0.0, selling_price if selling_price >= 0 else 0.0),
                "updated_at": datetime.utcnow(),
            }
        if errors:
            for msg in errors:
                flash(msg, "danger")
            return redirect(url_for("purchase_checker.purchase_checker"))

        stores_col.update_one(
            {"_id": store_doc["_id"], "owner_id": user_id},
            {
                "$set": {
                    "checker_product": {
                        "enabled": bool(overall_enabled),
                        "types": types_cfg,
                        "updated_at": datetime.utcnow(),
                    },
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        flash("USSD Results Checker settings saved.", "success")
        return redirect(url_for("purchase_checker.purchase_checker"))

    if request.method == "POST":
        checker_id = (request.form.get("checker_id") or "").strip()
        checker_type = normalize_checker_type(request.form.get("checker_type") or request.args.get("type"))
        store_slug = (request.form.get("store_slug") or request.args.get("store_slug") or "").strip()
        delivery_phone_raw = (request.form.get("delivery_phone") or "").strip()
        delivery_phone = normalize_ghana_sms_phone(delivery_phone_raw)
        if not delivery_phone:
            flash("Enter a valid Ghana phone number for SMS delivery.", "warning")
            return redirect(url_for("purchase_checker.purchase_checker", type=checker_type or "wassce", phone=delivery_phone_raw))

        checker = None
        if checker_id:
            checker = checker_stock_col.find_one({"_id": ObjectId(checker_id), "status": "not_sold"})
        elif checker_type:
            unsold = list(checker_stock_col.find({"type": checker_type, "status": "not_sold"}))
            if unsold:
                checker = random.choice(unsold)
        if not checker:
            flash("Checker not available or already sold.", "danger")
            return redirect(url_for("purchase_checker.purchase_checker"))

        price = _store_checker_price(store_slug, checker.get("type")) if store_slug else None
        if price is None:
            price = _checker_price(checker, admin_id, admin_level, stage_label)
        if price is None:
            flash("Checker price is not configured yet. Please contact your admin.", "warning")
            return redirect(url_for("purchase_checker.purchase_checker", type=checker.get("type")))

        if balance < price:
            flash("Insufficient balance. Please top up.", "danger")
            return redirect(url_for("deposit.deposit_page"))

        new_balance = balance - price
        balances_col.update_one(
            {"user_id": user_id},
            {
                "$set": {"amount": new_balance, "updated_at": datetime.utcnow()},
                "$setOnInsert": {"admin_id": admin_id},
            },
            upsert=True,
        )

        checker_stock_col.update_one(
            {"_id": checker["_id"]},
            {
                "$set": {
                    "status": "sold",
                    "sold_to": str(user_id),
                    "sold_at": datetime.utcnow(),
                    "delivery_phone": delivery_phone,
                }
            },
        )

        sender_name = resolve_admin_sender_name(admin_id)
        sms_status = send_sms(delivery_phone, _delivery_sms_message(checker, sender_name), sender_id=sender_name)

        checker_layers = _checker_profit_layers(checker.get("type"), admin_id, admin_level, stage_label, price)
        base_cost_ghs = checker_layers["main_base_amount"]
        admin_checker_price = checker_layers["admin_base_amount"]
        profit_amount = max(0.0, round(float(price or 0.0) - float(base_cost_ghs or 0.0), 2))

        purchase_history_col.insert_one(
            {
                "user_id": str(user_id),
                "admin_id": admin_id,
                "checker_id": str(checker["_id"]),
                "type": checker.get("type", ""),
                "amount": price,
                "base_cost_ghs": base_cost_ghs,
                "profit_amount": profit_amount,
                "message": checker.get("message", ""),
                "delivery_phone": delivery_phone,
                "sms_delivery_status": sms_status,
                "store_slug": store_slug or None,
                "purchased_at": datetime.utcnow(),
                "pricing_meta": {
                    "admin_level": admin_level,
                    "stage_label": stage_label,
                    "base_cost_ghs": base_cost_ghs,
                    "profit_amount": profit_amount,
                    "source": "store" if store_slug else "direct",
                },
            }
        )

        line = {
            "phone": delivery_phone,
            "base_amount": admin_checker_price,
            "main_base_amount": base_cost_ghs,
            "admin_base_amount": admin_checker_price,
            "selling_amount": price,
            "amount": price,
            "profit_amount": 0.0,
            "profit_percent_used": 0.0,
            "value": str(checker.get("type") or checker_type).upper(),
            "value_obj": {"type": "results_checker", "checker_type": checker.get("type")},
            "serviceId": None,
            "serviceName": f"{str(checker.get('type') or checker_type).upper()} Results Checker",
            "service_type": "RESULTS_CHECKER",
            "line_status": "completed",
            "api_status": "not_applicable",
            "api_response": {"note": "Checker fulfilled and sent by SMS.", "sms_delivery_status": sms_status},
        }
        finalized = apply_profit_split(
            normalize_profit_line(
                line,
                selling_amount=price,
                main_base_amount=base_cost_ghs,
                admin_base_amount=admin_checker_price,
            )
        )
        profit_split_totals = profit_totals([finalized])
        order_id = f"CHECKER-{str(checker['_id'])}"
        orders_col.update_one(
            {"order_id": order_id},
            {
                "$setOnInsert": {
                    "user_id": user_id,
                    "admin_id": admin_id,
                    "wallet_owner_user_id": admin_id,
                    "order_id": order_id,
                    "items": [finalized],
                    "total_amount": round(float(price), 2),
                    "charged_amount": round(float(price), 2),
                    "admin_wallet_debit_total": 0.0,
                    "agent_wallet_debit_total": round(float(price), 2),
                    "wallet_debit_status": "completed",
                    "wallet_debits": [{"user_id": user_id, "amount": round(float(price), 2), "labels": ["checker_purchase_debit"]}],
                    "profit_amount_total": profit_split_totals["profit_amount_total"],
                    "main_admin_profit_total": profit_split_totals["main_admin_profit_total"],
                    "admin_profit_total": profit_split_totals["admin_profit_total"],
                    "store_profit_total": profit_split_totals["store_profit_total"],
                    "status": "completed",
                    "paid_from": "wallet",
                    "kind": "results_checker",
                    "checker_id": checker["_id"],
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )
        _clear_dashboard_cache_safely()

        if sms_status == "sent":
            flash("Purchase successful. Your checker has been sent by SMS.", "success")
        elif sms_status == "invalid_phone":
            flash("Purchase successful, but SMS was not sent because the phone number is invalid.", "warning")
        else:
            flash("Purchase successful, but SMS delivery failed. You can still copy the checker from your purchases page.", "warning")
        return redirect(url_for("purchases.view_purchases"))

    selected_type = normalize_checker_type(request.args.get("type")) if request.args.get("type") else ""
    prefill_phone = (request.args.get("phone") or "").strip()
    checkers = []
    if selected_type:
        unsold = list(checker_stock_col.find({"type": selected_type, "status": "not_sold"}))
        if unsold:
            checker = random.choice(unsold)
            checker["display_price"] = _checker_price(checker, admin_id, admin_level, stage_label)
            checkers = [checker]

    return render_template(
        "purchase_checker.html",
        balance=balance,
        checkers=checkers,
        selected_type=selected_type,
        ussd_store=store_doc,
        ussd_checker_config=_store_checker_config(store_doc, inventory),
        ussd_checker_stats=_ussd_checker_stats(store_doc),
        customer_stage_label=stage_label,
        prefill_phone=prefill_phone,
        checker_sender_name=resolve_admin_sender_name(admin_id),
    )
