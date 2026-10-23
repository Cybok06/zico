# admin_sidebar.py
from flask import Blueprint, session
from db import db
from tenant import current_admin_id_from_session, is_admin_role
from admin_paystack_ledger import evaluate_admin_wallet_low_balance

admin_sidebar_bp = Blueprint("admin_sidebar", __name__)

orders_col = db["orders"]
complaints_col = db["complaints"]   # change if your collection name differs
blocked_phone_numbers_col = db["blocked_phone_numbers"]
manual_topups_col = db["manual_wallet_topups"]
BOOSTING_PROVIDER = "exosupplier"


def _is_admin() -> bool:
    return is_admin_role(session.get("role"))

def _is_main_admin() -> bool:
    return (session.get("role") or "").strip().lower() == "main_admin"


@admin_sidebar_bp.app_context_processor
def inject_admin_counts():
    """
    Inject counts into all templates. If not an admin, expose zeros so templates stay safe.
    """
    if not _is_admin():
        return {
            "pending_orders_count": 0,
            "undelivered_orders_count": 0,
            "processing_boostings_count": 0,
            "pending_complaints_count": 0,
            "payment_confirmed_complaints_count": 0,
            "pending_manual_topups_count": 0,
            "blocked_phone_numbers_count": 0,
            "admin_wallet_low": False,
            "admin_wallet_balance": 0,
            "admin_wallet_low_limit": 50,
        }

    admin_oid = current_admin_id_from_session(session)
    scope = {} if _is_main_admin() else ({"admin_id": admin_oid} if admin_oid else {})
    complaint_scope = {"sent_to_main_admin": True} if _is_main_admin() else scope

    # Pending orders (if you use it anywhere else)
    try:
        pending_orders = orders_col.count_documents({"status": "pending", **scope})
    except Exception:
        pending_orders = 0

    # Orders that are pending or processing
    try:
        undelivered_orders = orders_col.count_documents({"status": {"$in": ["pending", "processing"]}, **scope})
    except Exception:
        undelivered_orders = 0

    try:
        processing_boostings = orders_col.count_documents(
            {
                **scope,
                "$or": [
                    {
                        "items": {
                            "$elemMatch": {
                                "provider": BOOSTING_PROVIDER,
                                "line_status": "processing",
                            }
                        }
                    },
                    {
                        "status": "processing",
                        "items.provider": BOOSTING_PROVIDER,
                    },
                ],
            }
        )
    except Exception:
        processing_boostings = 0

    # Complaints that are pending
    try:
        pending_complaints = complaints_col.count_documents({"status": "pending", **complaint_scope})
    except Exception:
        pending_complaints = 0

    try:
        payment_confirmed_complaints = 0
        if not _is_main_admin():
            payment_confirmed_complaints = complaints_col.count_documents({
                **complaint_scope,
                "payment_confirmed": True,
                "cart_snapshot": {"$exists": True, "$ne": []},
                "store_order_processed": {"$ne": True},
            })
    except Exception:
        payment_confirmed_complaints = 0

    # Manual wallet topups pending (main admin only)
    try:
        pending_manual_topups = manual_topups_col.count_documents({"status": "pending"}) if _is_main_admin() else 0
    except Exception:
        pending_manual_topups = 0

    # Active blocked phone numbers
    try:
        blocked_phone_numbers = blocked_phone_numbers_col.count_documents({"is_active": True, **scope})
    except Exception:
        blocked_phone_numbers = 0

    wallet_status = {"low": False, "balance": 0, "limit": 50}
    if not _is_main_admin() and admin_oid:
        try:
            wallet_status = evaluate_admin_wallet_low_balance(admin_oid, send_alert=False, run_auto_credit=False)
        except Exception:
            wallet_status = {"low": False, "balance": 0, "limit": 50}

    return {
        "pending_orders_count": pending_orders,
        "undelivered_orders_count": undelivered_orders,
        "processing_boostings_count": processing_boostings,
        "pending_complaints_count": pending_complaints,
        "payment_confirmed_complaints_count": payment_confirmed_complaints,
        "pending_manual_topups_count": pending_manual_topups,
        "blocked_phone_numbers_count": blocked_phone_numbers,
        "admin_wallet_low": bool(wallet_status.get("low")),
        "admin_wallet_balance": wallet_status.get("balance", 0),
        "admin_wallet_low_limit": wallet_status.get("limit", 50),
    }
