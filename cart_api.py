from flask import Blueprint, request, jsonify, session
from bson import ObjectId
from datetime import datetime
import re

from db import db

cart_api_bp = Blueprint("cart_api", __name__)

carts_col = db["carts"]  # { user_id, items: [ { _id, ... } ], updated_at }

PHONE_RE = re.compile(r"^0\d{9}$")

def _oid(x):
    return ObjectId(x) if isinstance(x, str) else x

def _ensure_customer():
    # Require an authenticated buyer user (customer/agent)
    if not session.get("user_id") or session.get("role") not in {"customer", "agent"}:
        return None
    try:
        return ObjectId(session["user_id"])
    except Exception:
        return None

def _to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default

def _is_social_boosting_item(raw):
    if not isinstance(raw, dict):
        return False
    value_obj = raw.get("value_obj")
    provider = str(raw.get("provider") or "").strip().lower()
    return bool(
        provider == "exosupplier"
        or (isinstance(value_obj, dict) and value_obj.get("social_boosting"))
        or str(raw.get("target_link") or "").strip()
    )

def _normalize_item(raw):
    """
    Accept and normalize a cart item from client.
    Required: serviceId, serviceName, phone, amount/total
    Optional: value, value_obj, base_amount
    """
    if not isinstance(raw, dict):
        return None, "Invalid item"
    service_id = raw.get("serviceId") or raw.get("service_id")
    service_name = (raw.get("serviceName") or raw.get("service_name") or "").strip()
    value_obj = raw.get("value_obj")
    phone = (raw.get("phone") or "").strip()
    amount = raw.get("total", raw.get("amount"))
    is_social_boosting = _is_social_boosting_item(raw)
    target_link = (
        raw.get("target_link")
        or (value_obj.get("link") if isinstance(value_obj, dict) else "")
        or phone
        or ""
    ).strip()

    if not service_id or not service_name:
        return None, "Missing service info"
    if is_social_boosting:
        if not target_link:
            return None, "Target link is required"
        phone = target_link
    elif not PHONE_RE.match(phone):
        return None, "Invalid phone (must be 0xxxxxxxxx)"
    try:
        amount = float(amount)
        if amount <= 0:
            return None, "Invalid amount"
    except Exception:
        return None, "Invalid amount"

    item = {
        "_id": ObjectId(),
        "serviceId": str(service_id),
        "serviceName": service_name,
        "phone": phone,
        "value": raw.get("value"),          # label like '1GB'
        "value_obj": value_obj,             # original offer value object (if any)
        "base_amount": float(raw.get("base_amount") or 0.0),
        "amount": amount,
        "total": amount,                    # keep both for compatibility with your JS
        "created_at": datetime.utcnow(),
    }

    if is_social_boosting:
        item["target_link"] = target_link
        item["provider"] = str(raw.get("provider") or "exosupplier")
        item["currency"] = str(raw.get("currency") or "GHS")
        item["provider_currency"] = str(raw.get("provider_currency") or "USD")

    for key in ("service_network", "network", "ported_expected_network", "ported_detected_network", "ported_prefix"):
        value = raw.get(key)
        if value not in (None, ""):
            item[key] = value

    if "ported_confirmed" in raw:
        item["ported_confirmed"] = bool(raw.get("ported_confirmed"))

    for key in ("base_amount_usd", "amount_usd", "usd_to_ghs_rate"):
        value = _to_float(raw.get(key), None)
        if value is not None:
            item[key] = value

    quantity = raw.get("quantity")
    if quantity in (None, "") and isinstance(value_obj, dict):
        quantity = value_obj.get("quantity")
    if quantity not in (None, ""):
        qty_num = _to_float(quantity, None)
        if qty_num is not None:
            item["quantity"] = int(qty_num)

    provider_service_id = raw.get("provider_service_id")
    if provider_service_id in (None, "") and isinstance(value_obj, dict):
        provider_service_id = value_obj.get("provider_service_id")
    if provider_service_id not in (None, ""):
        item["provider_service_id"] = provider_service_id

    return item, None

def _get_cart_doc(user_oid):
    doc = carts_col.find_one({"user_id": user_oid}, {"items": 1, "updated_at": 1})
    if not doc:
        doc = {"user_id": user_oid, "items": [], "updated_at": datetime.utcnow()}
        carts_col.insert_one(doc)
    return doc

@cart_api_bp.route("/api/cart", methods=["GET"])
def get_cart():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    # stringify _id for JSON
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/add_bulk", methods=["POST"])
def add_bulk():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"success": False, "error": "No items"}), 400

    normalized = []
    for r in raw_items:
        item, err = _normalize_item(r)
        if err:
            return jsonify({"success": False, "error": err}), 400
        normalized.append(item)

    carts_col.update_one(
        {"user_id": user_oid},
        {"$push": {"items": {"$each": normalized}}, "$set": {"updated_at": datetime.utcnow()}},
        upsert=True,
    )

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/replace", methods=["POST"])
def replace_cart():
    """
    Replace entire cart with given items array (used by client sync).
    """
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        return jsonify({"success": False, "error": "Invalid items"}), 400

    normalized = []
    for r in raw_items:
        item, err = _normalize_item(r)
        if err:
            return jsonify({"success": False, "error": err}), 400
        normalized.append(item)

    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": normalized, "updated_at": datetime.utcnow()}},
        upsert=True,
    )

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/remove", methods=["POST"])
def remove_item():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    item_id = payload.get("item_id")
    if not item_id:
        return jsonify({"success": False, "error": "item_id required"}), 400

    try:
        carts_col.update_one(
            {"user_id": user_oid},
            {"$pull": {"items": {"_id": ObjectId(item_id)}}, "$set": {"updated_at": datetime.utcnow()}},
        )
    except Exception:
        return jsonify({"success": False, "error": "Invalid item_id"}), 400

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/clear", methods=["POST"])
def clear_cart():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": [], "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return jsonify({"success": True, "items": [], "count": 0})

@cart_api_bp.route("/api/cart/checkout_start", methods=["POST"])
def checkout_start():
    """
    Atomically snapshot cart items and clear them, returning the snapshot.
    Use this response as the immutable 'lockedCart' for payment.
    """
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    total = 0.0
    out = []
    for it in items:
        amt = float(it.get("total", it.get("amount", 0)) or 0)
        total += amt
        snapshot = {
            "_id": str(it["_id"]),
            "serviceId": it.get("serviceId"),
            "serviceName": it.get("serviceName"),
            "phone": it.get("phone"),
            "value": it.get("value"),
            "value_obj": it.get("value_obj"),
            "base_amount": float(it.get("base_amount", 0) or 0),
            "amount": float(it.get("amount", amt) or amt),
            "total": float(it.get("total", amt) or amt),
        }
        for key in (
            "target_link",
            "provider",
            "service_network",
            "network",
            "currency",
            "provider_currency",
            "ported_expected_network",
            "ported_detected_network",
            "ported_prefix",
            "provider_service_id",
            "quantity",
        ):
            value = it.get(key)
            if value not in (None, ""):
                snapshot[key] = value
        if "ported_confirmed" in it:
            snapshot["ported_confirmed"] = bool(it.get("ported_confirmed"))
        for key in ("base_amount_usd", "amount_usd", "usd_to_ghs_rate"):
            value = _to_float(it.get(key), None)
            if value is not None:
                snapshot[key] = value
        out.append(snapshot)

    # Clear the cart immediately
    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": [], "updated_at": datetime.utcnow()}},
        upsert=True,
    )

    return jsonify({
        "success": True,
        "locked": out,
        "count": len(out),
        "total": round(total, 2)
    })
