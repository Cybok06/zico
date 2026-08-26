from flask import Blueprint, render_template, session, redirect, url_for, jsonify, request
from bson import ObjectId
from datetime import datetime
import secrets
import re
from db import db
from tenant import resolve_admin_id_for_user_id

agent_api_bp = Blueprint("agent_api", __name__)
api_keys_col = db["api_keys"]
users_col = db["users"]
auth_pages_col = db["auth_pages"]
services_col = db["services"]
balances_col = db["balances"]
orders_col = db["orders"]

from customer_dashboard import (
    _service_state,
    _service_unit,
    _parse_value_field,
    _extract_volume,
    _value_text_for_display,
    _customer_price_for_offer,
    _offer_id_from_value,
    _service_priority_tuple,
    _to_float,
)
from checkout import _process_checkout_core

def _require_customer():
    return bool(session.get("user_id")) and session.get("role") in {"customer", "agent"}

def _api_brand_prefix_for_user(user_oid: ObjectId) -> str:
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    if not admin_id:
        return "AZICO"

    brand_name = ""
    try:
        auth_doc = auth_pages_col.find_one({"admin_id": admin_id}, {"business_name": 1})
    except Exception:
        auth_doc = None
    try:
        admin_doc = users_col.find_one({"_id": admin_id}, {"business_name": 1, "username": 1})
    except Exception:
        admin_doc = None

    brand_name = (
        (auth_doc or {}).get("business_name")
        or (admin_doc or {}).get("business_name")
        or (admin_doc or {}).get("username")
        or ""
    )
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", brand_name or "").strip("_").upper()
    return prefix[:18] or "AZICO"

def _generate_api_key(user_oid: ObjectId) -> str:
    return f"{_api_brand_prefix_for_user(user_oid)}_" + secrets.token_urlsafe(24)

def _generate_api_reference(phone: str) -> str:
    base = re.sub(r"\D+", "", phone or "")[-10:] or "0000000000"
    return "API" + base + secrets.token_hex(4)

def _api_auth():
    key = (
        request.headers.get("x-api-key")
        or request.headers.get("X-API-KEY")
        or request.headers.get("X-Api-Key")
        or request.args.get("api_key")
        or ""
    ).strip()
    if not key:
        return None, jsonify({"status": 401, "message": "API key required"}), 401

    doc = api_keys_col.find_one({"key": key})
    if not doc or not doc.get("user_id"):
        return None, jsonify({"status": 401, "message": "Invalid API key"}), 401

    user_id = doc.get("user_id")
    user_doc = users_col.find_one({"_id": user_id}, {"role": 1})
    if not user_doc or user_doc.get("role") not in {"customer", "agent"}:
        return None, jsonify({"status": 403, "message": "Unauthorized"}), 403

    return user_id, None, None

def _is_express_service(svc: dict) -> bool:
    cat = (svc.get("service_category") or "").strip().lower()
    cat2 = (svc.get("category") or "").strip().lower()
    return cat == "express services" or cat2 == "express"

def _user_stage_label(user_oid: ObjectId) -> str:
    try:
        user_doc = users_col.find_one({"_id": user_oid}, {"stage_label": 1})
    except Exception:
        user_doc = None
    return (user_doc or {}).get("stage_label") or "Normal Agent"

def _build_packages_for_user(user_oid: ObjectId):
    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)
    if not admin_id:
        return None, None, "Account is not mapped to an admin"
    raw_services = list(services_col.find({"admin_id": admin_id})) if admin_id else []
    raw_services.sort(key=_service_priority_tuple)
    stage_label = _user_stage_label(user_oid)

    regular_packages = []
    bigtime_packages = []

    for s in raw_services:
        s["_id_str"] = str(s["_id"])
        st = _service_state(s)
        s.update(st)

        unit = _service_unit(s)
        offers = s.get("offers") or []
        for idx, of in enumerate(offers, start=1):
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)
            value_text = _value_text_for_display(parsed_value, unit)
            amount = _to_float(of.get("amount"))
            offer_id = _offer_id_from_value(parsed_value, idx)
            total = _customer_price_for_offer(s, of, offer_id, user_oid, stage_label)

            item = {
                "service_name": s.get("name") or s.get("network") or "",
                "network": (s.get("network") or s.get("name") or "").strip(),
                "offer_id": offer_id,
                "package": value_text,
                "amount": total,
            }

            if _is_express_service(s):
                bigtime_packages.append(item)
            else:
                regular_packages.append(item)

    return regular_packages, bigtime_packages

