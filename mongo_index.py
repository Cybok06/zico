# mongo_index_init.py
from pymongo import ASCENDING, DESCENDING
from db import db

INDEX_PLAN = {
    "orders": [
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("paid_from", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("total_amount", DESCENDING), ("created_at", DESCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("paystack_reference", ASCENDING)],
        [("order_id", ASCENDING)],
    ],
    "transactions": [
        [("user_id", ASCENDING), ("verified_at", DESCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("verified_at", DESCENDING), ("created_at", DESCENDING)],
        [("reference", ASCENDING), ("status", ASCENDING)],
        [("balance_log_id", ASCENDING)],
        [("meta.store_slug", ASCENDING), ("type", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
    ],
    "payment_intents": [
        [("provider", ASCENDING), ("reference", ASCENDING)],
        [("intent_id", ASCENDING)],
        [("status", ASCENDING), ("created_at", DESCENDING)],
        [("flow", ASCENDING), ("reference", ASCENDING)],
    ],
    "users": [
        [("admin_id", ASCENDING), ("role", ASCENDING), ("status", ASCENDING)],
        [("admin_id", ASCENDING), ("role", ASCENDING), ("_id", DESCENDING)],
        [("admin_id", ASCENDING), ("role", ASCENDING), ("first_name", ASCENDING)],
        [("username", ASCENDING)],
        [("phone", ASCENDING)],
    ],
    "stores": [
        [("slug", ASCENDING), ("status", ASCENDING)],
        [("owner_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING), ("created_at", DESCENDING)],
    ],
    "bulk_sms_deliveries": [
        [("admin_id", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        [("user_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("reference", ASCENDING)],
    ],
    "balances": [
        [("user_id", ASCENDING)],
        [("admin_id", ASCENDING), ("updated_at", DESCENDING)],
    ],
    "balance_logs": [
        [("admin_id", ASCENDING), ("action", ASCENDING), ("created_at", DESCENDING)],
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
    ],
    "store_accounts": [
        [("store_slug", ASCENDING)],
        [("admin_id", ASCENDING), ("store_slug", ASCENDING)],
    ],
    "store_withdraw_requests": [
        [("owner_id", ASCENDING), ("store_slug", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
    ],
    "store_payouts": [
        [("owner_id", ASCENDING), ("store_slug", ASCENDING)],
    ],
    "store_payout_logs": [
        [("owner_id", ASCENDING), ("store_slug", ASCENDING), ("created_at", DESCENDING)],
    ],
    "afa_registrations": [
        [("admin_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
        [("store_slug", ASCENDING), ("paystack_reference", ASCENDING)],
    ],
    "activity_logs": [
        [("created_at", DESCENDING)],
    ],
    "services": [
        [("admin_id", ASCENDING), ("base_service_id", ASCENDING)],
        [("admin_id", ASCENDING), ("name", ASCENDING)],
    ],
}

def _canon(spec):
    return tuple((k, int(v)) for k, v in spec)

def ensure_indexes():
    for coll_name, specs in INDEX_PLAN.items():
        coll = db[coll_name]
        existing = {
            tuple((k, int(v)) for k, v in info["key"])
            for info in coll.index_information().values()
        }
        for spec in specs:
            if _canon(spec) in existing:
                print(f"[skip] {coll_name} {spec}")
                continue
            name = "idx__" + coll_name + "__" + "__".join(f"{k}_{'asc' if v == 1 else 'desc'}" for k, v in spec)
            coll.create_index(spec, name=name, background=True)
            print(f"[create] {coll_name} {name}")

if __name__ == "__main__":
    ensure_indexes()
