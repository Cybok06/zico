from __future__ import annotations

from typing import Any, Dict

from db import db
from tenant import resolve_admin_id_for_user_id, to_object_id

users_col = db["users"]
referrals_col = db["referrals"]
auth_pages_col = db["auth_pages"]

DEFAULT_LOGO_URL = "https://imagedelivery.net/h9fmMoa1o2c2P55TcWJGOg/f0e6ee84-1110-4d96-0636-8814ce177a00/public"
DEFAULT_BRAND_NAME = "AZICO"


def _display_name(user_doc: Dict[str, Any] | None) -> str:
    doc = user_doc or {}
    for key in ("business_name", "full_name", "name", "username"):
        value = str(doc.get(key) or "").strip()
        if value:
            return value
    first = str(doc.get("first_name") or "").strip()
    last = str(doc.get("last_name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    email = str(doc.get("email") or "").strip()
    if email:
        return email.split("@", 1)[0]
    return DEFAULT_BRAND_NAME


def default_signup_branding() -> Dict[str, str]:
    subtitle = "Register as an admin to access services and manage your network."
    return {
        "brand_name": DEFAULT_BRAND_NAME,
        "logo_url": DEFAULT_LOGO_URL,
        "page_title": f"{DEFAULT_BRAND_NAME} | Create Account",
        "page_heading": "Create Admin Account",
        "page_subtitle": subtitle,
        "meta_description": subtitle,
        "slug": "",
    }


def signup_branding_for_admin(admin_id: Any) -> Dict[str, str]:
    branding = default_signup_branding()
    admin_oid = to_object_id(admin_id)
    if not admin_oid:
        return branding

    auth_doc = auth_pages_col.find_one(
        {"admin_id": admin_oid},
        {"business_name": 1, "logo_url": 1, "hero_text": 1, "slug": 1},
    ) or {}
    admin_doc = users_col.find_one(
        {"_id": admin_oid},
        {
            "business_name": 1,
            "full_name": 1,
            "name": 1,
            "username": 1,
            "first_name": 1,
            "last_name": 1,
            "email": 1,
        },
    ) or {}

    brand_name = str(auth_doc.get("business_name") or _display_name(admin_doc) or branding["brand_name"]).strip()
    subtitle = str(
        auth_doc.get("hero_text")
        or f"Register through {brand_name} to access services and manage your network."
    ).strip()
    if not subtitle:
        subtitle = branding["page_subtitle"]

    branding.update(
        {
            "brand_name": brand_name or branding["brand_name"],
            "logo_url": str(auth_doc.get("logo_url") or branding["logo_url"]).strip() or branding["logo_url"],
            "page_title": f"{brand_name or branding['brand_name']} | Create Account",
            "page_heading": "Create Admin Account",
            "page_subtitle": subtitle,
            "meta_description": subtitle,
            "slug": str(auth_doc.get("slug") or "").strip(),
        }
    )
    return branding


def signup_branding_for_referral_code(referral_code: str | None) -> Dict[str, str]:
    code = (referral_code or "").strip().upper()
    if not code:
        return default_signup_branding()

    ref_doc = referrals_col.find_one({"ref_code": code}, {"admin_id": 1, "user_id": 1}) or {}
    admin_id = to_object_id(ref_doc.get("admin_id")) or resolve_admin_id_for_user_id(users_col, ref_doc.get("user_id"))
    return signup_branding_for_admin(admin_id)
