from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Any

from bson import ObjectId
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from db import db
from login import (
    log_login_event,
    _normalize_phone_local,
    _phone_lookup_or,
    _send_password_reset_otp,
    _verify_password_reset_otp,
)
from maintenance import get_admin_doc, get_maintenance_status
from cloudflare_images import upload_image_to_cloudflare
from agent_code_utils import create_agent_code_for_user


admin_auth_pages_bp = Blueprint("admin_auth_pages", __name__)

users_col = db["users"]
balances_col = db["balances"]
auth_pages_col = db["auth_pages"]
services_col = db["services"]
referrals_col = db["referrals"]

AGENT_PUBLIC_BASE_URL = "https://zishop.site"


RESERVED_SLUGS = {
    "admin",
    "login",
    "signup",
    "logout",
    "uploads",
    "static",
    "healthz",
    "reset",
    "api",
    "cart",
    "orders",
    "transactions",
    "deposit",
    "checkout",
    "shares",
    "store",
    "stores",
    "settings",
    "referral",
    "complaints",
    "index",
    "admin-auth",
    "admin-auth-page",
}


def _slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _normalize_phone(raw: str) -> str:
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


def _normalize_whatsapp_link(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if "." in value and " " not in value:
        return f"https://{value}"
    digits = re.sub(r"\D", "", value)
    if not digits:
        return value
    if digits.startswith("0") and len(digits) == 10:
        digits = "233" + digits[1:]
    elif len(digits) == 9:
        digits = "233" + digits
    return f"https://wa.me/{digits}"


def _branding_payload(doc: Dict[str, Any] | None, fallback_name: str = "Agent Portal") -> Dict[str, Any]:
    d = doc or {}
    whatsapp_link = d.get("whatsapp_link") or d.get("whatsapp") or ""
    support_phone = d.get("support_phone") or d.get("phone") or ""
    support_email = d.get("support_email") or d.get("email") or ""
    return {
        "business_name": d.get("business_name") or fallback_name,
        "slug": d.get("slug") or "",
        "logo_url": d.get("logo_url") or "",
        "hero_image_url": d.get("hero_image_url") or "",
        "whatsapp_link": whatsapp_link,
        "support_phone": support_phone,
        "support_email": support_email,
        "whatsapp": whatsapp_link,
        "phone": support_phone,
        "email": support_email,
        "primary_color": d.get("primary_color") or "#0ea5e9",
        "accent_color": d.get("accent_color") or "#0f172a",
        "theme_style": d.get("theme_style") or "rose",
        "hero_title": d.get("hero_title") or "Welcome to your Agent Portal",
        "hero_text": d.get("hero_text") or "Sign in to access services, manage orders, and grow your business.",
        "background_color": d.get("background_color") or "#f8fafc",
        "background_url": d.get("background_url") or "",
    }


_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
_GB = re.compile(r"(\d+(?:\.\d+)?)\s*G(?:B|IG)?\b", re.IGNORECASE)
_MB = re.compile(r"(\d+(?:\.\d+)?)\s*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)


def _fmt_num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _format_volume(value: Any, unit: str) -> str:
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "-"
        if unit == "minutes":
            m = _MIN.search(s)
            if m:
                return f"{_fmt_num(float(m.group(1)))} mins"
        m = _GB.search(s)
        if m:
            return f"{_fmt_num(float(m.group(1)))}GB"
        m = _MB.search(s)
        if m:
            return f"{_fmt_num(float(m.group(1)))}MB"
        return s
    try:
        v = float(str(value).replace(",", "").strip())
    except Exception:
        return str(value) if value is not None else "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    if v >= 100:
        return f"{int(round(v))}MB"
    return f"{_fmt_num(v)}GB"


def _offer_value_text(of: Dict[str, Any], unit: str) -> str:
    if not isinstance(of, dict):
        return "-"
    if of.get("value_text"):
        return str(of.get("value_text")).strip()
    v = of.get("value")
    if isinstance(v, dict):
        for k in ("volume", "offer", "gb", "size", "qty"):
            if k in v and v.get(k) not in (None, ""):
                return _format_volume(v.get(k), unit)
    if isinstance(v, (int, float)) or (isinstance(v, str) and _NUM.match(v or "")):
        return _format_volume(v, unit)
    if isinstance(v, str) and v.strip():
        return v.strip()
    if of.get("label"):
        return str(of.get("label")).strip()
    return "-"


def _offer_price(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    for k in ("store_amount", "amount", "total", "price"):
        v = of.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            continue
    return None


def _available_services_for_admin(admin_id: ObjectId | None) -> List[Dict[str, Any]]:
    if not admin_id:
        return []
    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "service_category": 1,
        "description": 1,
        "image_url": 1,
        "unit": 1,
        "offers": 1,
        "store_offers": 1,
    }
    out: List[Dict[str, Any]] = []
    try:
        raw = list(services_col.find({"admin_id": admin_id}, fields))
    except Exception:
        return []

    for s in raw:
        status = (s.get("status") or "OPEN").upper()
        availability = (s.get("availability") or "AVAILABLE").upper()
        svc_type = (s.get("type") or "API").upper()
        if status != "OPEN" or availability != "AVAILABLE" or svc_type == "OFF":
            continue
        unit = (s.get("unit") or "data").strip().lower()
        out.append(
            {
                "name": (s.get("name") or "Service").strip(),
                "type": svc_type,
                "category": (s.get("service_category") or "").strip(),
                "description": (s.get("description") or "").strip(),
                "image_url": (s.get("image_url") or "").strip(),
                "availability": availability,
                "unit": unit,
                "offers": [],
            }
        )
    out.sort(key=lambda x: (x.get("name") or "").lower())
    return out


def _require_admin_user() -> ObjectId | None:
    role = (session.get("role") or "").lower()
    if role != "admin":
        return None
    user_id = session.get("user_id")
    try:
        return ObjectId(user_id)
    except Exception:
        return None


# ===============================
# Admin: Configure branding page
# ===============================
@admin_auth_pages_bp.route("/admin/auth-page", methods=["GET", "POST"])
def admin_auth_page():
    admin_oid = _require_admin_user()
    if not admin_oid:
        flash("Only sub-admins can manage branded auth pages.", "warning")
        return redirect(url_for("admin_dashboard.admin_dashboard"))

    user_doc = users_col.find_one({"_id": admin_oid}, {"business_name": 1, "username": 1, "role": 1}) or {}
    if (user_doc.get("role") or "").lower() == "main_admin":
        flash("Main admin does not use branded auth pages.", "warning")
        return redirect(url_for("admin_dashboard.admin_dashboard"))

    existing = auth_pages_col.find_one({"admin_id": admin_oid})

    if request.method == "POST":
        business_name = (request.form.get("business_name") or "").strip()
        slug_raw = (request.form.get("slug") or "").strip()
        logo_url = (request.form.get("logo_url") or "").strip()
        hero_image_url = (request.form.get("hero_image_url") or "").strip()
        primary_color = (request.form.get("primary_color") or "").strip()
        accent_color = (request.form.get("accent_color") or "").strip()
        theme_style = (request.form.get("theme_style") or "").strip()
        hero_title = (request.form.get("hero_title") or "").strip()
        hero_text = (request.form.get("hero_text") or "").strip()
        whatsapp_link = _normalize_whatsapp_link(request.form.get("whatsapp_link") or "")
        support_phone = (request.form.get("support_phone") or "").strip()
        support_email = (request.form.get("support_email") or "").strip().lower()
        background_color = (request.form.get("background_color") or "").strip()
        background_url = (request.form.get("background_url") or "").strip()

        if not business_name:
            flash("Business name is required.", "danger")
            return redirect(url_for("admin_auth_pages.admin_auth_page"))

        if not slug_raw:
            slug_raw = business_name

        slug = _slugify(slug_raw)
        if not slug:
            flash("Invalid slug. Use letters or numbers only.", "danger")
            return redirect(url_for("admin_auth_pages.admin_auth_page"))

        if slug in RESERVED_SLUGS:
            flash("That slug is reserved. Pick a different one.", "danger")
            return redirect(url_for("admin_auth_pages.admin_auth_page"))

        other = auth_pages_col.find_one({"slug": slug, "admin_id": {"$ne": admin_oid}}, {"_id": 1})
        if other:
            flash("That slug is already in use by another admin.", "danger")
            return redirect(url_for("admin_auth_pages.admin_auth_page"))

        if support_email and not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", support_email):
            flash("Enter a valid support email address.", "danger")
            return redirect(url_for("admin_auth_pages.admin_auth_page"))

        now = datetime.utcnow()
        payload = {
            "admin_id": admin_oid,
            "business_name": business_name,
            "slug": slug,
            "logo_url": logo_url,
            "hero_image_url": hero_image_url,
            "whatsapp_link": whatsapp_link,
            "support_phone": support_phone,
            "support_email": support_email,
            "whatsapp": whatsapp_link,
            "phone": support_phone,
            "email": support_email,
            "primary_color": primary_color or "#0ea5e9",
            "accent_color": accent_color or "#0f172a",
            "theme_style": theme_style or "rose",
            "hero_title": hero_title or "Welcome to your Agent Portal",
            "hero_text": hero_text or "Sign in to access services, manage orders, and grow your business.",
            "background_color": background_color or "#f8fafc",
            "background_url": background_url,
            "updated_at": now,
        }

        auth_pages_col.update_one(
            {"admin_id": admin_oid},
            {"$set": payload, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        flash("Branded login/signup page saved.", "success")
        return redirect(url_for("admin_auth_pages.admin_auth_page"))

    branding = _branding_payload(existing, fallback_name=user_doc.get("business_name") or user_doc.get("username") or "Agent Portal")
    preview_login = f"{AGENT_PUBLIC_BASE_URL}/{branding['slug']}/login" if branding.get("slug") else None
    preview_signup = f"{AGENT_PUBLIC_BASE_URL}/{branding['slug']}/signup" if branding.get("slug") else None
    preview_landing = f"{AGENT_PUBLIC_BASE_URL}/{branding['slug']}" if branding.get("slug") else None
    return render_template(
        "admin_auth_pages.html",
        branding=branding,
        preview_login=preview_login,
        preview_signup=preview_signup,
        preview_landing=preview_landing,
    )


# ===============================
# Admin: Cloudflare image upload
# ===============================
@admin_auth_pages_bp.route("/api/admin-auth/upload_image", methods=["POST"])
def admin_auth_upload_image():
    admin_oid = _require_admin_user()
    if not admin_oid:
        return jsonify({"success": False, "error": "Admin login required"}), 401

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No file part in request"}), 400

    image = request.files["image"]
    ok, payload, code = upload_image_to_cloudflare(
        image,
        owner_id=str(admin_oid),
        module="auth_pages",
        variant=request.args.get("variant"),
        content_length=request.content_length,
    )
    if not ok:
        return jsonify({"success": False, **payload}), code

    return jsonify({"success": True, **payload})


# ===============================
# Public: Branded login / signup
# ===============================
@admin_auth_pages_bp.route("/<slug>/login", methods=["GET", "POST"])
def branded_login(slug: str):
    slug = _slugify(slug)
    doc = auth_pages_col.find_one({"slug": slug})
    if not doc:
        abort(404)

    admin_doc = users_col.find_one({"_id": doc.get("admin_id")}, {"role": 1})
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        abort(404)

    branding = _branding_payload(doc)
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = users_col.find_one({"username": username, "role": "agent", "admin_id": doc.get("admin_id")})
        if (not user) or (not check_password_hash(user.get("password", ""), password)):
            log_login_event(user or {"username": username, "role": "agent"}, success=False, reason="invalid_credentials")
            flash("Invalid username or password.", "danger")
            return render_template("branded_login.html", branding=branding, slug=slug)

        status = (user.get("status") or "active").lower()
        approval_status = (user.get("approval_status") or "").strip().lower()
        if status == "pending" or approval_status == "pending":
            log_login_event(user, success=False, reason="pending_approval")
            flash("Your agent account is pending admin approval.", "warning")
            return render_template("branded_login.html", branding=branding, slug=slug)

        if status == "blocked":
            log_login_event(user, success=False, reason="blocked")
            flash("Your account is blocked. Please contact your admin.", "danger")
            return render_template("branded_login.html", branding=branding, slug=slug)

        # Maintenance gate (block agents if admin overdue)
        admin_doc = get_admin_doc(doc.get("admin_id"))
        m_status = get_maintenance_status(admin_doc) if admin_doc else {}
        if m_status.get("is_overdue"):
            log_login_event(user, success=False, reason="maintenance_overdue")
            flash("Admin account is suspended due to unpaid maintenance fee.", "danger")
            return render_template("branded_login.html", branding=branding, slug=slug)

        session.clear()
        session["user_id"] = str(user["_id"])
        session["username"] = user["username"]
        session["role"] = user.get("role", "customer")
        session["admin_id"] = str(doc.get("admin_id"))
        session.permanent = True
        session.modified = True

        log_login_event(user, success=True)
        return redirect(url_for("customer_dashboard.customer_dashboard"))

    return render_template("branded_login.html", branding=branding, slug=slug)


@admin_auth_pages_bp.route("/<slug>/forgot-password", methods=["GET", "POST"])
def branded_forgot_password(slug: str):
    slug = _slugify(slug)
    doc = auth_pages_col.find_one({"slug": slug})
    if not doc:
        abort(404)

    admin_doc = users_col.find_one({"_id": doc.get("admin_id")}, {"role": 1})
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        abort(404)

    branding = _branding_payload(doc)
    step = "request"
    ctx = session.get("pw_reset_branded") or {}
    if ctx.get("slug") == slug and ctx.get("user_id") and ctx.get("phone"):
        step = "verify"

    if request.method == "POST":
        step = (request.form.get("step") or "request").lower()

        if step == "request":
            phone_raw = (request.form.get("phone") or "").strip()
            phone_norm = _normalize_phone_local(phone_raw)
            if not phone_norm or not re.fullmatch(r"^0\d{9}$", phone_norm):
                flash("Enter a valid Ghana phone number (0XXXXXXXXX).", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="request")

            user = users_col.find_one(
                {
                    "admin_id": doc.get("admin_id"),
                    "role": "agent",
                    "$or": _phone_lookup_or(phone_norm),
                },
                {"_id": 1, "admin_id": 1, "role": 1},
            )
            if not user:
                flash("No agent found for that phone number.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="request")

            ok, status = _send_password_reset_otp(
                user["_id"],
                phone_raw,
                phone_norm,
                admin_id=user.get("admin_id"),
                recipient_role=user.get("role"),
                brand_label=branding.get("business_name") or "Agent Portal",
            )
            if not ok:
                if status == "invalid_phone":
                    flash("Invalid phone number format.", "danger")
                else:
                    flash("Failed to send OTP. Please try again.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="request")

            session["pw_reset_branded"] = {"user_id": str(user["_id"]), "phone": phone_norm, "slug": slug}
            flash("OTP sent. Enter the code to reset your password.", "success")
            return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="verify")

        if step == "verify":
            otp = (request.form.get("otp") or "").strip()
            new_pw = request.form.get("password") or ""
            confirm_pw = request.form.get("confirm_password") or ""
            ctx = session.get("pw_reset_branded") or {}
            if ctx.get("slug") != slug or not ctx.get("user_id") or not ctx.get("phone"):
                flash("Reset session expired. Please request a new code.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="request")

            if not otp or not re.fullmatch(r"^\d{6}$", otp):
                flash("Enter a valid 6-digit OTP.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="verify")

            if new_pw != confirm_pw:
                flash("Passwords do not match.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="verify")

            ok, reason = _verify_password_reset_otp(ObjectId(ctx["user_id"]), ctx["phone"], otp)
            if not ok:
                flash("Invalid or expired OTP. Request a new code.", "danger")
                return render_template("branded_forgot_password.html", branding=branding, slug=slug, step="verify")

            users_col.update_one(
                {"_id": ObjectId(ctx["user_id"])},
                {"$set": {"password": generate_password_hash(new_pw), "updated_at": datetime.utcnow()}},
            )
            session.pop("pw_reset_branded", None)
            flash("Password updated. Please login.", "success")
            return redirect(url_for("admin_auth_pages.branded_login", slug=slug))

    return render_template("branded_forgot_password.html", branding=branding, slug=slug, step=step)


@admin_auth_pages_bp.route("/<slug>/signup", methods=["GET", "POST"])
def branded_signup(slug: str):
    slug = _slugify(slug)
    doc = auth_pages_col.find_one({"slug": slug})
    if not doc:
        abort(404)

    admin_doc = users_col.find_one({"_id": doc.get("admin_id")}, {"role": 1})
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        abort(404)

    branding = _branding_payload(doc)
    referral_code = ((request.args.get("ref") or request.form.get("referral") or "").strip()).upper()

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        username = re.sub(r"\s+", "", request.form.get("username") or "").strip().lower()
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()
        referral = ((request.form.get("referral") or "").strip()).upper()
        password = request.form.get("password") or ""
        confirm_pw = request.form.get("confirm_password") or ""
        active_referral = referral or referral_code

        def _render_signup():
            return render_template(
                "branded_signup.html",
                branding=branding,
                slug=slug,
                referral_code=active_referral,
            )

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
            return _render_signup()

        if password != confirm_pw:
            flash("Passwords do not match.", "danger")
            return _render_signup()

        if not re.fullmatch(r"^[a-zA-Z0-9._-]{3,}$", username):
            flash("Invalid username format.", "danger")
            return _render_signup()
        if not re.fullmatch(r"^0\d{9}$", phone):
            flash("Invalid Ghana phone number.", "danger")
            return _render_signup()
        if not re.fullmatch(r"^0\d{9}$", whatsapp):
            flash("Invalid WhatsApp number.", "danger")
            return _render_signup()

        phone_normalized = _normalize_phone(phone)

        if users_col.find_one({"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}):
            flash("Username already exists.", "danger")
            return _render_signup()
        if users_col.find_one({"email": email}):
            flash("Email already exists.", "danger")
            return _render_signup()
        if phone_normalized and users_col.find_one({"phone_normalized": phone_normalized}):
            flash("Phone number already exists.", "danger")
            return _render_signup()

        ref_doc = None
        if active_referral:
            ref_doc = referrals_col.find_one({"ref_code": active_referral})
            if not ref_doc:
                flash("Invalid referral code.", "danger")
                return _render_signup()
            if ref_doc.get("admin_id") != doc.get("admin_id"):
                flash("Referral code does not belong to this admin signup page.", "danger")
                return _render_signup()

        now = datetime.utcnow()
        is_auto_approved = bool(ref_doc)
        approval_status = "approved" if is_auto_approved else "pending"
        account_status = "active" if is_auto_approved else "pending"
        approved_at = now if is_auto_approved else None
        approved_by = doc.get("admin_id") if is_auto_approved else None

        new_user = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "email": email,
            "phone": phone,
            "phone_normalized": phone_normalized,
            "business_name": None,
            "whatsapp": whatsapp,
            "referral": active_referral or None,
            "password": generate_password_hash(password),
            "role": "agent",
            "admin_id": doc.get("admin_id"),
            "status": account_status,
            "approval_status": approval_status,
            "approved_at": approved_at,
            "approved_by": approved_by,
            "created_at": now,
            "updated_at": now,
        }

        try:
            res = users_col.insert_one(new_user)
            user_id = res.inserted_id
            balances_col.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "admin_id": doc.get("admin_id"),
                        "amount": 0.00,
                        "currency": "GHS",
                        "created_at": now,
                    },
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )
            create_agent_code_for_user(user_id, doc.get("admin_id"), now)
            if ref_doc:
                try:
                    referrals_col.update_one({"_id": ref_doc["_id"]}, {"$inc": {"signups": 1}})
                except Exception:
                    pass
        except Exception:
            try:
                if "user_id" in locals():
                    balances_col.delete_one({"user_id": user_id})
                    db["agent_codes"].delete_one({"user_id": user_id})
                    users_col.delete_one({"_id": user_id})
            except Exception:
                pass
            flash("Could not complete signup. Please try again.", "danger")
            return _render_signup()

        if is_auto_approved:
            flash("Agent account created successfully. Your referral was applied and your account is ready to use.", "success")
        else:
            flash("Agent account created successfully. Please wait for admin approval before logging in.", "success")
        return redirect(url_for("admin_auth_pages.branded_login", slug=slug))

    return render_template("branded_signup.html", branding=branding, slug=slug, referral_code=referral_code)


# ===============================
# Public: Branded landing page
# ===============================
@admin_auth_pages_bp.route("/<slug>")
def branded_landing(slug: str):
    slug = _slugify(slug)
    doc = auth_pages_col.find_one({"slug": slug})
    if not doc:
        abort(404)

    admin_doc = users_col.find_one({"_id": doc.get("admin_id")}, {"role": 1})
    if not admin_doc or (admin_doc.get("role") or "").lower() == "main_admin":
        abort(404)

    branding = _branding_payload(doc)
    available_services = _available_services_for_admin(doc.get("admin_id"))
    return render_template(
        "branded_landing.html",
        branding=branding,
        slug=slug,
        available_services=available_services,
    )
