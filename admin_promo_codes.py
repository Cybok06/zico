from __future__ import annotations

import random
import string
from datetime import datetime

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from db import db


admin_promo_codes_bp = Blueprint("admin_promo_codes", __name__)

promo_codes_col = db["promo_codes"]
users_col = db["users"]


def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


def _require_main_admin() -> bool:
    return bool(session.get("user_id")) and _is_main_admin()


def _safe_oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalize_code(raw: str) -> str:
    cleaned = "".join(ch for ch in str(raw or "").upper() if ch.isalnum() or ch == "-")
    return cleaned[:40]


def _generate_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        token = "PROMO-" + "".join(random.choice(alphabet) for _ in range(8))
        if not promo_codes_col.find_one({"code": token}, {"_id": 1}):
            return token


@admin_promo_codes_bp.route("/admin/promo-codes", methods=["GET", "POST"])
def promo_codes_page():
    if not _require_main_admin():
        return redirect(url_for("login.login"))

    if request.method == "POST":
        raw_code = (request.form.get("code") or "").strip()
        code = _normalize_code(raw_code) or _generate_code()
        try:
            amount = round(float(request.form.get("amount") or 0), 2)
        except Exception:
            amount = 0.0
        now = datetime.utcnow()
        if not code:
            flash("Promo code is required.", "danger")
            return redirect(url_for("admin_promo_codes.promo_codes_page"))
        if amount <= 0:
            flash("Promo code amount must be greater than zero.", "danger")
            return redirect(url_for("admin_promo_codes.promo_codes_page"))
        if promo_codes_col.find_one({"code": code}, {"_id": 1}):
            flash("Promo code already exists.", "warning")
            return redirect(url_for("admin_promo_codes.promo_codes_page"))

        promo_codes_col.insert_one(
            {
                "code": code,
                "amount": amount,
                "status": "unused",
                "created_at": now,
                "updated_at": now,
                "created_by": _safe_oid(session.get("user_id")),
                "created_by_username": session.get("username") or "",
            }
        )
        flash("Promo code created.", "success")
        return redirect(url_for("admin_promo_codes.promo_codes_page"))

    rows = list(promo_codes_col.find({}).sort([("created_at", -1)]).limit(300))
    used_ids = [r.get("used_by_admin_id") for r in rows if isinstance(r.get("used_by_admin_id"), ObjectId)]
    used_admins = {
        u["_id"]: u
        for u in users_col.find(
            {"_id": {"$in": used_ids}},
            {"first_name": 1, "last_name": 1, "username": 1, "business_name": 1, "email": 1},
        )
    } if used_ids else {}
    for row in rows:
        row["_used_admin"] = used_admins.get(row.get("used_by_admin_id")) or {}

    return render_template("admin_promo_codes.html", rows=rows)