def _resolve_service_offer(payload: dict, user_oid: ObjectId, mode: str | None):
    service_id = (payload.get("service_id") or payload.get("serviceId") or "").strip()
    service_name = (payload.get("service_name") or payload.get("serviceName") or "").strip()
    network = (payload.get("network") or payload.get("service") or "").strip()

    admin_id = resolve_admin_id_for_user_id(users_col, user_oid)

    svc_doc = None
    if service_id:
        try:
            svc_doc = services_col.find_one({"_id": ObjectId(service_id), "admin_id": admin_id})
        except Exception:
            svc_doc = None
    if svc_doc is None and service_name:
        svc_doc = services_col.find_one(
            {
                "admin_id": admin_id,
                "$or": [
                    {"name": {"$regex": f"^{re.escape(service_name)}$", "$options": "i"}},
                    {"network": {"$regex": f"^{re.escape(service_name)}$", "$options": "i"}},
                    {"service_network": {"$regex": f"^{re.escape(service_name)}$", "$options": "i"}},
                ]
            }
        )

    if svc_doc is None and network:
        svc_doc = services_col.find_one(
            {
                "admin_id": admin_id,
                "$or": [
                    {"name": {"$regex": f"^{re.escape(network)}$", "$options": "i"}},
                    {"network": {"$regex": f"^{re.escape(network)}$", "$options": "i"}},
                    {"service_network": {"$regex": f"^{re.escape(network)}$", "$options": "i"}},
                ]
            }
        )

    if not svc_doc:
        return None, None, "Service not found"

    if mode in ("bigtime", "regular"):
        is_express = _is_express_service(svc_doc)
        if (mode == "bigtime" and not is_express) or (mode == "regular" and is_express):
            return None, None, "Service not allowed for this endpoint"

    unit = _service_unit(svc_doc)
    offers = svc_doc.get("offers") or []
    stage_label = _user_stage_label(user_oid)

    offer_id_raw = payload.get("offer_id") or payload.get("offerId")
    offer_id = None
    if offer_id_raw not in (None, ""):
        try:
            offer_id = int(float(offer_id_raw))
        except Exception:
            offer_id = str(offer_id_raw).strip()

    value_raw = payload.get("value") or payload.get("package")
    gig_raw = payload.get("gig")

    chosen = None
    best_score = None
    for idx, of in enumerate(offers, start=1):
        parsed_value = _parse_value_field(of.get("value"))
        vol_num = _extract_volume(parsed_value, unit)
        value_text = _value_text_for_display(parsed_value, unit)
        amount = _to_float(of.get("amount"))
        offer_id_calc = _offer_id_from_value(parsed_value, idx)
        total = _customer_price_for_offer(svc_doc, of, offer_id_calc, user_oid, stage_label)

        offer_pkg_id = _offer_id_from_value(parsed_value, idx)

        # offer_id match
        if offer_id is not None and (offer_id == offer_pkg_id or offer_id == idx):
            chosen = {
                "value_obj": parsed_value,
                "value_text": value_text,
                "amount": total,
                "base_amount": amount,
            }
            break

        # value string match
        if value_raw:
            v = str(value_raw).strip().lower()
            if v and (v == str(parsed_value).strip().lower() or v == value_text.strip().lower()):
                chosen = {
                    "value_obj": parsed_value,
                    "value_text": value_text,
                    "amount": total,
                    "base_amount": amount,
                }
                break

        # gig match
        if gig_raw not in (None, ""):
            try:
                g = float(str(gig_raw).strip())
            except Exception:
                g = None
            if g is not None and vol_num is not None:
                targets = []
                if unit == "minutes":
                    targets.append(g)
                else:
                    targets.append(g * 1000.0)
                    targets.append(g)
                for t in targets:
                    score = abs(float(vol_num) - float(t))
                    if score <= 1.0 and (best_score is None or score < best_score):
                        best_score = score
                        chosen = {
                            "value_obj": parsed_value,
                            "value_text": value_text,
                            "amount": total,
                            "base_amount": amount,
                        }

    if not chosen:
        return None, None, "Package not found for this service"

    return svc_doc, chosen, None

