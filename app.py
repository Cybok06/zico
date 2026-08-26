from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, send_from_directory, session, request, redirect, render_template
from bson import ObjectId

# Load .env for non-secret things (e.g., Paystack keys)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from db import db  # required
auth_pages_col = db["auth_pages"]

from customer_dashboard import customer_dashboard_bp
from admin_dashboard import admin_dashboard_bp
from login import login_bp
from signup import signup_bp
from admin_customers import admin_customers_bp
from admin_phone_numbers import admin_phone_numbers_bp
from admin_services import admin_services_bp
from deposit import deposit_bp
from checkout import checkout_bp
from orders import orders_bp
from boostings import boostings_bp
from transactions import transactions_bp
from customer_profile import customer_profile_bp
from complaints import complaints_bp
from referral import referral_bp
from admin_orders import admin_orders_bp
from admin_transactions import admin_transactions_bp
from admin_complaints import admin_complaints_bp
from admin_referrals import admin_referrals_bp
from admin_balance import admin_balance_bp
from admin_wassce_checker import admin_wassce_checker_bp
from purchases import purchases_bp
from purchase_checker import purchase_checker_bp
from admin_purchases import admin_purchases_bp
from settings import settings_bp
from admin_sidebar import admin_sidebar_bp
from admin_admins import admin_admins_bp
from login_logs import login_logs_bp
from reset import reset_bp
from afa_routes import afa_bp
from admin_afa import admin_afa_bp
from cart_api import cart_api_bp
from check_status import check_status_bp
from shares import shares_bp
from agent_api import agent_api_bp
from admin_auth_pages import admin_auth_pages_bp
from admin_profile import admin_profile_bp
from paystack_transactions import paystack_transactions_bp
from admin_paystack_payouts import admin_paystack_payouts_bp
from admin_agent_codes import admin_agent_codes_bp
from admin_promo_codes import admin_promo_codes_bp
# Maintenance / billing
from maintenance import maintenance_bp, get_maintenance_status_for_admin_id
from announcements import announcements_bp
from admin_performance import admin_performance_bp
from internal_chat import internal_chat_bp
from bulk_sms import bulk_sms_bp
# Auto-upgrade admin levels (daily scheduler)
import admin_level_scheduler  # noqa: F401
# import customer_stage  # noqa: F401  (disabled: prevents daily stage updater)

# ✅ UPDATED: store blueprint now lives in routes/store_page.py
from routes.store_page import stores_bp
# ✅ IMPORTANT: importing this file attaches the create/api/media routes to stores_bp
import routes.store_create  # noqa: F401
from payments_moolre import moolre_payments_bp

from routes.customer_store import customer_store_bp
from routes.admin_store import admin_store_bp
from request_load import mark_request_start, mark_request_end
from tenant import current_admin_id_from_session, is_admin_role
from announcements import count_new_announcements_today

# ✅ Use ABSOLUTE import (place index.py next to this file)
from index import index_bp

# === Collections ===
visits_col = db["visits"]
users_col = db["users"]
admin_paystack_payout_requests_col = db["admin_paystack_payout_requests"]
manual_topups_col = db["manual_wallet_topups"]
internal_chat_messages_col = db["internal_chat_messages"]

# === Hard-coded config ===
SECRET_KEY = "m2k4vTq3Jp9Qf7A1R6xZ0Hc8Uy4Nd5LbX3gE2sW7iK0tP9qL5rV8wC6Bn1Dz0Ya"  # 64+ chars; keep private
SESSION_DAYS = 3650
SESSION_COOKIE_NAME = "azico_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"   # change to "Strict" if you don't embed cross-site
SESSION_COOKIE_SECURE = False     # set True if your site is HTTPS-only in production
SESSION_REFRESH_EACH_REQUEST = True

