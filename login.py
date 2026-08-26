# login.py
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from db import db
from bson import ObjectId
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta
import json
import re
import requests
import urllib3
import secrets
from tenant import resolve_admin_id_from_user_doc
from maintenance import get_admin_doc, get_maintenance_status
from sms_sender import get_site_sms_sender_name, resolve_system_sender_id, send_bulk_sms

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

login_bp = Blueprint("login", __name__)
users_col = db["users"]
login_logs_col = db["login_logs"]
auth_pages_col = db["auth_pages"]
reset_otps_col = db["password_reset_otps"]

# --- Configs for IP lookup (best-effort; won’t block login) ---
ENABLE_IP_LOOKUP = True
IP_LOOKUP_TIMEOUT = 4.0  # seconds
VERIFY_SSL = False       # avoids custom CA issues in your environment

OTP_TTL_MIN = 10
PASSWORD_RESET_SENDER_ID = "Zishop"


# ---------------------------
# Helpers
# ---------------------------

_PRIVATE_NETS = (
    re.compile(r"^(127\.0\.0\.1)$"),
    re.compile(r"^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^192\.168\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$"),
    re.compile(r"^::1$"),
)

_otp_indexes_ready = False

def _ensure_otp_indexes() -> None:
    global _otp_indexes_ready
    if _otp_indexes_ready:
        return
    try:
        reset_otps_col.create_index([("user_id", 1)], background=True)
        reset_otps_col.create_index([("phone_normalized", 1)], background=True)
        reset_otps_col.create_index("expires_at", expireAfterSeconds=0, background=True)
    except Exception:
        pass
    _otp_indexes_ready = True


def _normalize_phone_local(raw: str) -> str:
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


