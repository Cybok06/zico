from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import db
from werkzeug.security import generate_password_hash
from datetime import datetime
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
import re
from copy import deepcopy
from tenant import resolve_admin_id_for_user_id
from service_admin_pricing import apply_admin_pricing_to_offers, normalize_admin_level
from referral_branding import signup_branding_for_referral_code
from social_boosting_pricing import SOCIAL_BOOSTING_NAME, SOCIAL_BOOSTING_SERVICE_ID

signup_bp = Blueprint("signup", __name__)
users_col = db["users"]
balances_col = db["balances"]
referrals_col = db["referrals"]
services_col = db["services"]


def normalize_phone(raw: str) -> str:
    """Normalize Ghana numbers to '0XXXXXXXXX' where possible."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) >= 12:
        return "0" + digits[-9:]
    if len(digits) == 9:
        return "0" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return digits


def normalize_username(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").strip().lower()


def _username_error(username: str) -> str | None:
    if not username:
        return "Username is required."
    if not re.fullmatch(r"[a-zA-Z0-9._-]{3,30}", username):
        return "Username must be 3-30 characters and use only letters, numbers, dot, underscore, or hyphen."
    username_regex = {"$regex": f"^{re.escape(username)}$", "$options": "i"}
    if users_col.find_one({"username": username_regex}, {"_id": 1}):
        return "Username already exists."
    return None


def _base_services_query() -> dict:
    return {
        "$and": [
            {"$or": [{"admin_id": {"$exists": False}}, {"admin_id": None}]},
            {"_id": {"$ne": SOCIAL_BOOSTING_SERVICE_ID}},
            {"name": {"$ne": SOCIAL_BOOSTING_NAME}},
        ]
    }


def _duplicate_base_services_for_admin(admin_id: ObjectId) -> int:
    if not admin_id:
        return 0
    admin_doc = users_col.find_one({"_id": admin_id}, {"admin_level": 1}) or {}
    admin_level = normalize_admin_level(admin_doc.get("admin_level"))
    base_services = list(services_col.find(_base_services_query()))
    if not base_services:
        return 0
    now = datetime.utcnow()
    to_insert = []
    for base in base_services:
        if services_col.find_one({"admin_id": admin_id, "base_service_id": base.get("_id")}):
            continue
        new_doc = deepcopy(base)
        new_doc.pop("_id", None)
        new_doc["admin_id"] = admin_id
        new_doc["base_service_id"] = base.get("_id")
        new_doc["cloned_at"] = now
        new_doc["created_at"] = now
        new_doc["updated_at"] = now
        if isinstance(new_doc.get("offers"), list):
            new_doc["offers"] = apply_admin_pricing_to_offers(
                base.get("offers") or [],
                new_doc.get("offers") or [],
                admin_level,
            )
        to_insert.append(new_doc)
    if to_insert:
        services_col.insert_many(to_insert)
    return len(to_insert)


@signup_bp.route("/signup", methods=["GET", "POST"])
def signup():
    referral_code = (request.args.get("ref") or "").strip()
    branding = signup_branding_for_referral_code(referral_code)

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        username = normalize_username(request.form.get("username") or "")
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()
        referral = ((request.form.get("referral") or "").strip()).upper()
        password = request.form.get("password") or ""
        confirm_pw = request.form.get("confirm_password") or ""
        role_to_save = "admin"
        active_referral = referral or referral_code

        def _redirect_back():
            params = {}
            if active_referral:
                params["ref"] = active_referral
            return redirect(url_for("signup.signup", **params))

        missing = []
        for val, label in [
            (first_name, "First name"),
            (last_name, "Last name"),
            (username, "Username"),
            (email, "Email"),
            (phone, "Phone"),
            (whatsapp, "WhatsApp"),
            (password, "Password"),
            (confirm_pw, "Confirm password"),
        ]:
            if not val:
                missing.append(label)
        if missing:
            flash(f"Missing required fields: {', '.join(missing)}", "danger")
            return _redirect_back()

        if password != confirm_pw:
            flash("Passwords do not match", "danger")
            return _redirect_back()

        username_err = _username_error(username)
        if username_err:
            flash(username_err, "danger")
            return _redirect_back()

        if not re.fullmatch(r"^0\d{9}$", phone):
            flash("Invalid Ghana phone number.", "danger")
            return _redirect_back()
        if not re.fullmatch(r"^0\d{9}$", whatsapp):
            flash("Invalid WhatsApp number.", "danger")
            return _redirect_back()

        admin_id = None

        ref_doc = None
        if referral:
            ref_doc = referrals_col.find_one({"ref_code": referral})
            if not ref_doc:
                flash("Invalid referral code.", "danger")
                return _redirect_back()
            if role_to_save == "agent" and not admin_id:
                admin_id = resolve_admin_id_for_user_id(users_col, ref_doc.get("user_id"))

        phone_normalized = normalize_phone(phone)

        if users_col.find_one({"email": email}):
            flash("Email already exists.", "danger")
            return _redirect_back()

        if phone_normalized and users_col.find_one({"phone_normalized": phone_normalized}):
            flash("Phone number already exists.", "danger")
            return _redirect_back()

        now = datetime.utcnow()
        new_user = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "email": email,
            "phone": phone,
            "phone_normalized": phone_normalized,
            "business_name": "",
            "whatsapp": whatsapp,
            "referral": referral or None,
            "password": generate_password_hash(password),
            "role": role_to_save,
            "admin_level": "admin",
            "admin_id": admin_id,
            "status": "pending",
            "approval_status": "pending",
            "approved_at": None,
            "approved_by": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            res = users_col.insert_one(new_user)
            user_id = res.inserted_id

            effective_admin_id = admin_id

            balances_col.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "admin_id": effective_admin_id,
                        "amount": 0.00,
                        "currency": "GHS",
                        "created_at": now,
                    },
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )

            if ref_doc:
                try:
                    referrals_col.update_one({"_id": ref_doc["_id"]}, {"$inc": {"signups": 1}})
                except Exception:
                    pass
            try:
                if role_to_save == "admin":
                    _duplicate_base_services_for_admin(user_id)
            except Exception:
                pass

        except DuplicateKeyError as e:
            try:
                kv = (e.details or {}).get("keyValue") or {}
                if "username" in kv:
                    msg = "Username already exists."
                elif "email" in kv:
                    msg = "Email already exists."
                elif "phone_normalized" in kv:
                    msg = "Phone number already exists."
                else:
                    msg = "That credential is already registered."
            except Exception:
                msg = "That credential is already registered."
            flash(msg, "danger")
            return _redirect_back()
        except Exception:
            flash("Could not complete signup. Please try again.", "danger")
            return _redirect_back()

        flash("Account created successfully! Please wait for main admin approval before logging in.", "success")
        return redirect(url_for("login.login"))

    return render_template("signup.html", referral_code=referral_code, branding=branding)


@signup_bp.route("/signup/api/referral/validate")
def api_validate_referral():
    code = ((request.args.get("code") or "").strip()).upper()
    if not code:
        return jsonify({"ok": False, "reason": "empty"})
    ok = referrals_col.find_one({"ref_code": code}, {"_id": 1}) is not None
    return jsonify({"ok": ok})


@signup_bp.route("/signup/api/username/validate")
def api_validate_username():
    username = normalize_username(request.args.get("username") or "")
    reason = _username_error(username)
    return jsonify({
        "ok": reason is None,
        "username": username,
        "reason": reason or "available",
    })
