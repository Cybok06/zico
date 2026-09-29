import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import mongomock
import requests

import bundleportal as api
from bundleportal_orders import apply_event, dispatch_line, process_line, receive_webhook


class BundlePortalTests(unittest.TestCase):
    def setUp(self):
        self.client = mongomock.MongoClient()
        self.orders = self.client.nagonu.orders
        self.other = self.client.zico.orders
        self.inbox = self.client.nagonu.events
        self.env = patch.dict(os.environ, {"BUNDLEPORTAL_API_KEY": "bp_live_test", "BUNDLEPORTAL_WEBHOOK_SECRET": "test-secret", "BUNDLEPORTAL_BASE_URL": "https://api.bundleportal.com/v2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        api._catalogue.clear()
        self.orders.insert_one({"order_id": "order-1", "status": "processing", "items": [{
            "provider": "bundleportal", "provider_request_order_id": "line-1", "phone": "0241234567",
            "provider_network": "mtn", "provider_gig": 0.5, "api_status": "queued", "line_status": "processing"}]})

    def line(self):
        return self.orders.find_one()["items"][0]

    def event(self, status="completed", settled="2026-09-28T10:00:00Z"):
        return {"event": "order." + status, "order_id": "line-1", "status": status,
                "reference": "KT-123", "settled_at": settled}

    def signed(self, payload):
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return raw, signature

    def test_signatures_and_duplicates(self):
        raw, signature = self.signed(self.event())
        for bad in ["", "sha256=x", "é"]:
            self.assertEqual(receive_webhook(raw, bad, self.inbox)[1], 401)
        self.assertEqual(receive_webhook(raw + b" ", signature, self.inbox)[1], 401)
        for _ in range(2):
            self.assertEqual(receive_webhook(raw, signature, self.inbox)[1], 200)
        self.assertEqual(self.inbox.count_documents({}), 1)

    def test_webhook_updates_matching_tenant_only(self):
        self.other.insert_one({"order_id": "other", "items": [{"provider": "bundleportal", "provider_request_order_id": "different"}]})
        for _ in range(2):
            self.assertEqual(apply_event(self.event(), [self.orders, self.other]), "processed")
        self.assertEqual(self.orders.find_one()["status"], "completed")
        self.assertNotIn("status", self.other.find_one())

    def test_ambiguous_reference_not_applied(self):
        self.other.insert_one(self.orders.find_one())
        self.assertEqual(apply_event(self.event(), [self.orders, self.other]), "ambiguous")
        self.assertEqual(self.line()["line_status"], "processing")

    def test_refund_cannot_regress_and_does_not_credit_wallet(self):
        apply_event(self.event("refunded", "2026-09-28T11:00:00Z"), [self.orders])
        apply_event(self.event(), [self.orders])
        self.assertEqual(self.line()["line_status"], "processing")
        self.assertEqual(self.line()["provider_status"], "refunded")
        self.assertTrue(self.line()["refund_review_required"])
        self.assertEqual(self.orders.find_one()["status"], "processing")
        self.assertEqual(self.client.nagonu.wallets.count_documents({}), 0)

    def test_callback_preserves_an_actual_local_wallet_refund(self):
        self.orders.update_one({}, {"$set": {"status": "refunded", "refunded_at": datetime.utcnow(),
                                            "items.0.line_status": "refunded", "items.0.refunded_at": datetime.utcnow()}})
        apply_event(self.event(), [self.orders])
        self.assertEqual(self.orders.find_one()["status"], "refunded")
        self.assertEqual(self.line()["line_status"], "refunded")

    @patch("bundleportal_orders.api.call")
    def test_manual_delivery_cannot_be_submitted_again(self, call):
        self.orders.update_one({}, {"$set": {"items.0.bp_ready": True, "items.0.line_status": "delivered"}})
        process_line(self.orders, "order-1", "line-1")
        call.assert_not_called()

    def test_mixed_order_stays_processing(self):
        self.orders.update_one({}, {"$push": {"items": {"provider": "other", "line_status": "processing"}}})
        apply_event(self.event(), [self.orders])
        self.assertEqual(self.orders.find_one()["status"], "processing")

    @patch("bundleportal_orders.api.call")
    def test_unreleased_order_never_spends(self, call):
        process_line(self.orders, "order-1", "line-1")
        call.assert_not_called()

    @patch("bundleportal_orders.api.preflight", return_value=None)
    @patch("bundleportal_orders.api.call")
    def test_timeout_retries_same_id_skipping_preflight(self, call, preflight):
        call.side_effect = [{"success": False, "code": "network_error", "http_status": 599},
                            {"success": True, "data": {"order_id": "line-1", "status": "cached", "duplicate": True}}]
        dispatch_line(self.orders, "order-1", "line-1")
        self.assertEqual(self.line()["api_status"], "unconfirmed")
        self.orders.update_one({}, {"$set": {"items.0.bp_next_attempt": datetime.utcnow() - timedelta(seconds=1)}})
        process_line(self.orders, "order-1", "line-1")
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual([c.kwargs["order_id"] for c in call.call_args_list], ["line-1", "line-1"])
        self.assertEqual(self.line()["line_status"], "cached")
        self.assertEqual(self.line()["provider_order_id"], "line-1")

    @patch("bundleportal_orders.api.preflight", return_value=None)
    @patch("bundleportal_orders.api.call")
    def test_webhook_wins_race_against_submission_response(self, call, preflight):
        def respond(*args, **kwargs):
            apply_event(self.event(), [self.orders])
            return {"success": True, "data": {"status": "processing"}}
        call.side_effect = respond
        dispatch_line(self.orders, "order-1", "line-1")
        self.assertEqual(self.line()["line_status"], "completed")
        self.assertEqual(self.orders.find_one()["status"], "completed")

    @patch("bundleportal_orders.api.preflight", return_value={"success": False, "code": "not_allowlisted", "http_status": 403})
    @patch("bundleportal_orders.api.call")
    def test_unapproved_number_does_not_spend(self, call, preflight):
        dispatch_line(self.orders, "order-1", "line-1")
        call.assert_not_called()
        self.assertEqual(self.line()["provider_status"], "pending_approval")

    @patch("bundleportal.call")
    def test_catalogue_uses_exact_route_and_fraction(self, call):
        call.side_effect = [{"success": True, "data": {"can_order": True}},
                            {"success": True, "data": {"bundles": [{"size_gb": 0.5}]}}]
        self.assertIsNone(api.preflight("0241234567", "mtn_3", 0.5, "line-1"))
        self.assertEqual(call.call_args.kwargs["network"], "mtn_3")
        self.assertEqual(api.package_size({"size_gb": 0.5}, {}), 0.5)
        self.assertEqual(api.resolve_network({"bundleportal_network": "mtn_3"}, {}, "mtn"), "mtn_3")

    @patch("bundleportal.requests.post")
    def test_v2_auth_and_rate_limit(self, post):
        post.return_value = Mock(ok=False, status_code=429, headers={"Retry-After": "120"})
        post.return_value.json.return_value = {"success": False, "retry_after": 60}
        result = api.call("check_balance")
        self.assertEqual(result["retry_after"], 120)
        self.assertEqual(post.call_args.args[0], "https://api.bundleportal.com/v2")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "bp_live_test")
        self.assertEqual(api.outcome(result), "retry_pending")

    @patch("bundleportal.requests.post", side_effect=requests.Timeout)
    def test_transport_timeout_is_uncertain(self, post):
        self.assertEqual(api.outcome(api.call("place_order"), spending=True), "unconfirmed")

    def test_legacy_mb_values_match_azico_store_display(self):
        self.assertEqual(api.package_size({"volume": 1000}, {}), 1)
        self.assertEqual(api.package_size({"volume": 20000}, {}), 20)
        self.assertEqual(api.package_size({"volume": 500}, {"value": "500MB"}), 0.5)


if __name__ == "__main__":
    unittest.main()
