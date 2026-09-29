"""Durable BundlePortal submissions and signed webhook inbox for both stores."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

from pymongo import ReturnDocument

import bundleportal as api

log = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "refunded"}
RETRY = {"queued", "retry_pending", "unconfirmed"}
_started = False
_start_lock = threading.Lock()
_submission_slots = threading.BoundedSemaphore(3)


def aggregate_status(items):
    statuses = [item.get("line_status", "processing") for item in items]
    if statuses and all(status == "completed" for status in statuses):
        return "completed"
    if statuses and all(status == statuses[0] and status in TERMINAL for status in statuses):
        return statuses[0]
    if statuses and all(status in TERMINAL for status in statuses):
        return "failed" if any(status != "completed" for status in statuses) else "completed"
    return "processing"


def refresh_order(collection, order_id):
    # Compare the item snapshot so concurrent line updates cannot be overwritten.
    for _ in range(4):
        order = collection.find_one({"order_id": order_id})
        if not order or order.get("refunded_at"):
            return
        result = collection.update_one({"_id": order["_id"], "items": order.get("items", [])},
                                       {"$set": {"status": aggregate_status(order.get("items", [])), "updated_at": datetime.utcnow()}})
        if result.matched_count:
            return


def process_line(collection, order_id, ref):
    if not _submission_slots.acquire(blocking=False):
        return
    try:
        return _process_line(collection, order_id, ref)
    finally:
        _submission_slots.release()


def _process_line(collection, order_id, ref):
    now = datetime.utcnow()
    token = uuid.uuid4().hex
    match = {"provider": "bundleportal", "provider_request_order_id": ref, "bp_ready": True,
             "line_status": {"$nin": list(TERMINAL | {"delivered"})},
             "api_status": {"$in": list(RETRY)}, "bundleportal_final": {"$ne": True},
             "$and": [{"$or": [{"bp_next_attempt": {"$exists": False}}, {"bp_next_attempt": {"$lte": now}}]},
                       {"$or": [{"bp_lease_until": {"$exists": False}}, {"bp_lease_until": {"$lte": now}}]}]}
    order = collection.find_one_and_update(
        {"order_id": order_id, "items": {"$elemMatch": match}},
        {"$set": {"items.$.bp_lease_until": now + timedelta(minutes=5), "items.$.bp_lease": token},
         "$inc": {"items.$.bp_attempts": 1}}, return_document=ReturnDocument.AFTER)
    if not order:
        return
    line = next(item for item in order["items"] if item.get("bp_lease") == token)
    query = {"order_id": order_id, "items": {"$elemMatch": {
        "provider_request_order_id": ref, "bp_lease": token, "bundleportal_final": {"$ne": True}}}}
    spending = bool(line.get("bp_submission_started"))
    if not os.getenv("BUNDLEPORTAL_WEBHOOK_SECRET", "").strip():
        payload = {"success": False, "code": "configuration", "message": "BundlePortal webhook secret must be configured before ordering.", "http_status": 401}
    else:
        payload = None if spending else api.preflight(api.normalize_phone(line.get("phone")), line.get("provider_network"), line.get("provider_gig"), ref)
        if payload is None:
            # Persist BEFORE spending. Crash recovery must skip verify_number: our
            # own accepted order could now be the pending order blocking the number.
            saved = collection.update_one(query, {"$set": {"items.$.bp_submission_started": True}})
            if not saved.matched_count:
                return
            spending = True
            payload = api.call("place_order", network=line.get("provider_network"),
                               recipient=api.normalize_phone(line.get("phone")),
                               package_size=line.get("provider_gig"), order_id=ref)
    state = api.outcome(payload, spending)
    attempts = line.get("bp_attempts", 1)
    retry = state in {"retry_pending", "unconfirmed"}
    if retry and attempts >= 6:
        state = "review_required"
        retry = False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    accepted = payload.get("success") is True
    fields = {"api_response": payload, "api_status": "success" if accepted else state,
              "line_status": state if state in {"completed", "cached"} else "processing",
              "provider_status": state, "bp_lease_until": now,
              "bp_next_attempt": datetime.utcnow() + timedelta(seconds=max(float(payload.get("retry_after") or 0), min(900, 15 * 2 ** min(attempts, 6))) + random.uniform(0, 5))}
    if accepted:
        fields.update(provider_reference=data.get("reference"), provider_order_id=data.get("order_id") or ref)
    if state in TERMINAL:
        fields["bundleportal_final"] = True
        if state == "failed":
            fields["refund_review_required"] = True
    collection.update_one(query, {"$set": {**{"items.$." + key: value for key, value in fields.items()}, "updated_at": datetime.utcnow()}})
    refresh_order(collection, order_id)


def dispatch_line(collection, order_id, ref):
    """Only the paid-order dispatcher may authorize a new submission."""
    collection.update_one({"order_id": order_id, "items": {"$elemMatch": {
        "provider": "bundleportal", "provider_request_order_id": ref, "bundleportal_final": {"$ne": True}}}},
        {"$set": {"items.$.bp_ready": True}})
    try:
        process_line(collection, order_id, ref)
    except Exception:
        # The persisted authorization and lease allow the worker to recover.
        log.exception("BundlePortal dispatch deferred for durable recovery")


def receive_webhook(raw, signature, inbox, secret=None):
    secret = os.getenv("BUNDLEPORTAL_WEBHOOK_SECRET", "") if secret is None else secret
    if not secret:
        return {"success": False, "message": "Webhook is not configured."}, 503
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(expected.encode(), signature.encode()):
        return {"success": False, "message": "Invalid signature."}, 401
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("event") not in {"order." + status for status in TERMINAL} or not isinstance(payload.get("order_id"), str) or not payload["order_id"]:
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        return {"success": False, "message": "Invalid event."}, 400
    # Built-in unique _id makes redelivery safe without a separate index setup.
    inbox.update_one({"_id": hashlib.sha256(raw).hexdigest()}, {"$setOnInsert": {
        "payload": payload, "status": "pending", "created_at": datetime.utcnow()}}, upsert=True)
    return {"success": True}, 200


def apply_event(payload, collections):
    ref = payload["order_id"]
    matches = []
    for collection in collections:
        query = {"items": {"$elemMatch": {"provider": "bundleportal", "provider_request_order_id": ref}}}
        for order in collection.find(query).limit(2):
            matches.append((collection, order))
    if len(matches) != 1:
        return "unmatched" if not matches else "ambiguous"
    collection, order = matches[0]
    state = payload["event"].removeprefix("order.")
    line = next(item for item in order["items"] if item.get("provider") == "bundleportal" and item.get("provider_request_order_id") == ref)
    # Preserve later settlement events when callbacks are received out of order.
    previous = line.get("bundleportal_settled_at")
    settled = payload.get("settled_at")
    try:
        settled_time = datetime.fromisoformat(str(settled).replace("Z", "+00:00"))
        if settled_time.tzinfo is None:
            return "invalid_timestamp"
        if previous and settled_time < datetime.fromisoformat(previous):
            return "processed"
    except (ValueError, TypeError):
        return "invalid_timestamp"
    if line.get("provider_status") == "refunded" and state != "refunded":
        return "processed"
    # Zico's 'refunded' status certifies a LOCAL wallet refund. A provider wallet
    # reversal cannot set it: the existing admin refund function would then skip
    # the actual customer/admin credit. Keep adverse outcomes open for review.
    local_refunded = bool(order.get("refunded_at") or line.get("refunded_at") or line.get("line_status") == "refunded")
    local_status = "refunded" if local_refunded else ("completed" if state == "completed" else "processing")
    fields = {"line_status": local_status, "provider_status": state, "api_status": "success" if state == "completed" else state,
              "bundleportal_final": True, "bundleportal_event": payload,
              "bundleportal_settled_at": settled_time.isoformat(),
              "provider_order_id": ref, "provider_reference": payload.get("reference"),
              "failure_reason": payload.get("failure_reason"),
              "refund_review_required": not local_refunded and state in {"failed", "cancelled", "refunded"}}
    # CAS the exact item so concurrent webhook workers cannot regress settlement.
    result = collection.update_one({"_id": order["_id"], "items": {"$elemMatch": line}},
                                  {"$set": {**{"items.$." + key: value for key, value in fields.items()}, "updated_at": datetime.utcnow()}})
    if not result.matched_count:
        return "pending"
    refresh_order(collection, order["order_id"])
    return "processed"


def start_worker(collections, inbox):
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def run():
        try:
            inbox.create_index([("status", 1), ("created_at", 1)])
            for collection in collections:
                collection.create_index("items.provider_request_order_id")
                collection.create_index([("items.provider", 1), ("items.api_status", 1)])
        except Exception:
            log.exception("BundlePortal index setup failed")
        while True:
            try:
                for collection in collections:
                    now = datetime.utcnow()
                    query = {"items": {"$elemMatch": {"provider": "bundleportal", "bp_ready": True, "api_status": {"$in": list(RETRY)}, "bundleportal_final": {"$ne": True},
                        "$and": [{"$or": [{"bp_next_attempt": {"$exists": False}}, {"bp_next_attempt": {"$lte": now}}]},
                                 {"$or": [{"bp_lease_until": {"$exists": False}}, {"bp_lease_until": {"$lte": now}}]}]}}}
                    for order in collection.find(query).limit(20):
                        for line in order.get("items", []):
                            if line.get("provider") == "bundleportal" and line.get("api_status") in RETRY:
                                process_line(collection, order["order_id"], line.get("provider_request_order_id"))
            except Exception:
                log.exception("BundlePortal worker failed; durable work will be retried")
            time.sleep(2)

    def receive():
        while True:
            try:
                for event in inbox.find({"status": "pending"}).sort("created_at", 1).limit(50):
                    status = apply_event(event["payload"], collections)
                    inbox.update_one({"_id": event["_id"], "status": {"$ne": "processed"}},
                                     {"$set": {"status": status, "updated_at": datetime.utcnow()}})
            except Exception:
                log.exception("BundlePortal inbox processing failed; saved callbacks will be retried")
            time.sleep(1)

    threading.Thread(target=receive, name="bundleportal-inbox", daemon=True).start()
    threading.Thread(target=run, name="bundleportal-worker", daemon=True).start()
