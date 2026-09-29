"""Public signed callbacks; no session or checkout authentication required."""
from flask import Blueprint, current_app, jsonify, request
from pymongo import timeout

from db import db
from bundleportal_orders import receive_webhook, start_worker

bundleportal_bp = Blueprint("bundleportal", __name__)


@bundleportal_bp.post("/bundleportal/webhook")
def orders_webhook():
    if request.content_length and request.content_length > 16384:
        return jsonify({"success": False}), 413
    try:
        with timeout(3):
            payload, status = receive_webhook(
                request.get_data(), request.headers.get("X-BundlePortal-Signature", ""),
                db["bundleportal_webhook_events"],
            )
        return jsonify(payload), status
    except Exception:
        current_app.logger.exception("BundlePortal callback persistence failed")
        return jsonify({"success": False}), 503


def start_bundleportal_worker():
    start_worker([db["orders"]], db["bundleportal_webhook_events"])
