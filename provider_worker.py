from __future__ import annotations

import threading
import time

# Import checkout so provider job processors register on startup.
import checkout  # noqa: F401
import admin_orders  # noqa: F401

from order_job_queue import run_provider_job_worker_forever
from admin_orders import process_order_automation_tick


AUTOMATION_TICK_SECONDS = 180


def run_order_automation_forever() -> None:
    while True:
        try:
            process_order_automation_tick(
                max_schedule_batch=50,
                max_auto_rules=50,
                max_orders_per_rule=1000,
            )
        except Exception:
            pass
        time.sleep(AUTOMATION_TICK_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=run_order_automation_forever, name="order-automation-worker", daemon=True).start()
    run_provider_job_worker_forever()
