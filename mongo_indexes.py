"""
Safe MongoDB index setup for the Zico Flask app.

Run manually after deployment or when setting up a fresh database:

    python mongo_indexes.py

Indexes speed up reads, filters, counts, sorting, pagination, admin/user
scoping, and dashboard queries. They do add a small cost to writes because
MongoDB must maintain each index when documents change, so keep this list
focused on high-traffic query paths.

This script reuses the existing `db` object from db.py and does not create a
separate MongoDB connection. It is safe to run multiple times: create_index()
will reuse an existing matching index, and every index operation is isolated
with try/except so one failure does not stop the full setup.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from pymongo import ASCENDING, DESCENDING

from db import db


IndexSpec = List[Tuple[str, int]]


INDEXES: dict[str, list[IndexSpec]] = {
    # Order placement, dashboard counts/charts, admin filtering, customer history,
    # duplicate checks, status pages, and store checkout lookups.
    "orders": [
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("user_id", ASCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("paid_from", ASCENDING)],
        [("wallet_debit_status", ASCENDING)],
        [("created_at", DESCENDING)],
        [("order_id", ASCENDING)],
        [("reference", ASCENDING)],
        [("admin_id", ASCENDING), ("order_id", ASCENDING)],
        [("admin_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("paystack_reference", ASCENDING)],
        [("items.phone", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("items.serviceId", ASCENDING), ("created_at", DESCENDING)],
    ],

    # Login/admin/customer/agent scoping, approval queues, profile lookup, and search.
    "users": [
        [("role", ASCENDING)],
        [("admin_id", ASCENDING), ("role", ASCENDING)],
        [("admin_id", ASCENDING), ("role", ASCENDING), ("status", ASCENDING)],
        [("phone", ASCENDING)],
        [("email", ASCENDING)],
        [("username", ASCENDING)],
        [("status", ASCENDING)],
        [("approval_status", ASCENDING)],
        [("deleted", ASCENDING)],
        [("created_at", DESCENDING)],
    ],

    # Wallet balance lookups and admin balance screens.
    "balances": [
        [("user_id", ASCENDING)],
        [("admin_id", ASCENDING)],
        [("user_id", ASCENDING), ("admin_id", ASCENDING)],
        [("admin_id", ASCENDING), ("updated_at", DESCENDING)],
    ],

    # Deposit/deduction reporting, wallet audits, and dashboard wallet flow cards.
    "balance_logs": [
        [("user_id", ASCENDING), ("action", ASCENDING), ("created_at", DESCENDING)],
        [("action", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("action", ASCENDING), ("created_at", DESCENDING)],
        [("created_at", DESCENDING)],
        [("order_id", ASCENDING)],
        [("source", ASCENDING), ("created_at", DESCENDING)],
    ],

    # Transaction pages, Paystack cashflow, references, wallet payments, and filters.
    "transactions": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("gateway", ASCENDING)],
        [("source", ASCENDING)],
        [("reference", ASCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("created_at", DESCENDING)],
        [("verified_at", DESCENDING), ("created_at", DESCENDING)],
        [("meta.store_slug", ASCENDING), ("type", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
    ],

    "payment_intents": [
        [("provider", ASCENDING), ("reference", ASCENDING)],
        [("intent_id", ASCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("flow", ASCENDING), ("reference", ASCENDING)],
    ],

    # Store withdrawal modal, payout review, status filters, search, and pagination.
    "store_withdraw_requests": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("owner_id", ASCENDING)],
        [("reference", ASCENDING)],
        [("store_slug", ASCENDING)],
        [("created_at", DESCENDING)],
        [("owner_id", ASCENDING), ("store_slug", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
    ],

    # AFA dashboards, admin queues, customer search, and duplicate/reference checks.
    "afa_registrations": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("phone", ASCENDING)],
        [("ghana_card", ASCENDING)],
        [("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("paystack_reference", ASCENDING)],
    ],

    # Bulk SMS delivery dashboards, delivery history, references, and status filters.
    "bulk_sms_deliveries": [
        [("admin_id", ASCENDING), ("delivery_status", ASCENDING), ("created_at", DESCENDING)],
        [("delivery_status", ASCENDING), ("delivered_at", DESCENDING)],
        [("created_at", DESCENDING)],
        [("reference", ASCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
    ],
    "bulk_sms_delivery_logs": [
        [("delivery_id", ASCENDING), ("created_at", DESCENDING)],
        [("reference", ASCENDING)],
        [("created_at", DESCENDING)],
    ],

    # Main-admin activity feed.
    "activity_logs": [
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("created_at", DESCENDING)],
        [("actor_role", ASCENDING)],
        [("action", ASCENDING)],
    ],

    # Store balance/payout cards and store owner lookups.
    "store_accounts": [
        [("admin_id", ASCENDING)],
        [("store_slug", ASCENDING)],
        [("owner_id", ASCENDING)],
        [("total_profit_balance", DESCENDING)],
        [("admin_id", ASCENDING), ("store_slug", ASCENDING)],
    ],

    # Admin Paystack payout summary and request queues.
    "admin_paystack_balances": [
        [("admin_id", ASCENDING)],
        [("available_balance", DESCENDING)],
        [("pending_balance", DESCENDING)],
        [("updated_at", DESCENDING)],
    ],
    "admin_paystack_payout_requests": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("reference", ASCENDING)],
    ],
    "admin_paystack_balance_logs": [
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("reference", ASCENDING)],
    ],

    # Storefronts and products.
    "stores": [
        [("slug", ASCENDING)],
        [("owner_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING), ("created_at", DESCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
    ],
    "store_products": [
        [("admin_id", ASCENDING)],
        [("store_slug", ASCENDING)],
        [("category", ASCENDING)],
        [("status", ASCENDING)],
        [("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
    ],
    "products": [
        [("admin_id", ASCENDING)],
        [("category", ASCENDING)],
        [("status", ASCENDING)],
        [("created_at", DESCENDING)],
    ],

    # Service/offers management and pricing.
    "services": [
        [("admin_id", ASCENDING), ("base_service_id", ASCENDING)],
        [("admin_id", ASCENDING), ("name", ASCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING)],
        [("provider", ASCENDING)],
        [("network_id", ASCENDING)],
        [("created_at", DESCENDING)],
    ],
    "service_profits": [
        [("admin_id", ASCENDING), ("service_id", ASCENDING)],
        [("user_id", ASCENDING), ("service_id", ASCENDING)],
    ],

    # Store complaints and admin complaint queues.
    "complaints": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("order_id", ASCENDING)],
        [("phone", ASCENDING)],
        [("created_at", DESCENDING)],
    ],

    # Customer-specific collections that may exist in deployments.
    "customers": [
        [("admin_id", ASCENDING)],
        [("phone", ASCENDING)],
        [("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("phone", ASCENDING)],
    ],
    "payments": [
        [("customer_id", ASCENDING), ("date", DESCENDING)],
        [("agent_id", ASCENDING), ("date", DESCENDING)],
        [("admin_id", ASCENDING), ("date", DESCENDING)],
        [("created_at", DESCENDING)],
        [("reference", ASCENDING)],
    ],

    # Other app collections seen in the project.
    "purchase_history": [
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("order_id", ASCENDING)],
    ],
    "auth_pages": [
        [("admin_id", ASCENDING)],
        [("slug", ASCENDING)],
        [("updated_at", DESCENDING)],
    ],
    "wassce_checker": [
        [("admin_id", ASCENDING), ("checker_type", ASCENDING)],
        [("status", ASCENDING)],
        [("created_at", DESCENDING)],
    ],
    "announcements": [
        [("admin_id", ASCENDING), ("audience", ASCENDING), ("created_at", DESCENDING)],
        [("active", ASCENDING), ("created_at", DESCENDING)],
    ],
    "login_logs": [
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("role", ASCENDING), ("created_at", DESCENDING)],
    ],
    "blocked_phone_numbers": [
        [("phone", ASCENDING)],
        [("admin_id", ASCENDING), ("phone", ASCENDING)],
    ],
}


def _index_name(collection_name: str, spec: IndexSpec) -> str:
    parts = []
    for field, direction in spec:
        safe_field = field.replace(".", "_").replace("$", "").replace(" ", "_")
        suffix = "asc" if int(direction) == ASCENDING else "desc"
        parts.append(f"{safe_field}_{suffix}")
    return f"idx__{collection_name}__{'__'.join(parts)}"


def _canonical(spec: Iterable[Tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    return tuple((field, int(direction)) for field, direction in spec)


def create_indexes() -> None:
    print("Starting MongoDB index setup...")
    print("Using existing db object imported from db.py")

    created = 0
    skipped = 0
    failed = 0

    for collection_name, specs in INDEXES.items():
        collection = db[collection_name]
        print(f"\nCollection: {collection_name}")

        try:
            existing = {
                _canonical(info.get("key", []))
                for info in collection.index_information().values()
            }
        except Exception as exc:
            existing = set()
            failed += 1
            print(f"  [warn] Could not read existing indexes: {exc}")

        for spec in specs:
            name = _index_name(collection_name, spec)
            try:
                if _canonical(spec) in existing:
                    skipped += 1
                    print(f"  [skip] {name} already exists")
                    continue

                collection.create_index(spec, name=name, background=True)
                created += 1
                print(f"  [ok] created {name}: {spec}")
            except Exception as exc:
                failed += 1
                print(f"  [fail] {name}: {exc}")

    print("\nMongoDB index setup complete.")
    print(f"Created: {created}")
    print(f"Skipped: {skipped}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    create_indexes()
