from __future__ import annotations

from typing import Any, Dict, Optional

from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, BadTimeSignature, SignatureExpired


TOKEN_SALT = "store-admin-override.v1"
TOKEN_MAX_AGE_SECONDS = 60 * 60 * 2


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=TOKEN_SALT)


def generate_admin_override_token(
    *,
    complaint_id: str,
    store_slug: str,
    actor_user_id: str,
    actor_role: str,
    admin_id: Optional[str] = None,
) -> str:
    payload = {
        "complaint_id": str(complaint_id or "").strip(),
        "store_slug": str(store_slug or "").strip(),
        "actor_user_id": str(actor_user_id or "").strip(),
        "actor_role": str(actor_role or "").strip().lower(),
        "admin_id": str(admin_id or "").strip(),
    }
    return _serializer().dumps(payload)


def verify_admin_override_token(token: str, *, max_age: int = TOKEN_MAX_AGE_SECONDS) -> Optional[Dict[str, Any]]:
    token = str(token or "").strip()
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, BadTimeSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    return data
