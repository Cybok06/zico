from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for, jsonify

from db import db
from tenant import current_admin_id_from_session
from cloudflare_images import upload_image_to_cloudflare
from activity_log import log_activity

announcements_bp = Blueprint("announcements", __name__)

announcements_col = db["announcements"]
comments_col = db["announcement_comments"]
announcement_acks_col = db["announcement_acknowledgements"]
users_col = db["users"]
MAX_ANNOUNCEMENT_IMAGES = 20
ADMIN_VIEWER_ROLES = {
    "admin",
    "main_admin",
    "super_admin",
    "superadmin",
    "professional_admin",
    "super_professional",
}


def _now() -> datetime:
    return datetime.utcnow()


def _to_objectid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalize_role(role: str | None) -> str:
    return (role or "").strip().lower()


def _is_admin_viewer_role(role: str | None) -> bool:
    return _normalize_role(role) in ADMIN_VIEWER_ROLES


def _coerce_utc_naive(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "User"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    first = (user_doc.get("first_name") or "").strip()
    last = (user_doc.get("last_name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "User"


def _global_clause() -> Dict[str, Any]:
    return {"$or": [{"admin_id": {"$exists": False}}, {"admin_id": None}]}


def _not_expired_clause(now: Optional[datetime] = None) -> Dict[str, Any]:
    ref = _coerce_utc_naive(now) or _now()
    return {
        "$or": [
            {"delete_at": {"$exists": False}},
            {"delete_at": None},
            {"delete_at": {"$gt": ref}},
        ]
    }


def _is_expired_announcement(ann: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    delete_at = _coerce_utc_naive(ann.get("delete_at"))
    ref = _coerce_utc_naive(now) or _now()
    return bool(delete_at and delete_at <= ref)


def _viewer_created_at(viewer_id: Any) -> Optional[datetime]:
    viewer_oid = _to_objectid(viewer_id)
    if not viewer_oid:
        return None
    try:
        user_doc = users_col.find_one({"_id": viewer_oid}, {"created_at": 1})
    except Exception:
        user_doc = None
    return _coerce_utc_naive((user_doc or {}).get("created_at"))


def _viewer_missed_announcement(
    ann: Dict[str, Any],
    role: str,
    viewer_created_at: Optional[datetime],
) -> bool:
    role = _normalize_role(role)
    if _is_admin_viewer_role(role):
        return False
    joined_at = _coerce_utc_naive(viewer_created_at)
    ann_created_at = _coerce_utc_naive(ann.get("created_at"))
    if not joined_at or not ann_created_at:
        return False
    return ann_created_at < joined_at


def cleanup_expired_announcements(now: Optional[datetime] = None) -> int:
    ref = _coerce_utc_naive(now) or _now()
    try:
        expired_ids = [
            d.get("_id")
            for d in announcements_col.find({"delete_at": {"$lte": ref}}, {"_id": 1})
            if d.get("_id")
        ]
    except Exception:
        expired_ids = []

    if not expired_ids:
        return 0

    try:
        comments_col.delete_many({"announcement_id": {"$in": expired_ids}})
    except Exception:
        pass
    try:
        announcement_acks_col.delete_many({"announcement_id": {"$in": expired_ids}})
    except Exception:
        pass

    try:
        result = announcements_col.delete_many({"_id": {"$in": expired_ids}})
        return int(getattr(result, "deleted_count", 0) or len(expired_ids))
    except Exception:
        return 0


def build_visibility_query(
    role: str,
    admin_oid: Optional[ObjectId],
    viewer_created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    role = _normalize_role(role)
    global_clause = _global_clause()
    ors: List[Dict[str, Any]] = []

    if _is_admin_viewer_role(role):
        ors.append({"$and": [global_clause, {"audience": {"$in": ["admins", "all"]}}]})
        if admin_oid:
            ors.append({"admin_id": admin_oid})
    else:
        ors.append({"$and": [global_clause, {"audience": "all"}]})
        if admin_oid:
            ors.append({"admin_id": admin_oid, "audience": "agents"})

    if not ors:
        return {"_id": {"$exists": False}}

    filters: List[Dict[str, Any]] = [{"$or": ors}, _not_expired_clause()]
    joined_at = _coerce_utc_naive(viewer_created_at)
    if joined_at and not _is_admin_viewer_role(role):
        filters.append({"created_at": {"$gte": joined_at}})
    return {"$and": filters}


def can_view_announcement(
    ann: Dict[str, Any],
    role: str,
    admin_oid: Optional[ObjectId],
    viewer_created_at: Optional[datetime] = None,
) -> bool:
    role = _normalize_role(role)
    if _is_expired_announcement(ann):
        return False
    if _viewer_missed_announcement(ann, role, viewer_created_at):
        return False
    aud = (ann.get("audience") or "").strip().lower()
    ann_admin = _to_objectid(ann.get("admin_id"))
    is_global = ann.get("admin_id") is None or ("admin_id" not in ann)

    if _is_admin_viewer_role(role):
        if is_global:
            return aud in {"admins", "all"}
        if admin_oid and ann_admin and admin_oid == ann_admin:
            return True
        return False

    # agent/customer
    if is_global:
        return aud == "all"
    if admin_oid and ann_admin and admin_oid == ann_admin:
        return aud == "agents"
    return False


def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%d %b %Y, %I:%M %p")
    return ""


def _audience_label(audience: str | None) -> str:
    a = (audience or "").strip().lower()
    if a == "admins":
        return "Admins only"
    if a == "agents":
        return "Agents"
    if a == "all":
        return "All users"
    return "Audience"


def _scope_label(ann: Dict[str, Any]) -> str:
    if ann.get("admin_id") is None or ("admin_id" not in ann):
        return "Global"
    return "Tenant"


def _comment_view(c: Dict[str, Any], self_oid: Optional[ObjectId], viewer_role: str) -> Dict[str, Any]:
    author_id = _to_objectid(c.get("author_id"))
    show_author_name = _normalize_role(viewer_role) == "main_admin"
    return {
        "_id": str(c.get("_id") or ""),
        "body": c.get("body") or "",
        "author_name": (c.get("author_name") or "User") if show_author_name else "",
        "show_author_name": show_author_name,
        "author_role": c.get("author_role") or "",
        "created_at": c.get("created_at"),
        "created_at_fmt": _fmt_dt(c.get("created_at")),
        "is_self": bool(self_oid and author_id and self_oid == author_id),
    }


def _announcement_view(
    ann: Dict[str, Any],
    comments_by: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    ann_id = ann.get("_id")
    ann_id_str = str(ann_id) if ann_id else ""
    comments = comments_by.get(ann_id_str, [])
    image_urls = ann.get("image_urls")
    if isinstance(image_urls, str):
        image_urls = [image_urls]
    if not isinstance(image_urls, list):
        image_urls = []
    if ann.get("image_url"):
        image_urls = [ann.get("image_url")] + [u for u in image_urls if u != ann.get("image_url")]
    return {
        "_id": ann_id,
        "_id_str": ann_id_str,
        "title": ann.get("title") or "",
        "body": ann.get("body") or "",
        "image_url": ann.get("image_url") or "",
        "image_urls": image_urls,
        "post_type": (ann.get("post_type") or "announcement").strip().lower(),
        "audience": ann.get("audience") or "",
        "audience_label": _audience_label(ann.get("audience")),
        "scope_label": _scope_label(ann),
        "created_at": ann.get("created_at"),
        "created_at_fmt": _fmt_dt(ann.get("created_at")),
        "delete_at": ann.get("delete_at"),
        "delete_at_fmt": _fmt_dt(ann.get("delete_at")),
        "created_by_name": ann.get("created_by_name") or "Admin",
        "created_by_role": ann.get("created_by_role") or "",
        "allow_comments": bool(ann.get("allow_comments", True)),
        "popup": bool(ann.get("popup", False)),
        "comments": comments,
        "comment_count": len(comments),
    }


def fetch_announcements_for_view(
    role: str,
    admin_oid: Optional[ObjectId],
    self_oid: Optional[ObjectId],
    limit: int = 100,
) -> List[Dict[str, Any]]:
    role = _normalize_role(role)
    cleanup_expired_announcements()
    viewer_created_at = _viewer_created_at(self_oid)
    query = build_visibility_query(role, admin_oid, viewer_created_at)
    try:
        anns = list(announcements_col.find(query).sort("created_at", -1).limit(int(limit)))
    except Exception:
        anns = []

    ann_ids = [a.get("_id") for a in anns if a.get("_id")]
    comments_by: Dict[str, List[Dict[str, Any]]] = {}
    if ann_ids:
        try:
            comments_query: Dict[str, Any] = {"announcement_id": {"$in": ann_ids}}
            if role != "main_admin":
                comments_query["author_id"] = self_oid if self_oid else {"$exists": False}
            cur = comments_col.find(comments_query).sort("created_at", 1)
            for c in cur:
                aid = str(c.get("announcement_id") or "")
                if not aid:
                    continue
                comments_by.setdefault(aid, []).append(_comment_view(c, self_oid, role))
        except Exception:
            comments_by = {}

    return [_announcement_view(a, comments_by) for a in anns]


def _viewer_acknowledged_announcement(announcement_id: Any, viewer_id: Any) -> bool:
    ann_oid = _to_objectid(announcement_id)
    viewer_oid = _to_objectid(viewer_id)
    if not ann_oid or not viewer_oid:
        return False
    try:
        return bool(
            announcement_acks_col.find_one(
                {"announcement_id": ann_oid, "viewer_id": viewer_oid},
                {"_id": 1},
            )
        )
    except Exception:
        return False


def acknowledge_announcement_for_viewer(
    announcement_id: Any,
    role: str,
    admin_oid: Optional[ObjectId],
    viewer_id: Any,
) -> bool:
    ann_oid = _to_objectid(announcement_id)
    viewer_oid = _to_objectid(viewer_id)
    if not ann_oid or not viewer_oid:
        return False

    viewer_created_at = _viewer_created_at(viewer_oid)
    ann = announcements_col.find_one({"_id": ann_oid})
    if not ann or not can_view_announcement(ann, role, admin_oid, viewer_created_at):
        return False

    try:
        announcement_acks_col.update_one(
            {"announcement_id": ann_oid, "viewer_id": viewer_oid},
            {
                "$set": {
                    "announcement_id": ann_oid,
                    "viewer_id": viewer_oid,
                    "viewer_role": _normalize_role(role),
                    "admin_id": admin_oid,
                    "acknowledged_at": _now(),
                }
            },
            upsert=True,
        )
        return True
    except Exception:
        return False


def get_popup_announcement(role: str, admin_oid: Optional[ObjectId], viewer_id: Any = None) -> Optional[Dict[str, Any]]:
    cleanup_expired_announcements()
    viewer_created_at = _viewer_created_at(viewer_id)
    base_q = build_visibility_query(role, admin_oid, viewer_created_at)
    q = {"$and": [base_q, {"popup": True}]}
    try:
        ann_docs = list(announcements_col.find(q).sort("created_at", -1).limit(20))
    except Exception:
        ann_docs = []

    ann_doc = None
    for candidate in ann_docs:
        if not _viewer_acknowledged_announcement(candidate.get("_id"), viewer_id):
            ann_doc = candidate
            break

    return _announcement_view(ann_doc, comments_by={}) if ann_doc else None


def count_new_announcements_today(role: str, admin_oid: Optional[ObjectId], viewer_id: Any = None) -> int:
    viewer_created_at = _viewer_created_at(viewer_id)
    base_q = build_visibility_query(role, admin_oid, viewer_created_at)
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    q = {"$and": [base_q, {"created_at": {"$gte": day_start}}]}
    try:
        return int(announcements_col.count_documents(q))
    except Exception:
        return 0


@announcements_bp.route("/api/announcements/<announcement_id>/acknowledge", methods=["POST"])
def acknowledge_announcement(announcement_id: str):
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    role = _normalize_role(session.get("role"))
    admin_oid = current_admin_id_from_session(session)
    ok = acknowledge_announcement_for_viewer(announcement_id, role, admin_oid, session.get("user_id"))
    if not ok:
        return jsonify({"success": False, "message": "Unable to acknowledge announcement"}), 400
    return jsonify({"success": True})


@announcements_bp.route("/api/announcements/upload_image", methods=["POST"])
def upload_announcement_image():
    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Login required"}), 401

    role = _normalize_role(session.get("role"))
    if role not in {"admin", "main_admin"}:
        return jsonify({"success": False, "error": "Not allowed"}), 403

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No file part in request"}), 400

    image = request.files["image"]
    ok, payload, code = upload_image_to_cloudflare(
        image,
        owner_id=str(session.get("user_id")),
        module="announcements",
        variant=request.args.get("variant"),
        content_length=request.content_length,
    )
    if not ok:
        return jsonify({"success": False, **payload}), code

    return jsonify({"success": True, **payload})


@announcements_bp.route("/announcements")
def list_announcements():
    if not session.get("user_id"):
        return redirect(url_for("login.login"))

    role = _normalize_role(session.get("role"))
    admin_oid = current_admin_id_from_session(session)
    self_oid = _to_objectid(session.get("user_id"))

    announcements = fetch_announcements_for_view(role, admin_oid, self_oid)
    can_create = role == "main_admin"
    is_main_admin = role == "main_admin"

    return render_template(
        "announcements.html",
        announcements=announcements,
        can_create=can_create,
        is_main_admin=is_main_admin,
        role=role,
    )


@announcements_bp.route("/announcements/create", methods=["POST"])
def create_announcement():
    if not session.get("user_id"):
        return redirect(url_for("login.login"))

    role = _normalize_role(session.get("role"))
    if role != "main_admin":
        flash("You are not allowed to create announcements.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    image_url_raw = (request.form.get("image_url") or "").strip()
    image_urls_json = (request.form.get("image_urls_json") or "").strip()
    delete_at_raw = (request.form.get("delete_at") or request.form.get("delete_at_local") or "").strip()
    popup = bool(request.form.get("popup"))
    post_type = (request.form.get("post_type") or "announcement").strip().lower()

    if not title or not body:
        flash("Title and message are required.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    if len(title) > 140:
        flash("Title is too long (max 140 characters).", "danger")
        return redirect(url_for("announcements.list_announcements"))
    if len(body) > 4000:
        flash("Message is too long (max 4000 characters).", "danger")
        return redirect(url_for("announcements.list_announcements"))

    delete_at = _coerce_utc_naive(delete_at_raw)
    if delete_at_raw and not delete_at:
        flash("Delete time is invalid.", "danger")
        return redirect(url_for("announcements.list_announcements"))
    if delete_at and delete_at <= _now():
        flash("Delete time must be in the future.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    if post_type not in {"announcement", "ad"}:
        post_type = "announcement"

    # Optional images: Cloudflare URLs (from client upload) or direct URLs
    image_urls: List[str] = []
    if image_urls_json:
        try:
            import json as _json
            parsed = _json.loads(image_urls_json)
            if isinstance(parsed, list):
                image_urls = [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            image_urls = []

    if image_url_raw:
        image_urls.append(image_url_raw)

    cleaned_urls: List[str] = []
    for u in image_urls:
        if u.startswith("/uploads/") or u.startswith("http://") or u.startswith("https://"):
            if u not in cleaned_urls:
                cleaned_urls.append(u)
        else:
            flash("Image URL must start with http(s):// or /uploads/.", "danger")
            return redirect(url_for("announcements.list_announcements"))
    image_urls = cleaned_urls[:MAX_ANNOUNCEMENT_IMAGES]

    image_url = image_urls[0] if image_urls else ""

    admin_oid = current_admin_id_from_session(session)
    audience = (request.form.get("audience") or "all").strip().lower()
    if audience not in {"admins", "all"}:
        audience = "all"
    allow_comments = bool(request.form.get("allow_comments"))
    admin_id = None

    user_doc = None
    try:
        user_doc = users_col.find_one({"_id": _to_objectid(session.get("user_id"))}, {
            "full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1
        })
    except Exception:
        user_doc = None

    doc = {
        "title": title,
        "body": body,
        "image_url": image_url or None,
        "image_urls": image_urls,
        "post_type": post_type,
        "audience": audience,
        "admin_id": admin_id,
        "created_by": _to_objectid(session.get("user_id")),
        "created_by_role": role,
        "created_by_name": _display_name(user_doc),
        "delete_at": delete_at,
        "allow_comments": bool(allow_comments),
        "popup": bool(popup),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "active",
    }

    try:
        announcements_col.insert_one(doc)
        try:
            log_activity(
                "announcement_created",
                actor_id=session.get("user_id"),
                actor_role=role,
                admin_id=admin_id if admin_id else session.get("user_id"),
                target_type="announcement",
                target_id=doc.get("title"),
                message="Announcement created",
                meta={
                    "audience": audience,
                    "popup": bool(popup),
                    "post_type": post_type,
                    "image_count": len(image_urls),
                    "delete_at": delete_at.isoformat() + "Z" if delete_at else None,
                },
            )
        except Exception:
            pass
        flash("Announcement published.", "success")
    except Exception:
        flash("Failed to publish announcement.", "danger")

    return redirect(url_for("announcements.list_announcements"))


@announcements_bp.route("/announcements/<announcement_id>/delete", methods=["POST"])
def delete_announcement(announcement_id: str):
    if not session.get("user_id"):
        return redirect(url_for("login.login"))

    role = _normalize_role(session.get("role"))
    if role != "main_admin":
        flash("Only main admin can delete announcements.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    ann_oid = _to_objectid(announcement_id)
    if not ann_oid:
        flash("Announcement not found.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    ann = announcements_col.find_one({"_id": ann_oid})
    if not ann:
        flash("Announcement not found or already deleted.", "warning")
        return redirect(url_for("announcements.list_announcements"))

    try:
        comments_col.delete_many({"announcement_id": ann_oid})
        announcement_acks_col.delete_many({"announcement_id": ann_oid})
        result = announcements_col.delete_one({"_id": ann_oid})
        if result.deleted_count:
            try:
                log_activity(
                    "announcement_deleted",
                    actor_id=session.get("user_id"),
                    actor_role=role,
                    admin_id=session.get("user_id"),
                    target_type="announcement",
                    target_id=str(ann_oid),
                    message="Announcement deleted",
                    meta={
                        "title": ann.get("title") or "",
                        "audience": ann.get("audience") or "",
                        "post_type": ann.get("post_type") or "announcement",
                    },
                )
            except Exception:
                pass
            flash("Announcement deleted.", "success")
        else:
            flash("Announcement not found or already deleted.", "warning")
    except Exception:
        flash("Failed to delete announcement.", "danger")

    return redirect(url_for("announcements.list_announcements"))


@announcements_bp.route("/announcements/<announcement_id>/comment", methods=["POST"])
def add_comment(announcement_id: str):
    if not session.get("user_id"):
        return redirect(url_for("login.login"))

    role = _normalize_role(session.get("role"))
    admin_oid = current_admin_id_from_session(session)
    self_oid = _to_objectid(session.get("user_id"))
    viewer_created_at = _viewer_created_at(self_oid)

    ann_oid = _to_objectid(announcement_id)
    if not ann_oid:
        flash("Announcement not found.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    ann = announcements_col.find_one({"_id": ann_oid})
    if not ann or not can_view_announcement(ann, role, admin_oid, viewer_created_at):
        flash("Announcement not available.", "danger")
        return redirect(url_for("announcements.list_announcements"))

    if not ann.get("allow_comments", True):
        flash("Comments are closed for this announcement.", "warning")
        return redirect(url_for("announcements.list_announcements") + f"#ann-{announcement_id}")

    text = (request.form.get("comment") or "").strip()
    if not text:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("announcements.list_announcements") + f"#ann-{announcement_id}")
    if len(text) > 1200:
        flash("Comment is too long (max 1200 characters).", "danger")
        return redirect(url_for("announcements.list_announcements") + f"#ann-{announcement_id}")

    user_doc = None
    try:
        user_doc = users_col.find_one({"_id": self_oid}, {
            "full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1
        })
    except Exception:
        user_doc = None

    cdoc = {
        "announcement_id": ann_oid,
        "admin_id": admin_oid,
        "author_id": self_oid,
        "author_role": role,
        "author_name": _display_name(user_doc),
        "body": text,
        "created_at": _now(),
    }

    try:
        comments_col.insert_one(cdoc)
        flash("Comment posted.", "success")
    except Exception:
        flash("Failed to post comment.", "danger")

    return redirect(url_for("announcements.list_announcements") + f"#ann-{announcement_id}")