def _normalize_phone_233(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        return "233" + digits[1:]
    if digits.startswith("233") and len(digits) == 12:
        return digits
    return ""


def _phone_lookup_or(phone_normalized: str) -> list[dict]:
    """Match current and older Ghana phone formats stored on user documents."""
    phone_normalized = _normalize_phone_local(phone_normalized)
    if not phone_normalized:
        return []

    digits_233 = _normalize_phone_233(phone_normalized)
    candidates = {phone_normalized}
    if digits_233:
        candidates.update({digits_233, f"+{digits_233}"})

    return [
        {"phone_normalized": {"$in": list(candidates)}},
        {"phone": {"$in": list(candidates)}},
    ]


def _mask_sms_phone(msisdn: str) -> str:
    raw = str(msisdn or "")
    if len(raw) <= 5:
        return "***"
    return raw[:3] + "***" + raw[-2:]


def _reset_sms_log(event: str, **payload) -> None:
    try:
        print(json.dumps({"evt": event, **payload}, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception:
        print(f"[PASSWORD_RESET_SMS_LOG] {event} {payload}")


def _send_sms(
    msisdn: str,
    message: str,
    admin_id: ObjectId | None = None,
    recipient_role: str | None = None,
    recipient_user_id: ObjectId | None = None,
) -> str:
    sender_id = resolve_system_sender_id(
        admin_id=admin_id,
        recipient_role=recipient_role,
        recipient_user_id=recipient_user_id,
    )
    if str(recipient_role or "").strip().lower() in {"admin", "main_admin"}:
        sender_id = PASSWORD_RESET_SENDER_ID
    result = send_bulk_sms([msisdn], message, sender_id=sender_id)
    _reset_sms_log(
        "password_reset_sms_result",
        status=result.get("status"),
        provider=result.get("provider"),
        http_status=result.get("http_status"),
        provider_status=result.get("provider_status"),
        provider_message=result.get("provider_message"),
        api_key_source=result.get("api_key_source"),
        sender=sender_id,
        recipient=_mask_sms_phone(msisdn),
        recipient_role=recipient_role,
        user_id=str(recipient_user_id or ""),
    )
    if result.get("status") != "sent":
        fallback_sender = get_site_sms_sender_name()
        reset_role = str(recipient_role or "").strip().lower()
        if reset_role in {"admin", "main_admin"}:
            fallback_sender = ""
        if fallback_sender and fallback_sender != sender_id:
            retry_result = send_bulk_sms([msisdn], message, sender_id=fallback_sender)
            _reset_sms_log(
                "password_reset_sms_retry_result",
                status=retry_result.get("status"),
                provider=retry_result.get("provider"),
                http_status=retry_result.get("http_status"),
                provider_status=retry_result.get("provider_status"),
                provider_message=retry_result.get("provider_message"),
                api_key_source=retry_result.get("api_key_source"),
                original_sender=sender_id,
                sender=fallback_sender,
                recipient=_mask_sms_phone(msisdn),
                recipient_role=recipient_role,
                user_id=str(recipient_user_id or ""),
            )
            result = retry_result
    if result.get("status") == "sent":
        return "sent"
    if result.get("status") == "failed":
        return "failed"
    return "error"


def _generate_otp_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _store_password_reset_otp(user_id: ObjectId, phone_normalized: str, code: str, admin_id: ObjectId | None = None) -> None:
    _ensure_otp_indexes()
    reset_otps_col.delete_many(
        {"user_id": user_id, "phone_normalized": phone_normalized, "purpose": "password_reset"}
    )
    now = datetime.utcnow()
    reset_otps_col.insert_one(
        {
            "user_id": user_id,
            "admin_id": admin_id,
            "phone_normalized": phone_normalized,
            "purpose": "password_reset",
            "code_hash": generate_password_hash(code),
            "created_at": now,
            "expires_at": now + timedelta(minutes=OTP_TTL_MIN),
        }
    )


def _verify_password_reset_otp(user_id: ObjectId, phone_normalized: str, code: str) -> tuple[bool, str]:
    _ensure_otp_indexes()
    doc = reset_otps_col.find_one(
        {"user_id": user_id, "phone_normalized": phone_normalized, "purpose": "password_reset"},
        sort=[("created_at", -1)],
    )
    if not doc:
        return False, "missing"
    if doc.get("expires_at") and doc["expires_at"] < datetime.utcnow():
        return False, "expired"
    if not check_password_hash(doc.get("code_hash", ""), code or ""):
        return False, "invalid"
    reset_otps_col.delete_many(
        {"user_id": user_id, "phone_normalized": phone_normalized, "purpose": "password_reset"}
    )
    return True, "ok"


def _send_password_reset_otp(
    user_id: ObjectId,
    phone_raw: str,
    phone_normalized: str,
    admin_id: ObjectId | None = None,
    recipient_role: str | None = None,
    brand_label: str = "AZICO",
) -> tuple[bool, str]:
    msisdn = _normalize_phone_233(phone_raw)
    if not msisdn:
        return False, "invalid_phone"
    code = _generate_otp_code()
    _store_password_reset_otp(user_id, phone_normalized, code, admin_id=admin_id)
    msg = f"{brand_label} reset code: {code}. Expires in {OTP_TTL_MIN} minutes."
    status = _send_sms(
        msisdn,
        msg,
        admin_id=admin_id,
        recipient_role=recipient_role,
        recipient_user_id=user_id,
    )
    if status != "sent":
        reset_otps_col.delete_many(
            {"user_id": user_id, "phone_normalized": phone_normalized, "purpose": "password_reset"}
        )
        return False, status
    return True, "sent"
def _is_private_ip(ip: str) -> bool:
    ip = (ip or "").strip()
    if not ip:
        return True
    return any(p.match(ip) for p in _PRIVATE_NETS)

def get_client_ip() -> str:
    """
    Try to get the real client IP honoring proxies.
    """
    xfwd = (request.headers.get("X-Forwarded-For") or "").strip()
    if xfwd:
        # X-Forwarded-For: client, proxy1, proxy2
        first = xfwd.split(",")[0].strip()
        if first:
            return first
    xreal = (request.headers.get("X-Real-IP") or "").strip()
    if xreal:
        return xreal
    return request.remote_addr or ""

def build_device_info() -> dict:
    """
    Parse basic device info from Werkzeug's user_agent and headers.
    Kept simple (no external ua parser).
    """
    ua = request.user_agent
    ua_str = request.headers.get("User-Agent", "")
    s = ua_str.lower()

    is_tablet = ("ipad" in s) or ("tablet" in s)
    is_mobile = ("mobile" in s or "android" in s or "iphone" in s) and not is_tablet
    is_pc = not (is_mobile or is_tablet)

    return {
        "ua_string": ua_str,
        "browser": ua.browser or None,
        "version": ua.version or None,
        "platform": ua.platform or None,  # e.g., 'linux', 'macos', 'windows'
        "language": getattr(ua, "language", None),
        "accepted_languages": [l[0] for l in request.accept_languages] if request.accept_languages else [],
        "is_mobile": is_mobile,
        "is_tablet": is_tablet,
        "is_pc": is_pc,
        "device_label": "mobile" if is_mobile else ("tablet" if is_tablet else "desktop"),
    }

def lookup_ip_location(ip: str) -> dict:
    """
    Best-effort geolocation. Uses public endpoints with short timeouts.
    Skips private/local IPs. Never raises; returns a dict.
    """
    base = {
        "ip": ip,
        "is_private": _is_private_ip(ip),
        "source": None,
        "city": None,
        "region": None,
        "country": None,
        "country_name": None,
        "latitude": None,
        "longitude": None,
        "timezone": None,
    }

    if not ENABLE_IP_LOOKUP or base["is_private"] or not ip:
        return base

    # Try ipapi.co first
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=IP_LOOKUP_TIMEOUT, verify=VERIFY_SSL)
        if r.ok:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j:
                base.update({
                    "source": "ipapi.co",
                    "city": j.get("city"),
                    "region": j.get("region"),
                    "country": j.get("country"),
                    "country_name": j.get("country_name"),
                    "latitude": j.get("latitude"),
                    "longitude": j.get("longitude"),
                    "timezone": j.get("timezone"),
                })
                return base
    except Exception:
        pass

    # Fallback ipinfo.io
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=IP_LOOKUP_TIMEOUT, verify=VERIFY_SSL)
        if r.ok:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j:
                loc = j.get("loc", "")  # "lat,lon"
                lat, lon = (None, None)
                if isinstance(loc, str) and "," in loc:
                    try:
                        lat_s, lon_s = loc.split(",", 1)
                        lat = float(lat_s)
                        lon = float(lon_s)
                    except Exception:
                        pass
                base.update({
                    "source": "ipinfo.io",
                    "city": j.get("city"),
                    "region": j.get("region"),
                    "country": j.get("country"),
                    "country_name": j.get("country"),  # ipinfo lacks full name on free tier
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": j.get("timezone"),
                })
                return base
    except Exception:
        pass

    return base


def log_login_event(user: dict, success: bool, reason: str = "") -> None:
    """
    Inserts a login log document. Never raises to caller.
    """
    try:
        ip = get_client_ip()
        log_doc = {
            "user_id": user.get("_id") if user else None,
            "admin_id": user.get("admin_id") if user else None,
            "username": user.get("username") if user else None,
            "role": (user or {}).get("role", "customer"),
            "success": bool(success),
            "reason": reason or None,
            "ip": ip,
            "forwarded_for": request.headers.get("X-Forwarded-For"),
            "x_real_ip": request.headers.get("X-Real-IP"),
            "user_agent": request.headers.get("User-Agent"),
            "device": build_device_info(),
            "location": lookup_ip_location(ip),
            "created_at": datetime.utcnow(),
        }
        login_logs_col.insert_one(log_doc)
    except Exception:
        # Swallow all errors; logging must not break login flow.
        pass


# ---------------------------
# Keep sessions permanent while logged in
# ---------------------------

@login_bp.before_app_request
def _keep_permanent_session():
    """
    Runs before every request (any blueprint).
    If a user is logged in, ensure the session remains 'permanent'
    so the cookie keeps its expiration (set by PERMANENT_SESSION_LIFETIME).
    """
    if session.get("user_id"):
        session.permanent = True


@login_bp.before_app_request
def _maintenance_gate():
    """
    Enforce maintenance billing rules:
    - If admin is overdue, lock admin to Billing only.
    - If admin is overdue, block agent/customer access (force logout).
    """
    if not session.get("user_id"):
        return None

    role = (session.get("role") or "").strip().lower()
    admin_id = session.get("admin_id") or session.get("user_id")
    admin_doc = get_admin_doc(admin_id)
    status = get_maintenance_status(admin_doc)

    if not status or status.get("exempt"):
        session.pop("maintenance_locked", None)
        return None

    is_overdue = bool(status.get("is_overdue"))
    if not is_overdue:
        session.pop("maintenance_locked", None)
        return None

    # Admins: lock to billing only
    if role in {"admin", "main_admin"}:
        session["maintenance_locked"] = True
        allowed = {
            "login.login",
            "login.logout",
            "admin_profile.admin_profile",
            "maintenance.verify_maintenance_payment",
            "moolre_payments.create_moolre_payment",
            "moolre_payments.moolre_redirect",
            "moolre_payments.moolre_status",
            "static",
            "uploaded_file",
        }
        if request.endpoint in allowed:
            return None
        return redirect(url_for("admin_profile.admin_profile", tab="billing"))

    # Agents/customers: block access entirely
    session.clear()
    flash("Your admin account is suspended due to unpaid maintenance fee.", "danger")
    return redirect(url_for("login.login"))


# ---------------------------
# Routes
# ---------------------------

@login_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        # Find by username only
        user = users_col.find_one({"username": username})

        # Invalid credentials (user missing or password mismatch)
        if (not user) or (not check_password_hash(user.get("password", ""), password)):
            log_login_event(user or {"username": username, "role": "unknown"}, success=False, reason="invalid_credentials")
            flash("❌ Invalid username or password", "danger")
            return render_template("login.html")

        # Blocked status check AFTER password is correct
        role = (user.get("role") or "customer").strip().lower()
        status = (user.get("status") or "active").lower()
        approval_status = (user.get("approval_status") or "").strip().lower()
        if status == "pending" or approval_status == "pending":
            log_login_event(user, success=False, reason="pending_approval")
            if role == "agent":
                flash("Your agent account is pending admin approval.", "warning")
            elif role == "admin":
                flash("Your admin account is pending main admin approval.", "warning")
            else:
                flash("Your account is pending approval.", "warning")
            return render_template("login.html")

        if status == "blocked":
            # Log a blocked login attempt and refuse to create a session
            log_login_event(user, success=False, reason="blocked")
            flash("🚫 Your account is blocked. Please contact support.", "danger")
            return render_template("login.html")

        if status == "deleted" or user.get("deleted") is True:
            log_login_event(user, success=False, reason="deleted")
            flash("This account is no longer active. Please contact support.", "danger")
            return render_template("login.html")

        
        # Maintenance status check (admin + tenant)
        admin_oid = resolve_admin_id_from_user_doc(user)
        admin_doc = get_admin_doc(admin_oid) if admin_oid else None
        m_status = get_maintenance_status(admin_doc) if admin_doc else {}
        is_overdue = bool(m_status.get("is_overdue"))

        # If tenant is overdue, block agents/customers entirely
        if role not in {"admin", "main_admin"} and is_overdue:
            log_login_event(user, success=False, reason="maintenance_overdue")
            flash("Your admin account is suspended due to unpaid maintenance fee.", "danger")
            return render_template("login.html")

        # Successful auth (active or missing status treated as active)
        session.clear()
        session["user_id"] = str(user["_id"])
        session["username"] = user["username"]
        session["role"] = role or "customer"
        if session["role"] in {"admin", "main_admin"}:
            session["admin_level"] = user.get("admin_level") or ("main_admin" if session["role"] == "main_admin" else "admin")
        resolved_admin_id = resolve_admin_id_from_user_doc(user)
        if resolved_admin_id:
            session["admin_id"] = str(resolved_admin_id)
        session.permanent = True  # <- critical: sets cookie expiration
        session.modified = True

        if session["role"] in {"admin", "main_admin"} and is_overdue:
            session["maintenance_locked"] = True

        # Log success before redirect
        log_login_event(user, success=True)


        # Role-based redirect
        if session["role"] in {"admin", "main_admin"}:
            session["admin_logged_in"] = True
            if is_overdue:
                flash("Maintenance fee overdue. Please pay to restore full access.", "warning")
                return redirect(url_for("admin_profile.admin_profile", tab="billing"))
            if m_status.get("is_due"):
                flash("Maintenance fee is due. Please pay within 5 days to avoid suspension.", "warning")
            elif m_status.get("due_soon"):
                flash("Maintenance fee is due soon.", "info")
            return redirect(url_for("admin_dashboard.admin_dashboard"))
        else:
            session["customer_logged_in"] = True
            return redirect(url_for("customer_dashboard.customer_dashboard"))


    return render_template("login.html")


@login_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    step = "request"
    ctx = session.get("pw_reset_main") or {}
    if ctx.get("user_id") and ctx.get("phone"):
        step = "verify"

    if request.method == "POST":
        step = (request.form.get("step") or "request").lower()

        if step == "request":
            phone_raw = (request.form.get("phone") or "").strip()
            phone_norm = _normalize_phone_local(phone_raw)
            if not phone_norm or not re.fullmatch(r"^0\d{9}$", phone_norm):
                flash("Enter a valid Ghana phone number (0XXXXXXXXX).", "danger")
                return render_template("forgot_password.html", step="request")

            user = users_col.find_one(
                {
                    "role": {"$in": ["admin", "main_admin"]},
                    "$or": _phone_lookup_or(phone_norm),
                },
                {"_id": 1, "admin_id": 1, "role": 1},
            )
            if not user:
                flash("No admin account found for that phone number.", "danger")
                return render_template("forgot_password.html", step="request")

            ok, status = _send_password_reset_otp(
                user["_id"],
                phone_raw,
                phone_norm,
                admin_id=user.get("admin_id"),
                recipient_role=user.get("role"),
                brand_label="AZICO",
            )
            if not ok:
                if status == "invalid_phone":
                    flash("Invalid phone number format.", "danger")
                else:
                    flash("Failed to send OTP. Please try again.", "danger")
                return render_template("forgot_password.html", step="request")

            session["pw_reset_main"] = {"user_id": str(user["_id"]), "phone": phone_norm}
            flash("OTP sent. Enter the code to reset your password.", "success")
            return render_template("forgot_password.html", step="verify")

        if step == "verify":
            otp = (request.form.get("otp") or "").strip()
            new_pw = request.form.get("password") or ""
            confirm_pw = request.form.get("confirm_password") or ""
            ctx = session.get("pw_reset_main") or {}
            if not ctx.get("user_id") or not ctx.get("phone"):
                flash("Reset session expired. Please request a new code.", "danger")
                return render_template("forgot_password.html", step="request")

            if not otp or not re.fullmatch(r"^\d{6}$", otp):
                flash("Enter a valid 6-digit OTP.", "danger")
                return render_template("forgot_password.html", step="verify")

            if new_pw != confirm_pw:
                flash("Passwords do not match.", "danger")
                return render_template("forgot_password.html", step="verify")

            ok, reason = _verify_password_reset_otp(ObjectId(ctx["user_id"]), ctx["phone"], otp)
            if not ok:
                flash("Invalid or expired OTP. Request a new code.", "danger")
                return render_template("forgot_password.html", step="verify")

            users_col.update_one(
                {"_id": ObjectId(ctx["user_id"])},
                {"$set": {"password": generate_password_hash(new_pw), "updated_at": datetime.utcnow()}},
            )
            session.pop("pw_reset_main", None)
            flash("Password updated. Please login.", "success")
            return redirect(url_for("login.login"))

    return render_template("forgot_password.html", step=step)


@login_bp.route("/logout")
def logout():
    role = (session.get("role") or "").lower()
    admin_id = session.get("admin_id")
    slug = None
    if role in {"agent", "customer"} and admin_id:
        try:
            admin_oid = ObjectId(admin_id)
            doc = auth_pages_col.find_one({"admin_id": admin_oid}, {"slug": 1})
            slug = (doc or {}).get("slug") or None
        except Exception:
            slug = None

    session.clear()
    flash("Logged out successfully", "info")
    if slug:
        resp = redirect(url_for("admin_auth_pages.branded_landing", slug=slug))
    else:
        resp = redirect(url_for("login.login"))
    cookie_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
    resp.delete_cookie(cookie_name)
    return resp
