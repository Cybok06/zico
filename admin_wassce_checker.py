from datetime import datetime

from bson.objectid import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
import re

from checker_pricing import (
    ADMIN_LEVEL_KEYS,
    CUSTOMER_STAGE_KEYS,
    VALID_CHECKER_TYPES,
    admin_stage_price,
    checker_base_cost,
    get_checker_pricing_doc,
    normalize_checker_type,
    upsert_admin_stage_prices,
    upsert_checker_base_cost,
    upsert_customer_stage_prices,
)
from db import db
from social_boosting_pricing import normalize_admin_level
from tenant import current_admin_id_from_session


admin_wassce_checker_bp = Blueprint("admin_wassce_checker", __name__)
checker_stock_col = db["wassce_checker"]
users_col = db["users"]
purchase_history_col = db["purchase_history"]


def _require_admin_access():
    if not session.get("user_id"):
        return False
    return session.get("role") in {"admin", "main_admin"}


def _is_main_admin():
    return session.get("role") == "main_admin"


def _current_admin_level() -> str:
    if _is_main_admin():
        return "main_admin"
    try:
        user_doc = users_col.find_one({"_id": ObjectId(session.get("user_id"))}, {"admin_level": 1}) or {}
    except Exception:
        user_doc = {}
    return normalize_admin_level(user_doc.get("admin_level"))


def _admin_level_label(level_key: str) -> str:
    return {
        "admin": "Admin",
        "super_admin": "Super Admin",
        "super_professional": "Professional Admin",
        "main_admin": "Main Admin",
    }.get(level_key, "Admin")


def _type_display(checker_type: str) -> str:
    return str(checker_type or "").upper()


def _safe_float(raw):
    try:
        return float(raw)
    except Exception:
        return None


def _type_price_cards(current_admin_id, current_admin_level):
    cards = []
    admin_key = str(current_admin_id) if current_admin_id else ""
    for checker_type in ("wassce", "bece"):
        pricing_doc = get_checker_pricing_doc(checker_type)
        admin_prices = dict((pricing_doc.get("admin_stage_prices") or {}))
        customer_prices = dict(((pricing_doc.get("customer_stage_prices_by_admin") or {}).get(admin_key) or {}))
        assigned_admin_price = admin_stage_price(pricing_doc, current_admin_level)
        base_cost = checker_base_cost(pricing_doc)
        cards.append(
            {
                "checker_type": checker_type,
                "checker_type_label": _type_display(checker_type),
                "admin_stage_prices": admin_prices,
                "customer_stage_prices": customer_prices,
                "assigned_admin_price": assigned_admin_price,
                "base_cost": base_cost,
                "current_admin_level_label": _admin_level_label(current_admin_level),
            }
        )
    return cards


def _checker_sales_summary(current_admin_id) -> dict:
    match = {"type": {"$in": list(VALID_CHECKER_TYPES)}}
    if not _is_main_admin() and current_admin_id:
        match["admin_id"] = current_admin_id

    pricing_by_type = {
        checker_type: checker_base_cost(get_checker_pricing_doc(checker_type))
        for checker_type in VALID_CHECKER_TYPES
    }
    summary = {
        "total_sold": 0,
        "total_charged": 0.0,
        "total_profit": 0.0,
        "by_type": {
            checker_type: {"sold": 0, "charged": 0.0, "profit": 0.0, "base_cost": pricing_by_type.get(checker_type, 0.0)}
            for checker_type in sorted(VALID_CHECKER_TYPES)
        },
    }
    try:
        cursor = purchase_history_col.find(match, {"type": 1, "amount": 1, "base_cost_ghs": 1, "profit_amount": 1})
    except Exception:
        return summary

    for row in cursor:
        checker_type = normalize_checker_type(row.get("type"))
        amount = _safe_float(row.get("amount")) or 0.0
        base_cost = _safe_float(row.get("base_cost_ghs"))
        if base_cost is None:
            base_cost = pricing_by_type.get(checker_type, 0.0)
        profit = _safe_float(row.get("profit_amount"))
        if profit is None:
            profit = max(0.0, round(amount - float(base_cost or 0.0), 2))
        profit = max(0.0, round(float(profit or 0.0), 2))

        summary["total_sold"] += 1
        summary["total_charged"] += amount
        summary["total_profit"] += profit
        bucket = summary["by_type"].setdefault(
            checker_type,
            {"sold": 0, "charged": 0.0, "profit": 0.0, "base_cost": base_cost},
        )
        bucket["sold"] += 1
        bucket["charged"] += amount
        bucket["profit"] += profit

    summary["total_charged"] = round(summary["total_charged"], 2)
    summary["total_profit"] = round(summary["total_profit"], 2)
    for bucket in summary["by_type"].values():
        bucket["charged"] = round(bucket.get("charged", 0.0), 2)
        bucket["profit"] = round(bucket.get("profit", 0.0), 2)
    return summary


