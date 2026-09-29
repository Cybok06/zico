from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from db import db
from tenant import ADMIN_ROLES

admin_performance_bp = Blueprint("admin_performance", __name__)

orders_col = db["orders"]
users_col = db["users"]


def _now() -> datetime:
    return datetime.utcnow()


def _display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Admin"
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
    return "Admin"


def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%d %b %Y, %I:%M %p")
    return ""


def _age_days(dt: Any) -> Optional[int]:
    if isinstance(dt, datetime):
        delta = _now() - dt
        return max(0, int(delta.total_seconds() // 86400))
    return None


def _to_objectid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except Exception:
        return None


@admin_performance_bp.route("/admin/performance")
def admin_performance():
    if (session.get("role") or "").strip().lower() != "main_admin":
        return redirect(url_for("login.login"))

    view = (request.args.get("view") or "admins").strip().lower()
    if view not in {"admins", "agents"}:
        view = "admins"

    admin_focus_id = _to_objectid(request.args.get("admin_id"))
    agent_focus_id = _to_objectid(request.args.get("agent_id"))

    admin_roles = {r for r in ADMIN_ROLES if r != "main_admin"}
    admins = list(
        users_col.find(
            {"role": {"$in": list(admin_roles)}},
            {
                "first_name": 1,
                "last_name": 1,
                "full_name": 1,
                "name": 1,
                "username": 1,
                "email": 1,
                "phone": 1,
                "role": 1,
                "admin_level": 1,
                "status": 1,
                "created_at": 1,
            },
        )
    )

    admin_ids = [a["_id"] for a in admins if a.get("_id")]
    admin_ids_str = {str(a["_id"]) for a in admins if a.get("_id")}
    admin_name_map = {str(a["_id"]): _display_name(a) for a in admins if a.get("_id")}

    # Orders stats (all-time)
    orders_stats: Dict[str, Dict[str, Any]] = {}
    if admin_ids:
        pipeline = [
            {"$match": {"admin_id": {"$in": admin_ids}}},
            {"$addFields": {
                "status_norm": {"$toLower": {"$toString": {"$ifNull": ["$status", ""]}}},
            }},
            {"$group": {
                "_id": "$admin_id",
                "total_orders": {"$sum": 1},
                "total_sales": {"$sum": {"$convert": {"input": {"$ifNull": ["$total_amount", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
                "total_charged": {"$sum": {"$convert": {"input": {"$ifNull": ["$charged_amount", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
                "total_profit": {"$sum": {"$convert": {"input": {"$ifNull": ["$profit_amount_total", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
                "first_order_at": {"$min": "$created_at"},
                "last_order_at": {"$max": "$created_at"},
                "processing_count": {"$sum": {"$cond": [{"$eq": ["$status_norm", "processing"]}, 1, 0]}},
                "pending_count": {"$sum": {"$cond": [{"$eq": ["$status_norm", "pending"]}, 1, 0]}},
                "failed_count": {"$sum": {"$cond": [{"$eq": ["$status_norm", "failed"]}, 1, 0]}},
                "refunded_count": {"$sum": {"$cond": [{"$eq": ["$status_norm", "refunded"]}, 1, 0]}},
                "delivered_count": {"$sum": {"$cond": [{"$in": ["$status_norm", ["delivered", "completed", "success"]]}, 1, 0]}},
            }},
        ]
        try:
            for row in orders_col.aggregate(pipeline):
                orders_stats[str(row["_id"])] = row
        except Exception:
            orders_stats = {}

    # Orders stats (today)
    today_start = datetime.combine(_now().date(), datetime.min.time())
    today_end = today_start + timedelta(days=1)
    orders_today: Dict[str, Dict[str, Any]] = {}
    if admin_ids:
        pipeline_today = [
            {"$match": {"admin_id": {"$in": admin_ids}, "created_at": {"$gte": today_start, "$lt": today_end}}},
            {"$group": {
                "_id": "$admin_id",
                "orders_today": {"$sum": 1},
                "sales_today": {"$sum": {"$convert": {"input": {"$ifNull": ["$total_amount", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
            }},
        ]
        try:
            for row in orders_col.aggregate(pipeline_today):
                orders_today[str(row["_id"])] = row
        except Exception:
            orders_today = {}

    # Orders stats (last 30 days)
    d30_start = _now() - timedelta(days=30)
    orders_30d: Dict[str, Dict[str, Any]] = {}
    if admin_ids:
        pipeline_30 = [
            {"$match": {"admin_id": {"$in": admin_ids}, "created_at": {"$gte": d30_start}}},
            {"$group": {
                "_id": "$admin_id",
                "orders_30d": {"$sum": 1},
                "sales_30d": {"$sum": {"$convert": {"input": {"$ifNull": ["$total_amount", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
            }},
        ]
        try:
            for row in orders_col.aggregate(pipeline_30):
                orders_30d[str(row["_id"])] = row
        except Exception:
            orders_30d = {}

    # Users stats (agents/customers)
    user_stats: Dict[str, Dict[str, Any]] = {}
    if admin_ids:
        pipeline_users = [
            {"$match": {"admin_id": {"$in": admin_ids}, "role": {"$in": ["agent", "customer"]}}},
            {"$addFields": {"status_norm": {"$toLower": {"$toString": {"$ifNull": ["$status", "active"]}}}}},
            {"$group": {
                "_id": "$admin_id",
                "agents": {"$sum": {"$cond": [{"$eq": ["$role", "agent"]}, 1, 0]}},
                "customers": {"$sum": {"$cond": [{"$eq": ["$role", "customer"]}, 1, 0]}},
                "total_users": {"$sum": 1},
                "active_users": {"$sum": {"$cond": [{"$ne": ["$status_norm", "blocked"]}, 1, 0]}},
            }},
        ]
        try:
            for row in users_col.aggregate(pipeline_users):
                user_stats[str(row["_id"])] = row
        except Exception:
            user_stats = {}

    rows: List[Dict[str, Any]] = []
    for admin in admins:
        aid = admin.get("_id")
        if not aid:
            continue
        aid_str = str(aid)
        stats = orders_stats.get(aid_str, {})
        st_today = orders_today.get(aid_str, {})
        st_30d = orders_30d.get(aid_str, {})
        ust = user_stats.get(aid_str, {})

        total_orders = int(stats.get("total_orders", 0) or 0)
        total_sales = float(stats.get("total_sales", 0) or 0.0)
        avg_order = (total_sales / total_orders) if total_orders else 0.0
        delivered = int(stats.get("delivered_count", 0) or 0)
        success_rate = (delivered / total_orders * 100.0) if total_orders else 0.0
        sales_30d = float(st_30d.get("sales_30d", 0) or 0.0)
        orders_30 = int(st_30d.get("orders_30d", 0) or 0)
        avg_sales_30d = sales_30d / 30.0
        avg_orders_30d = orders_30 / 30.0

        rows.append({
            "admin_id": aid_str,
            "admin_name": _display_name(admin),
            "admin_level": admin.get("admin_level") or admin.get("role") or "admin",
            "status": admin.get("status") or "active",
            "email": admin.get("email") or "",
            "phone": admin.get("phone") or "",
            "created_at": admin.get("created_at"),
            "created_at_fmt": _fmt_dt(admin.get("created_at")),
            "account_age_days": _age_days(admin.get("created_at")),

            "total_orders": total_orders,
            "total_sales": total_sales,
            "total_charged": float(stats.get("total_charged", 0) or 0.0),
            "total_profit": float(stats.get("total_profit", 0) or 0.0),
            "avg_order_value": avg_order,
            "orders_today": int(st_today.get("orders_today", 0) or 0),
            "sales_today": float(st_today.get("sales_today", 0) or 0.0),
            "orders_30d": orders_30,
            "sales_30d": sales_30d,
            "avg_sales_30d": avg_sales_30d,
            "avg_orders_30d": avg_orders_30d,
            "first_order_at": stats.get("first_order_at"),
            "first_order_fmt": _fmt_dt(stats.get("first_order_at")),
            "last_order_at": stats.get("last_order_at"),
            "last_order_fmt": _fmt_dt(stats.get("last_order_at")),
            "pending_count": int(stats.get("pending_count", 0) or 0),
            "processing_count": int(stats.get("processing_count", 0) or 0),
            "failed_count": int(stats.get("failed_count", 0) or 0),
            "refunded_count": int(stats.get("refunded_count", 0) or 0),
            "delivered_count": delivered,
            "success_rate": success_rate,

            "agents": int(ust.get("agents", 0) or 0),
            "customers": int(ust.get("customers", 0) or 0),
            "total_users": int(ust.get("total_users", 0) or 0),
            "active_users": int(ust.get("active_users", 0) or 0),
        })

    rows.sort(key=lambda r: r.get("total_sales", 0), reverse=True)

    summary = {
        "total_admins": len(admins),
        "active_admins_30d": sum(1 for r in rows if r.get("orders_30d", 0) > 0),
        "total_orders": sum(r.get("total_orders", 0) for r in rows),
        "total_sales": sum(r.get("total_sales", 0.0) for r in rows),
        "avg_order_value": 0.0,
        "total_users": sum(r.get("total_users", 0) for r in rows),
    }
    if summary["total_orders"]:
        summary["avg_order_value"] = summary["total_sales"] / summary["total_orders"]

    # --- Charts: 30-day totals (all admins combined) ---
    days = [(_now().date() - timedelta(days=i)) for i in range(29, -1, -1)]
    daily_labels = [d.strftime("%b %d") for d in days]
    daily_sales = [0.0 for _ in days]
    daily_orders = [0 for _ in days]

    if admin_ids:
        window_start = datetime.combine(days[0], datetime.min.time())
        window_end = datetime.combine(days[-1], datetime.min.time()) + timedelta(days=1)
        pipeline_daily = [
            {"$match": {"admin_id": {"$in": admin_ids}, "created_at": {"$gte": window_start, "$lt": window_end}}},
            {"$project": {
                "d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}},
                "amt": {"$ifNull": ["$total_amount", 0]},
            }},
            {"$group": {
                "_id": "$d",
                "sales": {"$sum": {"$convert": {"input": "$amt", "to": "double", "onError": 0, "onNull": 0}}},
                "orders": {"$sum": 1},
            }},
        ]
        try:
            by_day: Dict[str, Dict[str, Any]] = {}
            for row in orders_col.aggregate(pipeline_daily):
                dt = row.get("_id")
                if isinstance(dt, datetime):
                    key = dt.date().isoformat()
                    by_day[key] = row
            for i, d in enumerate(days):
                key = d.isoformat()
                daily_sales[i] = float(by_day.get(key, {}).get("sales", 0) or 0.0)
                daily_orders[i] = int(by_day.get(key, {}).get("orders", 0) or 0)
        except Exception:
            pass

    # --- Charts: top admins ---
    top_rows = rows[:8]
    top_admin_labels = [r.get("admin_name") or "Admin" for r in top_rows]
    top_admin_sales = [float(r.get("total_sales", 0) or 0.0) for r in top_rows]
    top_admin_orders = [int(r.get("total_orders", 0) or 0) for r in top_rows]

    # --- Sub-accounts (agents/customers) ---
    sub_accounts = list(
        users_col.find(
            {"role": {"$in": ["agent", "customer"]}},
            {
                "first_name": 1,
                "last_name": 1,
                "full_name": 1,
                "name": 1,
                "username": 1,
                "email": 1,
                "phone": 1,
                "role": 1,
                "stage_label": 1,
                "status": 1,
                "admin_id": 1,
                "created_at": 1,
            },
        )
    )
    sub_ids = [u["_id"] for u in sub_accounts if u.get("_id")]
    sub_name_map = {str(u["_id"]): _display_name(u) for u in sub_accounts if u.get("_id")}

    # Aggregate sub-account orders
    sub_orders_stats: Dict[str, Dict[str, Any]] = {}
    if sub_ids:
        pipeline_sub = [
            {"$match": {"user_id": {"$in": sub_ids}}},
            {"$group": {
                "_id": "$user_id",
                "total_orders": {"$sum": 1},
                "total_sales": {"$sum": {"$convert": {"input": {"$ifNull": ["$total_amount", 0]}, "to": "double", "onError": 0, "onNull": 0}}},
                "first_order_at": {"$min": "$created_at"},
                "last_order_at": {"$max": "$created_at"},
            }},
        ]
        try:
            for row in orders_col.aggregate(pipeline_sub):
                sub_orders_stats[str(row["_id"])] = row
        except Exception:
            sub_orders_stats = {}

    sub_rows: List[Dict[str, Any]] = []
    for u in sub_accounts:
        uid = u.get("_id")
        if not uid:
            continue
        uid_str = str(uid)
        st = sub_orders_stats.get(uid_str, {})
        total_orders_u = int(st.get("total_orders", 0) or 0)
        total_sales_u = float(st.get("total_sales", 0) or 0.0)
        avg_order_u = (total_sales_u / total_orders_u) if total_orders_u else 0.0
        admin_id = u.get("admin_id")
        admin_name = admin_name_map.get(str(admin_id), "Admin") if admin_id else "Admin"

        sub_rows.append({
            "user_id": uid_str,
            "name": _display_name(u),
            "role": u.get("role") or "agent",
            "stage_label": u.get("stage_label") or "Normal",
            "status": u.get("status") or "active",
            "email": u.get("email") or "",
            "phone": u.get("phone") or "",
            "created_at": u.get("created_at"),
            "created_at_fmt": _fmt_dt(u.get("created_at")),
            "account_age_days": _age_days(u.get("created_at")),
            "admin_id": str(admin_id) if admin_id else "",
            "admin_name": admin_name,
            "total_orders": total_orders_u,
            "total_sales": total_sales_u,
            "avg_order_value": avg_order_u,
            "first_order_fmt": _fmt_dt(st.get("first_order_at")),
            "last_order_fmt": _fmt_dt(st.get("last_order_at")),
        })

    sub_rows.sort(key=lambda r: r.get("total_sales", 0), reverse=True)

    sub_summary = {
        "total_sub_accounts": len(sub_accounts),
        "total_orders": sum(r.get("total_orders", 0) for r in sub_rows),
        "total_sales": sum(r.get("total_sales", 0.0) for r in sub_rows),
        "avg_order_value": 0.0,
    }
    if sub_summary["total_orders"]:
        sub_summary["avg_order_value"] = sub_summary["total_sales"] / sub_summary["total_orders"]

    # Charts for sub-accounts (30-day trend)
    sub_daily_sales = [0.0 for _ in days]
    sub_daily_orders = [0 for _ in days]
    if sub_ids:
        window_start = datetime.combine(days[0], datetime.min.time())
        window_end = datetime.combine(days[-1], datetime.min.time()) + timedelta(days=1)
        pipeline_sub_daily = [
            {"$match": {"user_id": {"$in": sub_ids}, "created_at": {"$gte": window_start, "$lt": window_end}}},
            {"$project": {
                "d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}},
                "amt": {"$ifNull": ["$total_amount", 0]},
            }},
            {"$group": {
                "_id": "$d",
                "sales": {"$sum": {"$convert": {"input": "$amt", "to": "double", "onError": 0, "onNull": 0}}},
                "orders": {"$sum": 1},
            }},
        ]
        try:
            by_day = {}
            for row in orders_col.aggregate(pipeline_sub_daily):
                dt = row.get("_id")
                if isinstance(dt, datetime):
                    by_day[dt.date().isoformat()] = row
            for i, d in enumerate(days):
                key = d.isoformat()
                sub_daily_sales[i] = float(by_day.get(key, {}).get("sales", 0) or 0.0)
                sub_daily_orders[i] = int(by_day.get(key, {}).get("orders", 0) or 0)
        except Exception:
            pass

    # Top sub-accounts charts
    top_sub = sub_rows[:8]
    top_sub_labels = [r.get("name") or "User" for r in top_sub]
    top_sub_sales = [float(r.get("total_sales", 0) or 0.0) for r in top_sub]
    top_sub_orders = [int(r.get("total_orders", 0) or 0) for r in top_sub]

    # Focus detail (admin or sub-account)
    focused_admin = None
    if admin_focus_id:
        focused_admin = next((r for r in rows if r.get("admin_id") == str(admin_focus_id)), None)

    focused_agent = None
    if agent_focus_id:
        focused_agent = next((r for r in sub_rows if r.get("user_id") == str(agent_focus_id)), None)

    return render_template(
        "admin_performance.html",
        view=view,
        rows=rows,
        summary=summary,
        generated_at=_fmt_dt(_now()),
        daily_labels=daily_labels,
        daily_sales=daily_sales,
        daily_orders=daily_orders,
        top_admin_labels=top_admin_labels,
        top_admin_sales=top_admin_sales,
        top_admin_orders=top_admin_orders,
        admin_list=[{"id": str(a["_id"]), "name": _display_name(a), "level": a.get("admin_level") or "admin"} for a in admins if a.get("_id")],
        sub_rows=sub_rows,
        sub_summary=sub_summary,
        sub_daily_sales=sub_daily_sales,
        sub_daily_orders=sub_daily_orders,
        top_sub_labels=top_sub_labels,
        top_sub_sales=top_sub_sales,
        top_sub_orders=top_sub_orders,
        focused_admin=focused_admin,
        focused_agent=focused_agent,
        admin_focus_id=str(admin_focus_id) if admin_focus_id else "",
        agent_focus_id=str(agent_focus_id) if agent_focus_id else "",
    )
