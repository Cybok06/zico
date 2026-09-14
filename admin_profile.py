from __future__ import annotations

from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from tenant import current_admin_id_from_session
from maintenance import (
    get_maintenance_paystack_public_key,
    get_maintenance_status,
    get_payment_history,
    redeem_maintenance_promo_code,
)

admin_profile_bp = Blueprint("admin_profile", __name__)
users_col = db["users"]


def _require_admin():
    if session.get("admin_logged_in") or (session.get("role") in {"admin", "main_admin"}):
        return True
    return False


@admin_profile_bp.route("/admin/profile", methods=["GET", "POST"])
def admin_profile():
    if not _require_admin():
        return redirect(url_for("login.login"))

    active_tab = (request.args.get("tab") or "profile").strip().lower()
    if active_tab not in {"profile", "billing"}:
        active_tab = "profile"
    role = (session.get("role") or "").strip().lower()
    is_main_admin = role == "main_admin"

    user_id = session.get("user_id")
    try:
        oid = ObjectId(user_id)
    except Exception:
        return redirect(url_for("login.login"))

    user = users_col.find_one({"_id": oid}) or {}
    admin_oid = current_admin_id_from_session(session) or oid

    if request.method == "POST":
        form_type = (request.form.get("form_type") or "password").strip().lower()

        if form_type == "promo_code":
            if is_main_admin:
                flash("Main admin does not need promo code subscriptions.", "warning")
                return redirect(url_for("admin_profile.admin_profile", tab="billing"))
            promo_code = (request.form.get("promo_code") or "").strip()
            if not promo_code:
                flash("Enter a promo code.", "danger")
                return redirect(url_for("admin_profile.admin_profile", tab="billing"))
            try:
                redeem_maintenance_promo_code(user, promo_code, redeemed_by=session.get("user_id"))
                session.pop("maintenance_locked", None)
                flash("Promo code applied successfully. Subscription activated.", "success")
            except Exception as exc:
                flash(str(exc) or "Unable to redeem promo code.", "danger")
            return redirect(url_for("admin_profile.admin_profile", tab="billing"))

        if form_type in {"paystack_store", "paystack_deposit"}:
            flash("Paystack keys are now managed by the main admin in Settings.", "warning")
            return redirect(url_for("settings.manage_api", tab="paystack" if is_main_admin else "payments"))

        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm_pw = request.form.get("confirm_password") or ""

        if not current_pw or not new_pw or not confirm_pw:
            flash("All password fields are required.", "danger")
            return redirect(url_for("admin_profile.admin_profile"))

        if not check_password_hash(user.get("password", ""), current_pw):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("admin_profile.admin_profile"))

        if new_pw != confirm_pw:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("admin_profile.admin_profile"))

        if len(new_pw) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return redirect(url_for("admin_profile.admin_profile"))

        users_col.update_one(
            {"_id": oid},
            {"$set": {"password": generate_password_hash(new_pw), "updated_at": datetime.utcnow()}},
        )
        flash("Password updated successfully.", "success")
        return redirect(url_for("admin_profile.admin_profile"))

    maintenance_status = get_maintenance_status(user or {})
    maintenance_history = []
    try:
        maintenance_history = get_payment_history(oid, limit=25)
    except Exception:
        maintenance_history = []

    return render_template(
        "admin_profile.html",
        user=user,
        active_tab=active_tab,
        is_main_admin=is_main_admin,
        maintenance_status=maintenance_status,
        maintenance_history=maintenance_history,
        paystack_pk=get_maintenance_paystack_public_key(),
    )
