from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Any, List
import os
import traceback

from bson import ObjectId
from apscheduler.schedulers.background import BackgroundScheduler
import threading

from db import db
from service_admin_pricing import reprice_admin_services_for_admin

users_col = db["users"]
orders_col = db["orders"]

ADMIN_LEVELS = ("admin", "super_admin", "super_professional")

SUPER_ADMIN_REQ = {"min_months": 3, "min_agents": 30, "min_avg_sales": 500}
SUPER_PRO_REQ = {"min_months": 6, "min_agents": 70, "min_avg_sales": 1000}


def _now() -> datetime:
    return datetime.utcnow()


def _normalize_level(raw: str | None) -> str:
    lvl = (raw or "").strip().lower()
    return lvl if lvl in ADMIN_LEVELS else "admin"


def _age_months(created_at: Any) -> float:
    if not isinstance(created_at, datetime):
        return 0.0
    days = max(0, (_now() - created_at).days)
    return round(days / 30.0, 2)


def _agents_count(admin_oid: ObjectId) -> int:
    return int(
        users_col.count_documents(
            {
                "role": "agent",
                "admin_id": admin_oid,
                "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
            }
        )
    )


def _avg_daily_sales(admin_oid: ObjectId, days_back: int = 30) -> float:
    end = _now()
    start = end - timedelta(days=days_back)
    paid_statuses = ["processing", "delivered", "success", "completed", "paid"]
    amt_expr = {"$ifNull": ["$charged_amount", "$total_amount"]}
    pipeline = [
        {"$match": {
            "admin_id": admin_oid,
            "status": {"$in": paid_statuses},
            "created_at": {"$gte": start, "$lt": end},
        }},
        {"$group": {
            "_id": None,
            "total": {"$sum": {"$convert": {"input": amt_expr, "to": "double", "onError": 0, "onNull": 0}}},
        }},
    ]
    try:
        doc = next(orders_col.aggregate(pipeline), None)
        total = float((doc or {}).get("total", 0) or 0)
    except Exception:
        total = 0.0
    return round(total / float(days_back), 2)


def _eligible_for(level: str, months: float, agents: int, avg_sales: float) -> bool:
    if level == "super_admin":
        return (
            months >= SUPER_ADMIN_REQ["min_months"]
            and agents >= SUPER_ADMIN_REQ["min_agents"]
            and avg_sales >= SUPER_ADMIN_REQ["min_avg_sales"]
        )
    if level == "super_professional":
        return (
            months >= SUPER_PRO_REQ["min_months"]
            and agents >= SUPER_PRO_REQ["min_agents"]
            and avg_sales >= SUPER_PRO_REQ["min_avg_sales"]
        )
    return False


def _upgrade_one(admin_doc: Dict[str, Any]) -> bool:
    if not admin_doc:
        return False
    if (admin_doc.get("role") or "").strip().lower() == "main_admin":
        return False
    admin_oid = admin_doc.get("_id")
    if not isinstance(admin_oid, ObjectId):
        return False

    current = _normalize_level(admin_doc.get("admin_level"))
    if current == "super_professional":
        return False

    months = _age_months(admin_doc.get("created_at"))
    agents = _agents_count(admin_oid)
    avg_sales = _avg_daily_sales(admin_oid, days_back=30)

    target = None
    if _eligible_for("super_professional", months, agents, avg_sales):
        target = "super_professional"
    elif _eligible_for("super_admin", months, agents, avg_sales):
        target = "super_admin"

    if not target or target == current:
        return False

    now = _now()
    res = users_col.update_one(
        {"_id": admin_oid},
        {"$set": {
            "admin_level": target,
            "admin_level_auto_upgraded_at": now,
            "admin_level_updated_at": now,
            "admin_level_updated_by": "system",
            "updated_at": now,
        }}
    )
    if res.modified_count:
        try:
            reprice_admin_services_for_admin(admin_oid)
        except Exception:
            pass
    return bool(res.modified_count)


def run_admin_level_upgrades() -> Dict[str, int]:
    updated = 0
    errors = 0
    cursor = users_col.find({"role": {"$in": ["admin", "main_admin"]}}, {"_id": 1, "role": 1, "admin_level": 1, "created_at": 1})
    for doc in cursor:
        try:
            if _upgrade_one(doc):
                updated += 1
        except Exception:
            errors += 1
    return {"updated": updated, "errors": errors}


def _scheduled_upgrade_job():
    try:
        res = run_admin_level_upgrades()
        print(f"[admin_level] auto-upgrade done: {res}")
    except Exception:
        print("[admin_level] scheduled upgrade failed:", traceback.format_exc())


if os.getenv("ADMIN_LEVEL_SCHEDULER", "1").strip().lower() not in {"0", "false", "off"}:
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _scheduled_upgrade_job,
        "interval",
        days=1,
        max_instances=1,
        coalesce=True,
        id="admin_level_daily",
    )
    _scheduler.add_job(
        _scheduled_upgrade_job,
        "date",
        run_date=_now() + timedelta(seconds=15),
        id="admin_level_startup",
    )
    try:
        _scheduler.start()
        threading.Thread(target=run_admin_level_upgrades, daemon=True).start()
    except Exception:
        print("[admin_level] scheduler start failed:", traceback.format_exc())


if __name__ == "__main__":
    print(run_admin_level_upgrades())
