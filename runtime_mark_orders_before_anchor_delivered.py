"""
Mark older orders as delivered using an anchor order's created_at timestamp.

Default behavior is dry-run. The script:
1. Finds the anchor order by `order_id`
2. Selects orders created before the anchor order
3. Skips orders already in a final state by default
4. Marks the remaining orders as delivered when run with `--apply`

Example:
  python runtime_mark_orders_before_anchor_delivered.py
  python runtime_mark_orders_before_anchor_delivered.py --apply
  python runtime_mark_orders_before_anchor_delivered.py --anchor-order-id ORDER-581689 --limit 50
"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

from db import db

orders_col = db["orders"]

FINAL_STATUSES = {"delivered", "completed", "refunded", "cancelled", "canceled", "success"}
SKIP_STATUSES_DEFAULT = FINAL_STATUSES | {"failed", "rejected"}


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _anchor_order(order_id: str) -> dict | None:
    return orders_col.find_one({"order_id": order_id}, {"order_id": 1, "created_at": 1, "status": 1})


def _candidate_query(anchor_created_at: datetime, skip_statuses: set[str]) -> dict:
    query: dict[str, Any] = {
        "created_at": {"$lt": anchor_created_at},
    }
    if skip_statuses:
        query["status"] = {"$nin": sorted(skip_statuses)}
    return query


def _preview_rows(query: dict, limit: int) -> list[dict]:
    projection = {
        "_id": 1,
        "order_id": 1,
        "status": 1,
        "created_at": 1,
        "delivered_at": 1,
    }
    return list(
        orders_col.find(query, projection).sort([("created_at", 1), ("_id", 1)]).limit(limit)
    )


def _matching_order_ids(query: dict) -> list:
    return [doc["_id"] for doc in orders_col.find(query, {"_id": 1}) if doc.get("_id") is not None]


def _build_update_doc(now: datetime) -> dict:
    return {
        "$set": {
            "status": "delivered",
            "updated_at": now,
            "delivered_at": now,
        }
    }


def _apply_item_updates(order_id) -> None:
    try:
        orders_col.update_one(
            {"_id": order_id},
            {"$set": {"items.$[it].line_status": "delivered"}},
            array_filters=[
                {
                    "it.line_status": {
                        "$nin": ["delivered", "completed", "refunded", "failed", "cancelled", "canceled"]
                    }
                }
            ],
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark orders older than an anchor order as delivered."
    )
    parser.add_argument(
        "--anchor-order-id",
        default="ORDER-581689",
        help="Anchor order_id. Orders created before this order are selected.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write updates to MongoDB. Without this flag the script is dry-run only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many matching orders to print in the preview.",
    )
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Also mark failed/rejected orders as delivered.",
    )
    args = parser.parse_args()

    anchor = _anchor_order(args.anchor_order_id)
    if not anchor:
        print(f"Anchor order not found: {args.anchor_order_id}")
        return 1

    anchor_created_at = anchor.get("created_at")
    if not isinstance(anchor_created_at, datetime):
        print(f"Anchor order {args.anchor_order_id} does not have a valid created_at.")
        return 1

    skip_statuses = set(SKIP_STATUSES_DEFAULT)
    if args.include_failed:
        skip_statuses.discard("failed")
        skip_statuses.discard("rejected")

    query = _candidate_query(anchor_created_at, skip_statuses)
    total_matches = int(orders_col.count_documents(query))
    preview = _preview_rows(query, max(1, int(args.limit)))

    print(f"Anchor order: {anchor.get('order_id')}")
    print(f"Anchor created_at: {anchor_created_at.isoformat()}")
    print(f"Anchor current status: {anchor.get('status') or '-'}")
    print(f"Skip statuses: {', '.join(sorted(skip_statuses)) if skip_statuses else '(none)'}")
    print(f"Orders matching created_at < anchor: {total_matches}")
    print("")
    print("Preview:")
    for row in preview:
        created = row.get("created_at")
        created_text = created.isoformat() if isinstance(created, datetime) else str(created or "")
        print(
            f"- {row.get('order_id') or row.get('_id')} | "
            f"status={row.get('status') or '-'} | created_at={created_text}"
        )

    if not args.apply:
        print("")
        print("Dry run only. Re-run with --apply to mark these orders as delivered.")
        return 0

    now = datetime.utcnow()
    matching_ids = _matching_order_ids(query)
    result = orders_col.update_many(query, _build_update_doc(now))
    print("")
    print(f"Matched: {int(result.matched_count)}")
    print(f"Modified: {int(result.modified_count)}")

    for order_id in matching_ids:
        _apply_item_updates(order_id)

    print(f"Applied item line_status updates to {len(matching_ids)} matched orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
