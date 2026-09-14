# routes/referral.py
from flask import Blueprint, render_template, session, redirect, url_for, flash, request
from bson.objectid import ObjectId
from datetime import datetime
import random, string
from db import db
from tenant import resolve_admin_id_from_user_doc
from referral_branding import signup_branding_for_admin

referral_bp = Blueprint("referral", __name__)
referrals_col = db["referrals"]
users_col = db["users"]
auth_pages_col = db["auth_pages"]
ADMIN_PUBLIC_HOST = "azico.site"
AGENT_PUBLIC_HOST = "zishop.site"


def _request_scheme() -> str:
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    if forwarded in {"http", "https"}:
        return forwarded
    return request.scheme or "https"


def _absolute_public_url(host: str, endpoint: str, **values) -> str:
    path = url_for(endpoint, _external=False, **values)
    return f"{_request_scheme()}://{host}{path}"

def generate_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

@referral_bp.route("/referral/invite")
def generate_invite():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please login to access referral.", "warning")
        return redirect(url_for("login.login"))

    user = users_col.find_one({"_id": ObjectId(user_id)})
    if not user:
        flash("User not found", "danger")
        return redirect(url_for("login.login"))

    admin_id = resolve_admin_id_from_user_doc(user)
    if not admin_id:
        flash("User is not mapped to an admin.", "danger")
        return redirect(url_for("login.login"))

    # Check if referral already exists
    existing = referrals_col.find_one({"user_id": ObjectId(user_id), "admin_id": admin_id})
    if existing:
        code = existing["ref_code"]
    else:
        code = generate_code()
        referrals_col.insert_one({
            "user_id": ObjectId(user_id),
            "admin_id": admin_id,
            "ref_code": code,
            "created_at": datetime.utcnow()
        })

    # Build full link
    auth_page = auth_pages_col.find_one({"admin_id": admin_id}, {"slug": 1})
    signup_slug = (auth_page or {}).get("slug")
    if signup_slug:
        invite_link = _absolute_public_url(
            AGENT_PUBLIC_HOST,
            "admin_auth_pages.branded_signup",
            slug=signup_slug,
            ref=code,
        )
    else:
        invite_link = _absolute_public_url(ADMIN_PUBLIC_HOST, "signup.signup", ref=code)
    branding = signup_branding_for_admin(admin_id)

    return render_template("invite.html", invite_link=invite_link, code=code, user=user, branding=branding)
