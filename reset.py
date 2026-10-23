"""Admin-issued, single-use password reset links."""
from datetime import datetime, timedelta
import hashlib
import secrets

from flask import Blueprint, render_template, request, jsonify, session, redirect, flash
from werkzeug.security import generate_password_hash

from db import db
from tenant import ADMIN_ROLES, to_object_id

reset_bp = Blueprint("reset", __name__, template_folder="templates")
users_col = db["users"]
password_resets_col = db["password_resets"]
auth_pages_col = db["auth_pages"]
AUTH_PUBLIC_HOST = "azico.site"
AGENT_PUBLIC_HOST = "zishop.site"
RESET_HOURS = 12


def _now():
    return datetime.utcnow()


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _login_url(user):
    if user.get("role") in {"agent", "customer"}:
        admin_id = to_object_id(user.get("admin_id"))
        page = auth_pages_col.find_one({"admin_id": admin_id}) if admin_id else None
        if not page or not page.get("slug"):
            return None
        from urllib.parse import quote
        return f"https://{AGENT_PUBLIC_HOST}/{quote(page['slug'], safe='')}/login"
    return f"https://{AUTH_PUBLIC_HOST}/login"


def _target_query(user):
    query = {"_id": user["_id"], "role": user.get("role"), "deleted": {"$ne": True}}
    if user.get("role") in {"agent", "customer"}:
        query["admin_id"] = user.get("admin_id")
    return query


@reset_bp.after_request
def private_reset_response(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@reset_bp.route("/admin/reset/generate", methods=["POST"])
def admin_generate_reset():
    actor_id = to_object_id(session.get("user_id"))
    role = session.get("role")
    if role not in ADMIN_ROLES or not actor_id:
        return jsonify(status="error", message="Unauthorized"), 403
    actor = users_col.find_one({"_id": actor_id, "role": role, "deleted": {"$ne": True}})
    if not actor:
        return jsonify(status="error", message="Unauthorized"), 403
    payload = request.get_json(silent=True) or request.form
    target_id = to_object_id(payload.get("user_id"))
    if not target_id:
        return jsonify(status="error", message="Invalid user id"), 400
    query = {"_id": target_id, "deleted": {"$ne": True}}
    if role == "main_admin":
        query["role"] = {"$in": list(ADMIN_ROLES - {"main_admin"}) + ["agent", "customer"]}
    else:
        query.update(role={"$in": ["agent", "customer"]}, admin_id=actor_id)
    user = users_col.find_one(query)
    if not user:
        return jsonify(status="error", message="Account not found or access denied"), 404
    if not _login_url(user):
        return jsonify(status="error", message="Set up this account's branded login page before creating a reset link."), 400

    now = _now()
    expires_at = now + timedelta(hours=RESET_HOURS)
    raw_token = secrets.token_urlsafe(32)
    # Keep the active token on the account so issuing another link immediately
    # invalidates older links, including concurrent generation requests.
    token_hash = _hash_token(raw_token)
    password_resets_col.insert_one({
        "user_id": user["_id"], "role": user["role"], "admin_id": user.get("admin_id"),
        "token_hash": token_hash, "created_at": now, "expires_at": expires_at,
        "used_at": None, "created_by": actor_id,
    })
    result = users_col.update_one(_target_query(user), {"$set": {"password_reset_token_hash": token_hash}})
    if not result.matched_count:
        return jsonify(status="error", message="Account no longer available"), 409
    return jsonify(status="success", reset_url=f"https://{AUTH_PUBLIC_HOST}/reset/{raw_token}",
                   expires_at=expires_at.isoformat() + "Z")


def _active_reset(token):
    token_hash = _hash_token(token)
    rec = password_resets_col.find_one({"token_hash": token_hash, "used_at": None, "expires_at": {"$gt": _now()}})
    if not rec:
        return None, None
    query = {"_id": rec["user_id"], "role": rec.get("role"), "deleted": {"$ne": True},
             "password_reset_token_hash": token_hash}
    if rec.get("role") in {"agent", "customer"}:
        query["admin_id"] = rec.get("admin_id")
    user = users_col.find_one(query)
    return rec, user


@reset_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_form(token):
    rec, user = _active_reset(token)
    login_url = _login_url(user) if user else None
    if not user or not login_url:
        return render_template("reset.html", invalid=True), 400
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        error = None
        if len(password) < 6:
            error = "Password must contain at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        if error:
            flash(error, "danger")
            return render_template("reset.html", token=token, invalid=False), 400
        password_hash = generate_password_hash(password)
        # Atomically consume the active token AND change the password. A second
        # request or a replaced token cannot overwrite the new password.
        query = _target_query(user)
        query["password_reset_token_hash"] = rec["token_hash"]
        if rec["expires_at"] <= _now():
            return render_template("reset.html", invalid=True), 400
        result = users_col.update_one(query, {
            "$set": {"password": password_hash, "updated_at": _now()},
            "$unset": {"password_reset_token_hash": ""},
        })
        if not result.matched_count:
            return render_template("reset.html", invalid=True), 400
        password_resets_col.update_one({"_id": rec["_id"]}, {"$set": {"used_at": _now()}})
        return redirect(login_url, code=303)
    return render_template("reset.html", token=token, invalid=False)