PRIMARY_ADMIN_HOST = "azico.site"
ADMIN_HOST_ALIASES = {
    "azico.site",
    "www.azico.site",
}
PRIMARY_AGENT_HOST = "zishop.site"
AGENT_HOST_ALIASES = {
    "zishop.site",
    "www.zishop.site",
}
STORE_PUBLIC_HOST = os.getenv("STORE_PUBLIC_HOST", "nagmart.store").strip().lower()
LOCAL_HOST_ALIASES = {
    "127.0.0.1",
    "localhost",
}

# Read upload folder from env (fallback to ./uploads)
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(os.getcwd(), "uploads"))


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


def create_app():
    app = Flask(__name__)

    # --- Session / cookies (all hard-coded) ---
    app.secret_key = SECRET_KEY
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    app.config["SESSION_COOKIE_HTTPONLY"] = SESSION_COOKIE_HTTPONLY
    app.config["SESSION_COOKIE_SAMESITE"] = SESSION_COOKIE_SAMESITE
    app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
    app.config["SESSION_REFRESH_EACH_REQUEST"] = SESSION_REFRESH_EACH_REQUEST

    app.before_request(mark_request_start)
    app.after_request(mark_request_end)
    app.teardown_request(lambda exc: mark_request_end(None))

    # Keep sessions permanent whenever user is logged in
    @app.before_request
    def _keep_permanent_sessions():
        if session.get("user_id"):
            session.permanent = True

    def _host_only(raw_host: str) -> str:
        return (raw_host or "").split(":", 1)[0].strip().lower()

    def _is_store_public_host(host: str) -> bool:
        return bool(STORE_PUBLIC_HOST) and host in {STORE_PUBLIC_HOST, f"www.{STORE_PUBLIC_HOST}"}

    def _is_public_store_path(path: str) -> bool:
        return path.startswith((
            "/check-status",
            "/s/",
            "/store/",
            "/store-invoice/",
            "/store-checkout/",
            "/api/store-email/",
            "/api/store-products/",
            "/api/store-afa/",
            "/api/store-admin-complaints/",
            "/api/store-paystack-public-key/",
            "/api/store-complaints/",
            "/api/store-order/",
            "/api/store-order-by-ref/",
        ))

    def _is_public_store_api_path(path: str) -> bool:
        return path.startswith((
            "/store-checkout/",
            "/api/store-email/",
            "/api/store-afa/",
            "/api/store-admin-complaints/",
            "/api/store-paystack-public-key/",
            "/api/store-complaints/",
            "/api/store-order/",
            "/api/store-order-by-ref/",
        ))

    def _build_host_redirect(target_host: str):
        current = urlsplit(request.url)
        netloc = target_host
        current_host = request.host or ""
        if ":" in current_host:
            try:
                port = current_host.split(":", 1)[1]
                if port and port not in {"80", "443"}:
                    netloc = f"{target_host}:{port}"
            except Exception:
                pass
        target_url = urlunsplit((current.scheme, netloc, current.path, current.query, current.fragment))
        code = 308 if request.method not in {"GET", "HEAD", "OPTIONS"} else 302
        return redirect(target_url, code=code)

    def _build_admin_login_redirect():
        current = urlsplit(request.url)
        netloc = PRIMARY_ADMIN_HOST
        current_host = request.host or ""
        if ":" in current_host:
            try:
                port = current_host.split(":", 1)[1]
                if port and port not in {"80", "443"}:
                    netloc = f"{PRIMARY_ADMIN_HOST}:{port}"
            except Exception:
                pass
        return redirect(urlunsplit((current.scheme, netloc, "/login", "", "")), code=302)

    def _is_auth_path(path: str) -> bool:
        return path == "/login" or path == "/forgot-password" or path.startswith(("/signup", "/reset"))

    def _domain_404_response():
        return (
            render_template("404.html"),
            404,
            {
                "Cache-Control": "no-store, private",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    def _is_agent_protected_path(path: str) -> bool:
        return path.startswith((
            "/customer",
            "/deposit",
            "/checkout",
            "/orders",
            "/transactions",
            "/complaints",
            "/referral",
            "/purchase_checker",
            "/purchases",
            "/shares",
            "/agent",
            "/create-store",
            "/store/create",
            "/api/customer",
            "/api/store",
            "/api/cart",
            "/api/bulk-sms",
        ))

    def _desired_host_for_request() -> str | None:
        endpoint = request.endpoint or ""
        path = request.path or "/"
        current_host = _host_only(request.host)

        if endpoint in {"static", "uploaded_file", "images_file"}:
            return None

        if _is_store_public_host(current_host) and _is_public_store_path(path):
            return None

        if path == "/":
            return PRIMARY_ADMIN_HOST

        if endpoint.startswith("admin_auth_pages."):
            if endpoint in {
                "admin_auth_pages.admin_auth_page",
                "admin_auth_pages.admin_auth_upload_image",
            }:
                return PRIMARY_ADMIN_HOST
            return PRIMARY_AGENT_HOST

        if endpoint == "login.logout":
            role = (session.get("role") or "").strip().lower()
            return PRIMARY_AGENT_HOST if role in {"agent", "customer"} else PRIMARY_ADMIN_HOST

        if endpoint in {"reset.reset_form", "reset.reset_apply"}:
            return PRIMARY_ADMIN_HOST

        if endpoint == "reset.admin_generate_reset":
            return PRIMARY_ADMIN_HOST

        if endpoint in {
            "announcements.acknowledge_announcement",
            "announcements.list_announcements",
            "announcements.add_comment",
        }:
            role = (session.get("role") or "").strip().lower()
            return PRIMARY_ADMIN_HOST if role in {"admin", "main_admin", "super_admin", "superadmin", "professional_admin", "super_professional"} else PRIMARY_AGENT_HOST

        if endpoint in {
            "announcements.upload_announcement_image",
            "announcements.create_announcement",
            "announcements.delete_announcement",
        }:
            return PRIMARY_ADMIN_HOST

        if endpoint in {"login.login", "login.forgot_password"} or endpoint.startswith("signup."):
            return PRIMARY_ADMIN_HOST

        if endpoint.startswith("index.") or endpoint.startswith("login.") or endpoint.startswith("reset."):
            return PRIMARY_ADMIN_HOST

        if endpoint.startswith("check_status."):
            return None

        if endpoint.startswith((
            "admin_dashboard.",
            "admin_customers.",
            "admin_phone_numbers.",
            "admin_services.",
            "admin_orders.",
            "admin_transactions.",
            "admin_complaints.",
            "admin_referrals.",
            "admin_balance.",
            "admin_wassce_checker.",
            "admin_purchases.",
            "settings.",
            "admin_sidebar.",
            "admin_admins.",
            "login_logs.",
            "admin_afa.",
            "admin_profile.",
            "paystack_transactions.",
            "admin_paystack_payouts.",
            "maintenance.",
            "admin_performance.",
            "admin_agent_codes.",
            "admin_promo_codes.",
            "internal_chat.",
        )):
            return PRIMARY_ADMIN_HOST

        if endpoint.startswith("moolre_payments."):
            role = (session.get("role") or "").strip().lower()
            if role in {"admin", "main_admin", "super_admin", "superadmin", "professional_admin", "super_professional"}:
                return PRIMARY_ADMIN_HOST
            if role in {"agent", "customer"}:
                return PRIMARY_AGENT_HOST

        if endpoint in {"bulk_sms.admin_bulk_sms_deliveries"}:
            return PRIMARY_ADMIN_HOST

        if endpoint.startswith("deposit.admin_") or endpoint == "deposit.verify_wallet_deposit":
            return PRIMARY_ADMIN_HOST

        if endpoint == "boostings.admin_boostings" or path.startswith("/admin/boostings"):
            return PRIMARY_ADMIN_HOST

        if endpoint == "boostings.customer_boostings":
            return PRIMARY_AGENT_HOST

        if endpoint.startswith((
            "customer_dashboard.",
            "deposit.",
            "checkout.",
            "orders.",
            "boostings.",
            "transactions.",
            "customer_profile.",
            "complaints.",
            "referral.",
            "purchases.",
            "purchase_checker.",
            "shares.",
            "agent_api.",
            "stores.",
            "customer_store.",
        )):
            return PRIMARY_AGENT_HOST

        if endpoint in {
            "bulk_sms.customer_bulk_sms_deliveries",
            "bulk_sms.bulk_sms_pricing",
            "bulk_sms.create_bulk_sms_order",
        }:
            return PRIMARY_AGENT_HOST

        if path.startswith((
            "/customer",
            "/deposit",
            "/checkout",
            "/orders",
            "/transactions",
            "/complaints",
            "/referral",
            "/purchase_checker",
            "/purchases",
            "/shares",
            "/store/",
            "/s/",
            "/store-invoice/",
            "/api/store",
            "/api/cart",
            "/api/bulk-sms",
        )):
            return PRIMARY_AGENT_HOST

        if path.startswith((
            "/admin",
            "/settings",
            "/login",
            "/signup",
            "/reset",
        )):
            return PRIMARY_ADMIN_HOST

        return None

    @app.before_request
    def _enforce_domain_split():
        host = _host_only(request.host)
        if not host:
            return None

        if host in LOCAL_HOST_ALIASES:
            return None

        path = request.path or "/"

        if host in AGENT_HOST_ALIASES and (path == "/" or _is_auth_path(path)):
            return _domain_404_response()

        desired_host = _desired_host_for_request()

        if (
            host in AGENT_HOST_ALIASES
            and not session.get("user_id")
            and _is_agent_protected_path(path)
            and not _is_public_store_api_path(path)
        ):
            return _build_admin_login_redirect()

        if desired_host == PRIMARY_ADMIN_HOST and host not in ADMIN_HOST_ALIASES:
            return _build_host_redirect(PRIMARY_ADMIN_HOST)

        if desired_host == PRIMARY_AGENT_HOST and host not in AGENT_HOST_ALIASES:
            return _build_host_redirect(PRIMARY_AGENT_HOST)

        if desired_host == PRIMARY_ADMIN_HOST and host == "www.azico.site":
            return _build_host_redirect(PRIMARY_ADMIN_HOST)

        if desired_host == PRIMARY_AGENT_HOST and host == "www.zishop.site":
            return _build_host_redirect(PRIMARY_AGENT_HOST)

        return None

    # Count visits to "/" without adding a second route
    @app.before_request
    def _count_home_visits():
        if request.path == "/":
            try:
                visits_col.update_one(
                    {"_id": "global"},
                    {
                        "$inc": {"total": 1},
                        "$set": {"updated_at": datetime.utcnow()},
                        "$setOnInsert": {"created_at": datetime.utcnow()},
                    },
                    upsert=True,
                )
            except Exception as e:
                print(f"[visits] increment failed: {e}")

    # --- File uploads ---
    app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # --- Blueprints ---
    app.register_blueprint(customer_dashboard_bp)
    app.register_blueprint(admin_dashboard_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(signup_bp)
    app.register_blueprint(admin_customers_bp)
    app.register_blueprint(admin_phone_numbers_bp)
    app.register_blueprint(admin_services_bp)
    app.register_blueprint(deposit_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(boostings_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(customer_profile_bp)
    app.register_blueprint(complaints_bp)
    app.register_blueprint(referral_bp)
    app.register_blueprint(admin_orders_bp)
    app.register_blueprint(admin_transactions_bp)
    app.register_blueprint(admin_complaints_bp)
    app.register_blueprint(admin_referrals_bp)
    app.register_blueprint(admin_balance_bp)
    app.register_blueprint(admin_wassce_checker_bp)
    app.register_blueprint(purchase_checker_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(admin_purchases_bp)
    app.register_blueprint(admin_sidebar_bp)
    app.register_blueprint(admin_admins_bp)
    app.register_blueprint(login_logs_bp)
    app.register_blueprint(reset_bp)
    app.register_blueprint(afa_bp)
    app.register_blueprint(admin_afa_bp)
    app.register_blueprint(cart_api_bp)  # no prefix; routes already start with /api/cart
    app.register_blueprint(index_bp)     # serves "/" dynamically with offers & public buy
    app.register_blueprint(check_status_bp)
    app.register_blueprint(shares_bp)
    app.register_blueprint(agent_api_bp)
    app.register_blueprint(admin_auth_pages_bp)
    app.register_blueprint(admin_profile_bp)
    app.register_blueprint(paystack_transactions_bp)
    app.register_blueprint(admin_paystack_payouts_bp)
    app.register_blueprint(admin_agent_codes_bp)
    app.register_blueprint(admin_promo_codes_bp)
    app.register_blueprint(moolre_payments_bp)
    app.register_blueprint(maintenance_bp)
    app.register_blueprint(announcements_bp)
    app.register_blueprint(admin_performance_bp)
    app.register_blueprint(internal_chat_bp)
    app.register_blueprint(bulk_sms_bp)

    # ✅ Store module (public store page + create + CRUD + media) now comes from store_page/store_create split
    app.register_blueprint(stores_bp)

    app.register_blueprint(customer_store_bp)
    app.register_blueprint(admin_store_bp)

    # --- Jinja env injection ---
    @app.context_processor
    def inject_env():
        def internal_chat_unread_count():
            role_value = (session.get("role") or "").strip().lower()
            if role_value not in {"main_admin", "admin", "professional_admin", "super_admin", "superadmin", "agent", "customer"} or not session.get("user_id"):
                return 0
            try:
                user_oid = ObjectId(session["user_id"])
                return int(
                    internal_chat_messages_col.count_documents(
                        {"recipient_id": user_oid, "read_by": {"$ne": user_oid}}
                    )
                )
            except Exception:
                return 0

        admin_level = None
        admin_level_label = None
        role = session.get("role")
        maintenance = None
        tenant_branding = None
        pending_paystack_payout_requests_count = 0
        pending_agents_count = 0
        pending_manual_topups_count = 0
        announcements_new_today_count = 0
        role_key = (role or "").strip().lower()

        if role_key == "main_admin":
            admin_level = "main_admin"
            admin_level_label = "Main Admin"
        elif is_admin_role(role_key) and session.get("user_id"):
            try:
                user_oid = ObjectId(session["user_id"])
                udoc = users_col.find_one({"_id": user_oid}, {"admin_level": 1})
                admin_level = (udoc or {}).get("admin_level") or "admin"
            except Exception:
                admin_level = session.get("admin_level") or "admin"

            if admin_level == "super_admin":
                admin_level_label = "Super Admin"
            elif admin_level == "super_professional":
                admin_level_label = "Professional Admin"
            else:
                admin_level_label = "Admin"

        # Maintenance status (admin only)
        if is_admin_role(role_key) and session.get("user_id"):
            try:
                maintenance = get_maintenance_status_for_admin_id(session.get("user_id"))
            except Exception:
                maintenance = None

        if role_key == "main_admin":
            try:
                pending_paystack_payout_requests_count = int(
                    admin_paystack_payout_requests_col.count_documents({"status": "pending"})
                )
            except Exception:
                pending_paystack_payout_requests_count = 0

        if is_admin_role(role_key) and session.get("user_id"):
            try:
                pending_agents_query = {
                    "role": "agent",
                    "status": "pending",
                    "approval_status": "pending",
                    "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
                }
                if role_key != "main_admin":
                    pending_agents_query["admin_id"] = ObjectId(session["user_id"])
                pending_agents_count = int(users_col.count_documents(pending_agents_query))
            except Exception:
                pending_agents_count = 0

        if is_admin_role(role_key) and session.get("user_id"):
            try:
                pending_manual_query = {"status": "pending"}
                if role_key == "main_admin":
                    pending_manual_query["source"] = {"$ne": "agent_wallet_manual"}
                else:
                    pending_manual_query["source"] = "agent_wallet_manual"
                    pending_manual_query["admin_id"] = ObjectId(session["user_id"])
                pending_manual_topups_count = int(manual_topups_col.count_documents(pending_manual_query))
            except Exception:
                pending_manual_topups_count = 0

        # Tenant branding + contact for agents/customers
        if role in {"agent", "customer"} and session.get("admin_id"):
            try:
                admin_oid = ObjectId(session.get("admin_id"))
                bdoc = auth_pages_col.find_one(
                    {"admin_id": admin_oid},
                    {
                        "business_name": 1,
                        "logo_url": 1,
                        "slug": 1,
                        "email": 1,
                        "phone": 1,
                        "whatsapp": 1,
                        "support_email": 1,
                        "support_phone": 1,
                        "whatsapp_link": 1,
                    },
                )
                admin_doc = users_col.find_one(
                    {"_id": admin_oid},
                    {"business_name": 1, "username": 1, "email": 1, "phone": 1, "whatsapp": 1},
                )
                if bdoc or admin_doc:
                    brand_name = (
                        (bdoc or {}).get("business_name")
                        or (admin_doc or {}).get("business_name")
                        or (admin_doc or {}).get("username")
                        or ""
                    )
                    support_email = (
                        (bdoc or {}).get("support_email")
                        or (bdoc or {}).get("email")
                        or (admin_doc or {}).get("email")
                        or ""
                    )
                    support_phone = (
                        (bdoc or {}).get("support_phone")
                        or (bdoc or {}).get("phone")
                        or (admin_doc or {}).get("phone")
                        or ""
                    )
                    whatsapp_link = (
                        (bdoc or {}).get("whatsapp_link")
                        or (bdoc or {}).get("whatsapp")
                        or (admin_doc or {}).get("whatsapp")
                        or ""
                    )
                    whatsapp_link = _normalize_whatsapp_link(whatsapp_link)
                    tenant_branding = {
                        "business_name": brand_name,
                        "logo_url": (bdoc or {}).get("logo_url") or "",
                        "slug": (bdoc or {}).get("slug") or "",
                        "email": support_email,
                        "phone": support_phone,
                        "whatsapp": whatsapp_link,
                        "support_email": support_email,
                        "support_phone": support_phone,
                        "whatsapp_link": whatsapp_link,
                    }
            except Exception:
                tenant_branding = None

        if role_key in {"main_admin", "admin", "professional_admin", "super_admin", "superadmin", "agent", "customer"} and session.get("user_id"):
            try:
                announcements_new_today_count = int(
                    count_new_announcements_today(
                        role_key,
                        current_admin_id_from_session(session),
                        session.get("user_id"),
                    )
                )
            except Exception:
                announcements_new_today_count = 0

        return {
            "PAYSTACK_PUBLIC_KEY": os.getenv("PAYSTACK_PUBLIC_KEY", ""),
            "COMPANY_NAME": os.getenv("COMPANY_NAME", "AZICO"),
            "SUPPORT_EMAIL": os.getenv("SUPPORT_EMAIL", "yagyae4@gmail.com"),
            "SUPPORT_PHONE": os.getenv("SUPPORT_PHONE", "0240818745"),
            "SUPPORT_WHATSAPP": os.getenv("SUPPORT_WHATSAPP", "https://wa.me/233240818745"),
            "COMMUNITY_WHATSAPP": os.getenv(
                "COMMUNITY_WHATSAPP",
                "https://chat.whatsapp.com/ELrcPhAcUNGJGgxncgvbXW?mode=ac_t",
            ),
            "admin_level": admin_level,
            "admin_level_label": admin_level_label,
            "maintenance": maintenance,
            "tenant_branding": tenant_branding,
            "pending_paystack_payout_requests_count": pending_paystack_payout_requests_count,
            "pending_agents_count": pending_agents_count,
            "pending_manual_topups_count": pending_manual_topups_count,
            "announcements_new_today_count": announcements_new_today_count,
            "internal_chat_unread_count": internal_chat_unread_count,
            "endpoint_exists": lambda endpoint: endpoint in app.view_functions,
        }

    # --- Utility routes ---
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.route("/images/<path:filename>")
    def images_file(filename):
        return send_from_directory(os.path.join(os.getcwd(), "images"), filename)

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    return app


# Gunicorn entrypoint: `gunicorn app:app`
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
