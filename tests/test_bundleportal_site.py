import hashlib
import hmac
import importlib
import json
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from bson import ObjectId
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

from bundleportal_orders import apply_event, process_line

SERVICES = [("MTN Normal", "mtn"), ("MTN Express", "mtn"), ("Telecel", "telecel"),
            ("AT iShare", "ishare"), ("AT Bigtime", "airteltigo")]


class BundlePortalSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        enable_gridfs_integration()
        cls.db = mongomock.MongoClient().zico
        module = types.ModuleType("db")
        module.db = cls.db
        cls.stack.enter_context(patch.dict(sys.modules, {"db": module}))
        cls.stack.enter_context(patch("requests.post", side_effect=AssertionError("No live HTTP")))
        cls.stack.enter_context(patch("requests.get", side_effect=AssertionError("No live HTTP")))
        cls.stack.enter_context(patch("threading.Thread"))
        cls.checkout = importlib.import_module("checkout")
        cls.admin_services = importlib.import_module("admin_services")
        cls.store = importlib.import_module("routes.store_page")
        cls.webhook = importlib.import_module("bundleportal_webhook")
        for mod in (cls.checkout, cls.store):
            for name in ("jlog", "_background_process_providers", "evaluate_admin_wallet_low_balance", "send_mtn_mashup_order_sms", "log_activity"):
                cls.stack.enter_context(patch.object(mod, name))
        cls.stack.enter_context(patch.object(cls.store, "record_admin_paystack_credit"))
        cls.stack.enter_context(patch.object(cls.store, "_checkout_helpers", {}))
        cls.stack.enter_context(patch.dict("os.environ", {"BUNDLEPORTAL_WEBHOOK_SECRET": "test-secret"}))
        cls.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        cls.app.secret_key = "test-only"
        cls.app.jinja_loader = ChoiceLoader([DictLoader({"admin_sidebar.html": ""}), cls.app.jinja_loader])
        cls.app.register_blueprint(cls.checkout.checkout_bp)
        cls.app.register_blueprint(cls.store.stores_bp)
        cls.app.register_blueprint(cls.admin_services.admin_services_bp)
        cls.app.register_blueprint(cls.webhook.bundleportal_bp)
        cls.app.jinja_env.globals["url_for"] = lambda *args, **kwargs: "/test"

    def setUp(self):
        for name in self.db.list_collection_names():
            self.db[name].delete_many({})
        self.admin, self.agent, self.main = ObjectId(), ObjectId(), ObjectId()
        self.db.users.insert_many([{"_id": self.admin, "role": "admin"},
                                  {"_id": self.agent, "role": "agent", "admin_id": self.admin},
                                  {"_id": self.main, "role": "main_admin"}])
        self.db.balances.insert_many([{"user_id": self.agent, "amount": 1000}, {"user_id": self.admin, "amount": 1000}])
        self.db.stores.insert_one({"slug": "test-store", "owner_id": self.agent, "admin_id": self.admin, "status": "active"})
        self.client = self.app.test_client()

    def service(self, name, **fields):
        doc = {"_id": ObjectId(), "admin_id": self.admin, "name": name, "provider": "bundleportal",
               "type": "API", "status": "OPEN", "availability": "AVAILABLE",
               "offers": [{"value": {"size_gb": 0.5}, "amount": 3}], **fields}
        self.db.services.insert_one(doc)
        return doc

    def cart(self, service):
        phone = "0201234567" if service["name"] == "Telecel" else ("0271234567" if service["name"].startswith("AT") else "0241234567")
        return {"phone": phone, "serviceId": str(service["_id"]), "serviceName": service["name"],
                "value": "0.5GB", "value_obj": {"size_gb": 0.5}, "amount": 5, "base_amount": 3,
                "provider": "portal02"}  # stale cart label must not override saved service

    def checkout_order(self, channel, service, paid=True):
        cart = [self.cart(service)]
        if channel == "dashboard":
            with self.client.session_transaction() as session:
                session.update(user_id=str(self.agent), role="agent", admin_id=str(self.admin))
            return self.client.post("/checkout", json={"cart": cart, "method": "wallet"})
        with patch.object(self.store, "_server_reprice_store_cart", return_value=(cart, 5)), \
             patch.object(self.store, "_verify_paystack", return_value=(paid, {"amount": 503, "currency": "GHS", "channel": "mobile_money"}, "test", {})):
            return self.client.post("/store-checkout/test-store", json={"cart": cart, "method": "paystack_inline", "paystack": {"reference": str(ObjectId())}})

    def test_all_services_reach_v2_from_store_and_dashboard(self):
        for channel in ("store", "dashboard"):
            for name, network in SERVICES:
                with self.subTest(channel=channel, service=name):
                    response = self.checkout_order(channel, self.service(name))
                    self.assertEqual(response.status_code, 200, response.get_json())
                    order = self.db.orders.find_one({"order_id": response.get_json()["order_id"]})
                    line = order["items"][0]
                    self.assertEqual(line["provider"], "bundleportal")
                    self.assertEqual(line["provider_network"], network)
                    self.assertEqual(line["provider_gig"], 0.5)
                    self.assertTrue(line["bp_ready"])
                    self.assertNotEqual(order["order_id"], order["batch_id"])
                    with patch("bundleportal_orders.api.preflight", return_value=None), \
                         patch("bundleportal_orders.api.call", return_value={"success": True, "data": {"status": "processing"}}) as call:
                        process_line(self.db.orders, order["order_id"], line["provider_request_order_id"])
                        self.assertEqual(call.call_args.args[0], "place_order")
                        self.assertEqual(call.call_args.kwargs["network"], network)
                    event = {"event": "order.completed", "order_id": line["provider_request_order_id"], "settled_at": "2026-09-28T12:00:00Z"}
                    self.assertEqual(apply_event(event, [self.db.orders]), "processed")
                    self.assertEqual(self.db.orders.find_one({"order_id": order["order_id"]})["status"], "completed")
                    self.db.orders.delete_many({})

    def test_main_admin_can_save_each_provider_and_propagate_to_tenant(self):
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.main), role="main_admin")
        for name, _ in SERVICES:
            with self.subTest(service=name):
                base = self.service(name, admin_id=None, provider="skplug")
                child = self.service(name, base_service_id=base["_id"], provider="skplug")
                response = self.client.post(f'/admin/services/{base["_id"]}/provider?admin_scope=base', json={"provider": "bundleportal"})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(self.db.services.find_one({"_id": child["_id"]})["provider"], "bundleportal")

    def test_explicit_routes_propagate_and_reach_both_checkouts(self):
        from bundleportal import ROUTE_CHOICES, selected_route
        for key, (network, label) in ROUTE_CHOICES.items():
            names = ['MTN Normal', 'MTN Express'] if network.startswith('mtn') else (['Telecel'] if network == 'telecel' else ['AT iShare'])
            for name in names:
                for channel in ('dashboard', 'store'):
                    with self.subTest(route=key, service=name, channel=channel):
                        with self.client.session_transaction() as session:
                            session.update(user_id=str(self.main), role='main_admin')
                        base = self.service(name, admin_id=None, provider='codecraft')
                        child = self.service(name, base_service_id=base['_id'], provider='codecraft')
                        response = self.client.post(f'/admin/services/{base["_id"]}/provider?admin_scope=base', json={'provider': key})
                        self.assertEqual(response.status_code, 200, response.get_json())
                        self.assertEqual(response.get_json()['provider'], key)
                        child = self.db.services.find_one({'_id': child['_id']})
                        self.assertEqual(child['bundleportal_network'], network)
                        self.assertEqual(selected_route(child), key)
                        response = self.checkout_order(channel, child)
                        self.assertEqual(response.status_code, 200, response.get_json())
                        order = self.db.orders.find_one({'order_id': response.get_json()['order_id']})
                        line = order['items'][0]
                        self.assertEqual(line['provider_network'], network)
                        with patch('bundleportal_orders.api.preflight', return_value=None), patch('bundleportal_orders.api.call', return_value={'success': True, 'data': {'status': 'processing'}}) as call:
                            process_line(self.db.orders, order['order_id'], line['provider_request_order_id'])
                            self.assertEqual(call.call_args.kwargs['network'], network)
                        self.db.orders.delete_many({})

    def test_wrong_service_rejects_explicit_route(self):
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.main), role='main_admin')
        for name, provider in [('Telecel', 'bundleportal_mtn2'), ('MTN Normal', 'bundleportal_telecel'), ('AT Bigtime', 'bundleportal_ishare')]:
            service = self.service(name, admin_id=None)
            response = self.client.post(f'/admin/services/{service["_id"]}/provider?admin_scope=base', json={'provider': provider})
            self.assertEqual(response.status_code, 400)

    def test_services_page_renders_provider_on_cards_and_modals(self):
        for name, _ in SERVICES:
            self.service(name, admin_id=None)
            self.service(name)
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.main), role="main_admin")
        response = self.client.get("/admin/services?admin_scope=base")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True).count('<option value="bundleportal"'), 10)
        response = self.client.get(f"/admin/services?admin_scope={self.admin}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True).count('<option value="bundleportal"'), 15)

    def test_explicit_mtn_route_is_kept_on_both_checkout_paths(self):
        for channel in ("store", "dashboard"):
            with self.subTest(channel=channel):
                response = self.checkout_order(channel, self.service("MTN Express", bundleportal_network="mtn_3"))
                self.assertEqual(response.status_code, 200, response.get_json())
                line = self.db.orders.find_one({"order_id": response.get_json()["order_id"]})["items"][0]
                self.assertEqual(line["provider_network"], "mtn_3")
                self.db.orders.delete_many({})

    def test_callback_updates_only_its_split_order(self):
        cart = [self.cart(self.service("MTN Normal")), self.cart(self.service("Telecel"))]
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.agent), role="agent", admin_id=str(self.admin))
        response = self.client.post("/checkout", json={"cart": cart, "method": "wallet"})
        self.assertEqual(response.status_code, 200, response.get_json())
        ids = response.get_json()["order_ids"]
        self.assertEqual(len(ids), 2)
        first = self.db.orders.find_one({"order_id": ids[0]})
        event = {"event": "order.completed", "order_id": first["items"][0]["provider_request_order_id"], "settled_at": "2026-09-28T12:00:00Z"}
        self.assertEqual(apply_event(event, [self.db.orders]), "processed")
        self.assertEqual(self.db.orders.find_one({"order_id": ids[0]})["status"], "completed")
        self.assertNotEqual(self.db.orders.find_one({"order_id": ids[1]})["status"], "completed")

    def test_non_main_admin_cannot_change_provider(self):
        service = self.service("Telecel")
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.admin), role="admin")
        self.assertEqual(self.client.post(f'/admin/services/{service["_id"]}/provider', json={"provider": "bundleportal"}).status_code, 403)

    def test_unverified_store_payment_creates_no_order(self):
        response = self.checkout_order("store", self.service("Telecel"), paid=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.orders.count_documents({}), 0)

    def test_off_services_remain_manual_on_both_channels(self):
        for channel in ("store", "dashboard"):
            with self.subTest(channel=channel):
                response = self.checkout_order(channel, self.service("Telecel", type="OFF"))
                self.assertEqual(response.status_code, 200, response.get_json())
                line = self.db.orders.find_one({"order_id": response.get_json()["order_id"]})["items"][0]
                self.assertNotIn("bp_ready", line)
                self.assertNotEqual(line.get("api_status"), "queued")
                self.db.orders.delete_many({})

    def test_webhook_is_public_signed_and_deduplicated(self):
        raw = json.dumps({"event": "order.completed", "order_id": "test", "settled_at": "2026-09-28T12:00:00Z"}).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post("/bundleportal/webhook", data=raw).status_code, 401)
        for _ in range(2):
            self.assertEqual(self.client.post("/bundleportal/webhook", data=raw, headers={"X-BundlePortal-Signature": signature}).status_code, 200)
        self.assertEqual(self.db.bundleportal_webhook_events.count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
