from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import db  # noqa: E402
from profit_ledger import apply_profit_split, money, normalize_profit_line, profit_totals  # noqa: E402


orders_col = db["orders"]


def _line_has_split_fields(line: Dict[str, Any]) -> bool:
    return all(line.get(k) not in (None, "") for k in ("selling_amount", "admin_base_amount", "main_base_amount"))


def _normalize_order_lines(lines: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, float], bool]:
    changed = False
    finalized: List[Dict[str, Any]] = []
    for raw in lines or []:
        line = dict(raw or {})
        before = {
            "selling_amount": line.get("selling_amount"),
            "admin_base_amount": line.get("admin_base_amount"),
            "main_base_amount": line.get("main_base_amount"),
            "base_amount": line.get("base_amount"),
            "amount": line.get("amount"),
            "profit_amount": line.get("profit_amount"),
            "main_admin_profit": line.get("main_admin_profit"),
            "admin_profit": line.get("admin_profit"),
            "store_profit_amount": line.get("store_profit_amount"),
        }
        selling = line.get("selling_amount") if line.get("selling_amount") not in (None, "") else line.get("amount")
        existing_store_profit = line.get("store_profit_amount")
        store_owner_base = line.get("store_owner_base_amount")
        if store_owner_base in (None, "") and existing_store_profit not in (None, ""):
            store_owner_base = max(0.0, round(money(selling) - money(existing_store_profit), 2))

        admin_base = line.get("admin_base_amount")
        if admin_base in (None, ""):
            if store_owner_base not in (None, "") and line.get("profit_amount") not in (None, ""):
                admin_base = max(0.0, round(money(store_owner_base) - money(line.get("profit_amount")), 2))
            else:
                admin_base = line.get("base_amount")

        main_base = line.get("main_base_amount")
        if main_base in (None, ""):
            main_base = admin_base

        normalized = normalize_profit_line(
            line,
            selling_amount=selling,
            admin_base_amount=admin_base,
            main_base_amount=main_base,
            store_owner_base_amount=store_owner_base if store_owner_base not in (None, "") else None,
            store_profit_amount=existing_store_profit,
        )
        normalized = apply_profit_split(normalized)
        after = {key: normalized.get(key) for key in before}
        if before != after or not _line_has_split_fields(line):
            changed = True
        finalized.append(normalized)
    return finalized, profit_totals(finalized), changed


def _needs_total_update(order: Dict[str, Any], totals: Dict[str, float]) -> bool:
    return (
        money(order.get("main_admin_profit_total")) != money(totals.get("main_admin_profit_total"))
        or money(order.get("admin_profit_total")) != money(totals.get("admin_profit_total"))
        or money(order.get("store_profit_total")) != money(totals.get("store_profit_total"))
        or money(order.get("profit_amount_total")) != money(totals.get("profit_amount_total"))
    )


def run(*, apply: bool, limit: int = 0) -> None:
    query: Dict[str, Any] = {"items": {"$type": "array", "$ne": []}}
    cursor = orders_col.find(
        query,
        {
            "items": 1,
            "profit_amount_total": 1,
            "main_admin_profit_total": 1,
            "admin_profit_total": 1,
            "store_profit_total": 1,
        },
        sort=[("created_at", -1)],
    )
    if limit > 0:
        cursor = cursor.limit(limit)

    scanned = updated = skipped = 0
    admin_before = admin_after = 0.0
    main_before = main_after = 0.0

    for order in cursor:
        scanned += 1
        admin_before += money(order.get("admin_profit_total"))
        main_before += money(order.get("main_admin_profit_total"))

        lines, totals, lines_changed = _normalize_order_lines(order.get("items") or [])
        admin_after += money(totals.get("admin_profit_total"))
        main_after += money(totals.get("main_admin_profit_total"))

        if not lines_changed and not _needs_total_update(order, totals):
            skipped += 1
            continue

        updated += 1
        if apply:
            orders_col.update_one(
                {"_id": order["_id"]},
                {
                    "$set": {
                        "items": lines,
                        "profit_amount_total": money(totals.get("profit_amount_total")),
                        "main_admin_profit_total": money(totals.get("main_admin_profit_total")),
                        "admin_profit_total": money(totals.get("admin_profit_total")),
                        "store_profit_total": money(totals.get("store_profit_total")),
                    }
                },
            )

    mode = "APPLY" if apply else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Scanned orders: {scanned}")
    print(f"Updated orders: {updated}")
    print(f"Skipped orders: {skipped}")
    print(f"Total admin profit before: {round(admin_before, 2)}")
    print(f"Total admin profit after: {round(admin_after, 2)}")
    print(f"Total main admin profit before: {round(main_before, 2)}")
    print(f"Total main admin profit after: {round(main_after, 2)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill normalized profit split fields on orders.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Scan and print summary without writing.")
    mode.add_argument("--apply", action="store_true", help="Write normalized items and totals to MongoDB.")
    parser.add_argument("--limit", type=int, default=0, help="Optional number of newest orders to scan.")
    args = parser.parse_args()
    run(apply=bool(args.apply), limit=max(0, int(args.limit or 0)))


if __name__ == "__main__":
    main()