@agent_api_bp.route("/agent/api", methods=["GET"])
def agent_api_access():
    if not _require_customer():
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    try:
        oid = ObjectId(user_id)
    except Exception:
        return redirect(url_for("login.login"))

    doc = api_keys_col.find_one({"user_id": oid})
    api_key = doc.get("key") if doc else ""
    return render_template("agent_api_access.html", api_key=api_key)

@agent_api_bp.route("/agent/api/generate", methods=["POST"])
def agent_api_generate():
    if not _require_customer():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    user_id = session.get("user_id")
    try:
        oid = ObjectId(user_id)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_user"}), 400

    new_key = _generate_api_key(oid)
    now = datetime.utcnow()
    api_keys_col.update_one(
        {"user_id": oid},
        {"$set": {"key": new_key, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return jsonify({"ok": True, "api_key": new_key})

@agent_api_bp.route("/agent/api/docs", methods=["GET"])
def agent_api_docs():
    if not _require_customer():
        return redirect(url_for("login.login"))
    return render_template("agent_api_docs.html")


# ===== Agent API (JSON) ======================================================

_PHONE_RE = re.compile(r"^0\d{9}$")

def _payload_dict():
    if request.method == "GET":
        return dict(request.args) if request.args else {}
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict() if request.form else {}


@agent_api_bp.route("/api/packages.php", methods=["GET"])
def agent_api_packages():
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    regular_packages, bigtime_packages = _build_packages_for_user(user_oid)
    return jsonify(
        {
            "status": "success",
            "message": "Successful",
            "data": {"data": {"regular_packages": regular_packages, "bigtime_packages": bigtime_packages}},
        }
    )


@agent_api_bp.route("/api/wallet.php", methods=["GET"])
def agent_api_wallet():
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    bal_doc = balances_col.find_one({"user_id": user_oid}) or {}
    bal = float(bal_doc.get("amount", 0.0) or 0.0)
    return jsonify({"status": "success", "message": "Successful", "data": {"wallet": round(bal, 2)}})


def _api_place_order(mode: str):
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    payload = _payload_dict()
    phone = (payload.get("recipient_number") or payload.get("phone") or "").strip()
    if not _PHONE_RE.match(phone):
        return jsonify({"status": 400, "message": "Invalid recipient_number"}), 400

    svc_doc, offer, err = _resolve_service_offer(payload, user_oid, mode)
    if err:
        return jsonify({"status": 400, "message": err}), 400
    if not offer.get("amount") or float(offer.get("amount") or 0.0) <= 0:
        return jsonify({"status": 400, "message": "Invalid package amount"}), 400

    api_reference_id = _generate_api_reference(phone)
    client_request_id = (payload.get("client_request_id") or "").strip()

    cart_item = {
        "serviceId": str(svc_doc["_id"]),
        "serviceName": svc_doc.get("name") or svc_doc.get("network") or "",
        "phone": phone,
        "value": offer.get("value_text"),
        "value_obj": offer.get("value_obj"),
        "base_amount": float(offer.get("base_amount") or 0.0),
        "amount": float(offer.get("amount") or 0.0),
        "total": float(offer.get("amount") or 0.0),
    }

    resp = _process_checkout_core(
        user_oid,
        {"cart": [cart_item], "method": "wallet"},
        client_request_id_override=client_request_id,
        api_reference_id=api_reference_id,
        api_mode=mode,
        api_source="agent_api",
    )

    # unwrap Flask response
    if isinstance(resp, tuple):
        flask_resp, status = resp
    else:
        flask_resp, status = resp, getattr(resp, "status_code", 200)

    payload_out = {}
    try:
        payload_out = flask_resp.get_json(silent=True) or {}
    except Exception:
        payload_out = {}

    if status == 200 and payload_out.get("success"):
        return (
            jsonify(
                {
                    "status": 200,
                    "message": "Order recorded successfully",
                    "reference_id": api_reference_id,
                    "order_id": payload_out.get("order_id"),
                }
            ),
            200,
        )

    message = payload_out.get("message") or "Order failed"
    code = 400 if status in (400, 401, 403) else 500
    return jsonify({"status": code, "message": message}), code


@agent_api_bp.route("/api/send_order.php", methods=["POST"])
def agent_api_send_order():
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    payload = _payload_dict()
    phone = (payload.get("recipient_number") or payload.get("phone") or "").strip()
    if not _PHONE_RE.match(phone):
        return jsonify({"status": 400, "message": "Invalid recipient_number"}), 400

    svc_doc, offer, err = _resolve_service_offer(payload, user_oid, None)
    if err:
        return jsonify({"status": 400, "message": err}), 400
    if not offer.get("amount") or float(offer.get("amount") or 0.0) <= 0:
        return jsonify({"status": 400, "message": "Invalid package amount"}), 400

    api_reference_id = _generate_api_reference(phone)
    client_request_id = (payload.get("client_request_id") or "").strip()

    cart_item = {
        "serviceId": str(svc_doc["_id"]),
        "serviceName": svc_doc.get("name") or svc_doc.get("network") or "",
        "phone": phone,
        "value": offer.get("value_text"),
        "value_obj": offer.get("value_obj"),
        "base_amount": float(offer.get("base_amount") or 0.0),
        "amount": float(offer.get("amount") or 0.0),
        "total": float(offer.get("amount") or 0.0),
    }

    resp = _process_checkout_core(
        user_oid,
        {"cart": [cart_item], "method": "wallet"},
        client_request_id_override=client_request_id,
        api_reference_id=api_reference_id,
        api_mode="single",
        api_source="agent_api",
    )

    if isinstance(resp, tuple):
        flask_resp, status = resp
    else:
        flask_resp, status = resp, getattr(resp, "status_code", 200)

    payload_out = {}
    try:
        payload_out = flask_resp.get_json(silent=True) or {}
    except Exception:
        payload_out = {}

    if status == 200 and payload_out.get("success"):
        return (
            jsonify(
                {
                    "status": 200,
                    "message": "Order recorded successfully",
                    "reference_id": api_reference_id,
                    "order_id": payload_out.get("order_id"),
                }
            ),
            200,
        )

    message = payload_out.get("message") or "Order failed"
    code = 400 if status in (400, 401, 403) else 500
    return jsonify({"status": code, "message": message}), code


@agent_api_bp.route("/api/initiate.php", methods=["POST"])
def agent_api_initiate():
    return _api_place_order("regular")


@agent_api_bp.route("/api/special.php", methods=["POST"])
def agent_api_special():
    return _api_place_order("bigtime")


def _api_status_response(user_oid: ObjectId, reference_id: str, mode: str):
    if not reference_id:
        if request.is_json:
            body = request.get_json(silent=True) or {}
            reference_id = (body.get("reference_id") or "").strip()
        else:
            reference_id = (request.form.get("reference_id") if request.form else "") or ""
            reference_id = reference_id.strip()

    if not reference_id:
        return jsonify({"status": 400, "success": False, "message": "reference_id required"}), 400

    order = orders_col.find_one({"api_reference_id": reference_id, "user_id": user_oid})
    if not order:
        return jsonify({"status": 102, "success": False, "message": "Agent not found"}), 404

    items = order.get("items") or []
    item0 = items[0] if items and isinstance(items[0], dict) else {}

    created_at = order.get("created_at")
    order_date = created_at.strftime("%A, %B %d, %Y") if isinstance(created_at, datetime) else ""
    order_time = created_at.strftime("%H:%M:%S %p") if isinstance(created_at, datetime) else ""

    price = item0.get("amount") or order.get("charged_amount") or order.get("total_amount") or 0.0
    status_txt = (item0.get("line_status") or order.get("status") or "Pending").capitalize()

    return jsonify(
        {
            "status": 200,
            "success": True,
            "message": "Order found",
            "data": {
                "beneficiary": item0.get("phone") or "",
                "gig": item0.get("value") or "",
                "network": item0.get("serviceName") or "",
                "order_date": order_date,
                "order_time": order_time,
                "price": float(price),
                "order_status": status_txt,
            },
        }
    )


@agent_api_bp.route("/api/response_regular.php", methods=["GET"])
def agent_api_response_regular():
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    reference_id = (request.args.get("reference_id") or "").strip()
    return _api_status_response(user_oid, reference_id, "regular")


@agent_api_bp.route("/api/response_big_time.php", methods=["GET"])
def agent_api_response_big_time():
    user_oid, err_resp, err_code = _api_auth()
    if err_resp:
        return err_resp, err_code

    reference_id = (request.args.get("reference_id") or "").strip()
    return _api_status_response(user_oid, reference_id, "bigtime")
