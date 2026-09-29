from __future__ import annotations

from datetime import datetime
import os
import sys
import traceback

from db import db


PRESERVED_COLLECTIONS = {"services", "settings", "users","afa settings"}
SYSTEM_PREFIXES = ("system.",)


def _is_preserved(collection_name: str) -> bool:
    return collection_name in PRESERVED_COLLECTIONS or collection_name.startswith(SYSTEM_PREFIXES)


def _as_yes(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def main() -> int:
    dry_run = "--dry-run" in sys.argv or _as_yes(os.getenv("DRY_RUN"))
    started_at = datetime.utcnow()

    print("AZICO testing data reset")
    print(f"Started UTC: {started_at.isoformat(timespec='seconds')}")
    print("Preserving:", ", ".join(sorted(PRESERVED_COLLECTIONS)))

    collection_names = sorted(db.list_collection_names())
    target_names = [name for name in collection_names if not _is_preserved(name)]

    if not target_names:
        print("No resettable collections found.")
        return 0

    print("\nCollections to clear:")
    for name in target_names:
        print(f" - {name}")

    if dry_run:
        print("\nDry run only. No data was deleted.")
        return 0

    print("\nClearing testing/runtime data...")
    total_deleted = 0
    failures: list[tuple[str, str]] = []

    for name in target_names:
        try:
            result = db[name].delete_many({})
            deleted = int(result.deleted_count or 0)
            total_deleted += deleted
            print(f"Cleared {name}: {deleted} document(s)")
        except Exception as exc:
            failures.append((name, str(exc)))
            print(f"FAILED {name}: {exc}")

    print(f"\nDeleted total: {total_deleted} document(s)")
    print(f"Finished UTC: {datetime.utcnow().isoformat(timespec='seconds')}")

    if failures:
        print("\nSome collections could not be cleared:")
        for name, message in failures:
            print(f" - {name}: {message}")
        return 1

    print("\nReset complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
