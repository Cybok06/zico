# login_logs.py
from flask import Blueprint, render_template, request, session, redirect, url_for
from db import db
from datetime import datetime, timedelta
from typing import Dict, Any, List
import math
from bson import ObjectId
from tenant import current_admin_id_from_session

login_logs_bp = Blueprint("login_logs", __name__)
login_logs_col = db["login_logs"]
activity_logs_col = db["activity_logs"]
users_col = db["users"]

ADMIN_LOG_ROLES = ["admin", "main_admin", "superadmin", "super_admin", "professional_admin"]


def _id_variants(value):
    variants = []
    if not value:
        return variants
    variants.append(value)
    variants.append(str(value))
    oid = value if isinstance(value, ObjectId) else None
    if oid is None:
        try:
            oid = ObjectId(str(value))
        except Exception:
            oid = None
    if oid is not None and oid not in variants:
        variants.append(oid)
    return variants


def _parse_ymd(s: str):
    """Parse 'YYYY-MM-DD' -> datetime (naive UTC). Returns None on failure."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except Exception:
        return None


def _build_date_filter(start_date: str | None, end_date: str | None) -> Dict[str, Any]:
    """
    Build a Mongo filter on created_at using inclusive start (>=) and exclusive end (< next day).
    """
    filt: Dict[str, Any] = {}
    start_dt = _parse_ymd(start_date) if start_date else None
    end_dt = _parse_ymd(end_date) if end_date else None

    # If both provided and out of order, swap
    if start_dt and end_dt and start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    if start_dt and end_dt:
        filt["created_at"] = {"$gte": start_dt, "$lt": end_dt + timedelta(days=1)}
    elif start_dt:
        filt["created_at"] = {"$gte": start_dt}
    elif end_dt:
        filt["created_at"] = {"$lt": end_dt + timedelta(days=1)}

    return filt


def _daily_counts(col, base_filter: Dict[str, Any], days: int = 14) -> Dict[str, List[Any]]:
    today = datetime.utcnow().date()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    labels = [d.strftime("%b %d") for d in dates]
    counts = [0 for _ in dates]
    if not dates:
        return {"labels": [], "counts": []}

    start = datetime.combine(dates[0], datetime.min.time())
    end = datetime.combine(dates[-1], datetime.min.time()) + timedelta(days=1)

    pipeline = [
        {"$match": {**base_filter, "created_at": {"$gte": start, "$lt": end}}},
        {"$project": {"d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}}}},
        {"$group": {"_id": "$d", "c": {"$sum": 1}}},
    ]
    by_day: Dict[str, int] = {}
    try:
        for row in col.aggregate(pipeline):
            dt = row.get("_id")
            if isinstance(dt, datetime):
                by_day[dt.date().isoformat()] = int(row.get("c", 0) or 0)
    except Exception:
        pass

    for i, d in enumerate(dates):
        counts[i] = by_day.get(d.isoformat(), 0)

    return {"labels": labels, "counts": counts}


@login_logs_bp.route("/admin/login-logs")
@login_logs_bp.route("/admin/logs-activities")
def view_login_logs():
    # Admin-only
    if not session.get("admin_logged_in") and (session.get("role") not in {"admin", "main_admin"}):
        return redirect(url_for("login.login"))

    role = (session.get("role") or "").strip().lower()
    is_main_admin = role == "main_admin"
    admin_oid = current_admin_id_from_session(session)

    # Query params
    tab = (request.args.get("tab") or "logins").strip().lower()
    if tab not in {"logins", "activities"}:
        tab = "logins"
    start_date = (request.args.get("start_date") or "").strip() or None
    end_date = (request.args.get("end_date") or "").strip() or None
    page = request.args.get("page", "1")
    per_page = request.args.get("per_page", "20")
    action_filter = (request.args.get("action") or "").strip().lower()

    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        per_page = min(100, max(5, int(per_page)))  # clamp 5..100
    except Exception:
        per_page = 20

    date_filter = _build_date_filter(start_date, end_date)

    login_scope: Dict[str, Any] = {}
    activity_scope: Dict[str, Any] = {}
    if not is_main_admin:
        if not admin_oid:
            login_scope = {"_id": {"$exists": False}}
            activity_scope = {"_id": {"$exists": False}}
        else:
            admin_id_variants = _id_variants(admin_oid)
            login_scope = {
                "$or": [
                    {"user_id": {"$in": admin_id_variants}},
                    {
                        "admin_id": {"$in": admin_id_variants},
                        "role": {"$nin": ADMIN_LOG_ROLES},
                    },
                ]
            }
            activity_scope = {"admin_id": admin_oid}

    filt_login = {**login_scope, **date_filter}
    filt_activity = {**activity_scope, **date_filter}
    if action_filter:
        filt_activity["action"] = action_filter

    # Count then fetch
    try:
        total_login_count = login_logs_col.count_documents(filt_login)
    except Exception:
        total_login_count = 0

    try:
        total_activity_count = activity_logs_col.count_documents(filt_activity)
    except Exception:
        total_activity_count = 0

    total_count = total_login_count if tab == "logins" else total_activity_count

    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    logs = []
    activities = []
    if tab == "logins":
        try:
            logs = list(
                login_logs_col.find(filt_login)
                .sort("created_at", -1)
                .skip(skip)
                .limit(per_page)
            )
        except Exception:
            logs = []
    else:
        try:
            activities = list(
                activity_logs_col.find(filt_activity)
                .sort("created_at", -1)
                .skip(skip)
                .limit(per_page)
            )
        except Exception:
            activities = []

    # Admin map for main admin view
    admin_map: Dict[str, Dict[str, Any]] = {}
    if is_main_admin:
        admin_ids = list({a.get("admin_id") for a in activities if a.get("admin_id")})
        try:
            admin_map = {
                str(u["_id"]): u
                for u in users_col.find({"_id": {"$in": admin_ids}}, {"first_name": 1, "last_name": 1, "username": 1, "email": 1})
            }
        except Exception:
            admin_map = {}

    # Build a small window of page numbers around current (do it in Python to avoid Jinja max/min)
    start_win = 1 if page <= 3 else page - 2
    end_win = min(total_pages, start_win + 4)
    start_win = max(1, end_win - 4)
    page_numbers: List[int] = list(range(start_win, end_win + 1))

    # Summary cards (today)
    today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    today_end = today_start + timedelta(days=1)
    try:
        today_login_count = login_logs_col.count_documents({**login_scope, "created_at": {"$gte": today_start, "$lt": today_end}})
    except Exception:
        today_login_count = 0
    try:
        today_activity_count = activity_logs_col.count_documents({**activity_scope, "created_at": {"$gte": today_start, "$lt": today_end}})
    except Exception:
        today_activity_count = 0

    login_chart = _daily_counts(login_logs_col, login_scope, days=14)
    activity_chart = _daily_counts(activity_logs_col, activity_scope, days=14)

    return render_template(
        "login_logs.html",
        tab=tab,
        logs=logs,
        activities=activities,
        admin_map=admin_map,
        is_main_admin=is_main_admin,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        total_login_count=total_login_count,
        total_activity_count=total_activity_count,
        today_login_count=today_login_count,
        today_activity_count=today_activity_count,
        page_numbers=page_numbers,
        start_date=start_date or "",
        end_date=end_date or "",
        action_filter=action_filter,
        chart_labels=login_chart["labels"],
        chart_logins=login_chart["counts"],
        chart_activities=activity_chart["counts"],
        activity_actions=["order_placed", "store_order_placed", "announcement_created", "complaint_submitted"],
    )
