from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Tuple
import os
import traceback

from bson import ObjectId
from apscheduler.schedulers.background import BackgroundScheduler
import threading

from db import db

users_col = db["users"]
orders_col = db["orders"]
stores_col = db["stores"]

AVG_DAYS = 30  # rolling window for average daily sales


def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end


def _stage_label(avg_daily_sales: float) -> str:
    if avg_daily_sales <= 500:
        return "Normal Agent"
    if avg_daily_sales <= 1500:
        return "Elite Agent"
    return "Premium"


def _compute_avg_daily_sales(user_oid: ObjectId, days_back: int = AVG_DAYS) -> float:
    today = datetime.utcnow().date()
    days = [today - timedelta(days=i) for i in range(days_back)][::-1]
    window_start, _ = _day_range(days[0])
    _, window_end = _day_range(days[-1])

    store_slugs: List[str] = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1},
            )
            if s.get("slug")
        ]
    except Exception:
        store_slugs = []

    match_or = [{"user_id": user_oid}]
    if store_slugs:
        match_or.append({"store_slug": {"$in": store_slugs}})

    pipeline = [
        {"$match": {
            "$or": match_or,
            "created_at": {"$gte": window_start, "$lt": window_end},
        }},
        {"$project": {
            "d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}},
            "amt": {"$ifNull": ["$total_amount", 0]},
        }},
        {"$group": {"_id": "$d", "sales": {"$sum": "$amt"}}},
    ]

    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    by_day: Dict[date, float] = {}
    for row in agg:
        dt = row.get("_id")
        if isinstance(dt, datetime):
            by_day[dt.date()] = float(row.get("sales", 0) or 0.0)

    values = [float(by_day.get(d, 0.0)) for d in days]
    if not values:
        return 0.0

    return round(sum(values) / float(len(values)), 2)


def update_customer_stage(user_oid: ObjectId) -> Dict[str, Any]:
    avg = _compute_avg_daily_sales(user_oid, days_back=AVG_DAYS)
    label = _stage_label(avg)
    now = datetime.utcnow()

    users_col.update_one(
        {"_id": user_oid},
        {"$set": {
            "stage_label": label,
            "stage_avg_daily_sales": avg,
            "stage_updated_at": now,
        }},
    )
    return {"user_id": user_oid, "stage_label": label, "avg": avg}


def update_all_customer_stages() -> Dict[str, Any]:
    updated = 0
    errors = 0
    cur = users_col.find({"role": "customer"}, {"_id": 1})
    for u in cur:
        try:
            update_customer_stage(u["_id"])
            updated += 1
        except Exception:
            errors += 1
    return {"updated": updated, "errors": errors}


def _scheduled_stage_job():
    try:
        update_all_customer_stages()
    except Exception:
        print("[stage] scheduled update failed:", traceback.format_exc())


# ---- Scheduler: once daily ----
_scheduler = BackgroundScheduler(timezone="UTC")
_scheduler.add_job(
    _scheduled_stage_job,
    "interval",
    days=1,
    max_instances=1,
    coalesce=True,
    id="customer_stage_daily",
)
_scheduler.add_job(
    _scheduled_stage_job,
    "date",
    run_date=datetime.utcnow() + timedelta(seconds=10),
    id="customer_stage_startup",
)

try:
    _scheduler.start()
    # fire-and-forget update on app start (non-blocking)
    threading.Thread(target=update_all_customer_stages, daemon=True).start()
except Exception:
    print("[stage] scheduler start failed:", traceback.format_exc())


if __name__ == "__main__":
    # Run a manual update when executed directly
    print(update_all_customer_stages())