def _purchase_history_view(current_admin_id):
    selected_purchase_type = normalize_checker_type(request.args.get("purchase_type")) if request.args.get("purchase_type") else ""
    search = (request.args.get("purchase_q") or "").strip()
    try:
        page = max(int(request.args.get("purchase_page", 1) or 1), 1)
    except Exception:
        page = 1
    per_page = 12

    query = {}
    if not _is_main_admin() and current_admin_id:
        query["admin_id"] = current_admin_id
    if selected_purchase_type in VALID_CHECKER_TYPES:
        query["type"] = selected_purchase_type

    if search:
        regex = {"$regex": re.escape(search), "$options": "i"}
        or_filters = [
            {"delivery_phone": regex},
            {"message": regex},
        ]
        matched_user_ids = []
        try:
            user_cursor = users_col.find(
                {
                    "$or": [
                        {"username": regex},
                        {"email": regex},
                        {"phone": regex},
                        {"first_name": regex},
                        {"last_name": regex},
                    ]
                },
                {"_id": 1},
            )
            matched_user_ids = [str(u["_id"]) for u in user_cursor]
        except Exception:
            matched_user_ids = []
        if matched_user_ids:
            or_filters.append({"user_id": {"$in": matched_user_ids}})
        query["$or"] = or_filters

    total_purchases = purchase_history_col.count_documents(query)
    total_pages = max((total_purchases + per_page - 1) // per_page, 1) if total_purchases else 0
    if total_pages and page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    purchases = list(
        purchase_history_col.find(query)
        .sort("purchased_at", -1)
        .skip(skip)
        .limit(per_page)
    )

    user_ids = []
    for p in purchases:
        uid = p.get("user_id")
        try:
            user_ids.append(ObjectId(str(uid)))
        except Exception:
            continue
    users_map = {}
    if user_ids:
        users_map = {str(u["_id"]): u for u in users_col.find({"_id": {"$in": list(set(user_ids))}})}

    for p in purchases:
        user_doc = users_map.get(str(p.get("user_id"))) or {}
        p["customer_name"] = (
            user_doc.get("username")
            or f"{(user_doc.get('first_name') or '').strip()} {(user_doc.get('last_name') or '').strip()}".strip()
            or "Unknown"
        )
        p["customer_email"] = user_doc.get("email", "N/A")
        p["customer_phone"] = user_doc.get("phone", "N/A")

    return {
        "purchases": purchases,
        "purchase_page": page,
        "purchase_per_page": per_page,
        "purchase_total_pages": total_pages,
        "purchase_total_records": total_purchases,
        "purchase_selected_type": selected_purchase_type,
        "purchase_search": search,
    }


@admin_wassce_checker_bp.route("/admin/wassce_checker", methods=["GET", "POST"])
def admin_wassce_checker():
    if not _require_admin_access():
        flash("Not authorized to access Results Checker.", "warning")
        return redirect(url_for("login.login"))

    current_admin_id = current_admin_id_from_session(session)
    current_admin_level = _current_admin_level()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add":
            if not _is_main_admin():
                flash("Only main admin can add checker stock.", "danger")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

            message = (request.form.get("message") or "").strip()
            checker_type = normalize_checker_type(request.form.get("type"))
            if not message:
                flash("Checker message is required.", "warning")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

            checker_stock_col.insert_one(
                {
                    "message": message,
                    "status": "not_sold",
                    "type": checker_type,
                    "created_at": datetime.utcnow(),
                    "created_by": current_admin_id,
                }
            )
            flash(f"{_type_display(checker_type)} checker stock added successfully.", "success")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

        if action == "update":
            if not _is_main_admin():
                flash("Only main admin can edit checker stock.", "danger")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

            checker_id = (request.form.get("checker_id") or "").strip()
            message = (request.form.get("message") or "").strip()
            checker_type = normalize_checker_type(request.form.get("type"))
            if not checker_id or not message:
                flash("Checker message is required.", "warning")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))
            try:
                checker_stock_col.update_one(
                    {"_id": ObjectId(checker_id)},
                    {"$set": {"message": message, "type": checker_type, "updated_at": datetime.utcnow()}},
                )
                flash("Checker updated successfully.", "success")
            except Exception as exc:
                flash(f"Error updating checker: {exc}", "danger")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

        if action == "update_admin_pricing":
            if not _is_main_admin():
                flash("Only main admin can set admin checker prices.", "danger")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

            checker_type = normalize_checker_type(request.form.get("type"))
            stage_prices = {}
            invalid = False
            for key in ADMIN_LEVEL_KEYS:
                raw = (request.form.get(f"{key}_price") or "").strip()
                if raw == "":
                    continue
                value = _safe_float(raw)
                if value is None or value < 0:
                    invalid = True
                    break
                stage_prices[key] = value
            if invalid:
                flash("Admin stage prices must be valid non-negative numbers.", "warning")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

            upsert_admin_stage_prices(checker_type, stage_prices)
            flash(f"{_type_display(checker_type)} admin prices updated.", "success")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

        if action == "update_base_cost":
            if not _is_main_admin():
                flash("Only main admin can set checker base costs.", "danger")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

            checker_type = normalize_checker_type(request.form.get("type"))
            value = _safe_float((request.form.get("base_cost") or "").strip())
            if value is None or value < 0:
                flash("Base cost must be a valid non-negative amount.", "warning")
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))
            upsert_checker_base_cost(checker_type, value)
            flash(f"{_type_display(checker_type)} base cost updated.", "success")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

        if action == "update_customer_pricing":
            checker_type = normalize_checker_type(request.form.get("type"))
            pricing_doc = get_checker_pricing_doc(checker_type)
            floor_price = admin_stage_price(pricing_doc, current_admin_level)
            if floor_price is None:
                flash(
                    f"Main admin must set the {_type_display(checker_type)} price for {_admin_level_label(current_admin_level)} first.",
                    "warning",
                )
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))
            stage_prices = {}
            invalid = False
            for key in CUSTOMER_STAGE_KEYS:
                raw = (request.form.get(f"{key}_price") or "").strip()
                if raw == "":
                    continue
                value = _safe_float(raw)
                if value is None or value < 0:
                    invalid = True
                    break
                if floor_price is not None and value < floor_price:
                    invalid = True
                    break
                stage_prices[key] = value
            if invalid:
                flash(
                    "Customer prices must be valid numbers and cannot be lower than your assigned checker price.",
                    "warning",
                )
                return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

            upsert_customer_stage_prices(checker_type, current_admin_id, stage_prices)
            flash(f"{_type_display(checker_type)} customer prices updated for your agents/customers.", "success")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker", type=checker_type))

    if request.args.get("delete_id"):
        if not _is_main_admin():
            flash("Only main admin can delete checker stock.", "danger")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))
        try:
            checker_stock_col.delete_one({"_id": ObjectId(request.args.get("delete_id"))})
            flash("Checker deleted successfully.", "success")
        except Exception as exc:
            flash(f"Error deleting checker: {exc}", "danger")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    if request.args.get("delete_sold") == "1":
        if not _is_main_admin():
            flash("Only main admin can delete sold checker stock.", "danger")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))
        result = checker_stock_col.delete_many({"status": "sold"})
        flash(f"Deleted {result.deleted_count} sold checkers.", "info")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    filter_status = request.args.get("status")
    filter_type = normalize_checker_type(request.args.get("type")) if request.args.get("type") else None

    query = {}
    if filter_status in {"sold", "not_sold"}:
        query["status"] = filter_status
    if filter_type in VALID_CHECKER_TYPES:
        query["type"] = filter_type

    messages = list(checker_stock_col.find(query).sort("created_at", -1))
    purchase_view = _purchase_history_view(current_admin_id)

    return render_template(
        "admin_wassce_checker.html",
        messages=messages,
        selected_status=filter_status,
        selected_type=filter_type,
        is_main_admin=_is_main_admin(),
        current_admin_level=current_admin_level,
        current_admin_level_label=_admin_level_label(current_admin_level),
        price_cards=_type_price_cards(current_admin_id, current_admin_level),
        sales_summary=_checker_sales_summary(current_admin_id),
        purchases=purchase_view["purchases"],
        purchase_page=purchase_view["purchase_page"],
        purchase_per_page=purchase_view["purchase_per_page"],
        purchase_total_pages=purchase_view["purchase_total_pages"],
        purchase_total_records=purchase_view["purchase_total_records"],
        purchase_selected_type=purchase_view["purchase_selected_type"],
        purchase_search=purchase_view["purchase_search"],
    )
