"""Offline feature tests: all collections are in-memory, never the live database."""
from datetime import datetime
import importlib
from io import BytesIO
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from bson import ObjectId
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader
import mongomock
from openpyxl import load_workbook


class PhoneHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = mongomock.MongoClient()['azico_test']
        modules = ['phone_history', 'admin_phone_numbers', 'admin_phone_numbers_legacy', 'cart_api']
        cls.old_modules = {name: sys.modules.pop(name, None) for name in modules}
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=cls.database)}):
            cls.policy = importlib.import_module('phone_history')
            cls.admin = importlib.import_module('admin_phone_numbers')
            cls.cart = importlib.import_module('cart_api')
        cls.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        cls.app.config.update(TESTING=True, SECRET_KEY='offline-phone-history-test')
        cls.app.jinja_loader = ChoiceLoader([DictLoader({'admin_sidebar.html': ''}), cls.app.jinja_loader])
        cls.app.add_url_rule('/login', endpoint='login.login', view_func=lambda: 'login')
        cls.app.register_blueprint(cls.policy.phone_history_bp)
        cls.app.register_blueprint(cls.admin.admin_phone_numbers_bp)
        cls.app.register_blueprint(cls.cart.cart_api_bp)

    @classmethod
    def tearDownClass(cls):
        for name, original in cls.old_modules.items():
            sys.modules.pop(name, None)
            if original is not None:
                sys.modules[name] = original

    def setUp(self):
        for name in self.database.list_collection_names():
            self.database[name].delete_many({})
        self.client = self.app.test_client()
        self.mtn, self.express, self.telecel = ObjectId(), ObjectId(), ObjectId()
        self.database.services.insert_many([
            {'_id': self.mtn, 'name': 'MTN', 'network': 'MTN'},
            {'_id': self.express, 'name': 'MTN EXPRESS', 'network': 'MTN'},
            {'_id': self.telecel, 'name': 'Telecel', 'network': 'TELECEL'},
        ])
        self.line = {'serviceId': str(self.mtn), 'serviceName': 'MTN', 'phone': '0241234567', 'amount': 10}

    def settings(self, enabled=True, alert=False, message='Please allow 24 hours.'):
        self.database.phone_verification_settings.update_one({'_id': self.policy.SETTINGS_ID}, {'$set': {
            'require_existing_mtn_history': enabled, 'first_time_alert_enabled': alert,
            'first_time_alert_message': message,
        }}, upsert=True)

    def login(self, role='main_admin'):
        with self.client.session_transaction() as session:
            session['role'] = role
            session['user_id'] = str(ObjectId())

    def verify(self, line=None):
        return self.client.post('/api/phone/verify-existing-order', json=line or self.line).get_json()

    def test_default_off_and_master_off_suppress_alerts(self):
        self.assertTrue(self.verify()['allow_order'])
        self.settings(enabled=False, alert=True)
        result = self.verify()
        self.assertFalse(result['warning'])
        self.assertFalse(result['required'])
        self.assertEqual(self.database.not_in_database_phone_numbers.count_documents({}), 0)

    def test_unknown_mtn_blocked_and_recorded(self):
        self.settings()
        result = self.verify()
        self.assertFalse(result['allow_order'])
        row = self.database.not_in_database_phone_numbers.find_one({})
        self.assertEqual(row['normalized_phone'], self.line['phone'])
        self.assertEqual(row['network_label'], 'MTN')

    def test_history_normalizes_international_phone(self):
        self.settings()
        self.database.orders.insert_one({'items': [{'phone': '+233241234567'}]})
        self.assertTrue(self.verify()['verified'])

    def test_ussd_registry_is_recognized(self):
        self.settings()
        self.database.phone_numbers.insert_one({'phone_number': self.line['phone']})
        self.assertTrue(self.verify()['verified'])

    def test_mtn_express_is_covered(self):
        self.settings()
        self.assertFalse(self.verify(dict(self.line, serviceId=str(self.express)))['allow_order'])

    def test_non_mtn_is_unaffected_even_with_spoofed_client_name(self):
        self.settings()
        self.assertTrue(self.verify(dict(self.line, serviceId=str(self.telecel)))['allow_order'])
        self.assertFalse(self.verify(dict(self.line, serviceName='Telecel', network='TELECEL'))['allow_order'])

    def test_warning_requires_matching_acknowledgement_on_server(self):
        self.settings(alert=True)
        result = self.verify()
        self.assertTrue(result['first_time_alert'])
        self.assertEqual(result['warning_message'], 'Please allow 24 hours.')
        with self.app.app_context():
            self.assertIsNotNone(self.policy.validate_phone_history_items([self.line], 'test'))
            line = dict(self.line, phone_history_ack=result['acknowledgement_token'])
            self.assertIsNone(self.policy.validate_phone_history_items([line], 'test'))
            self.assertIsNotNone(self.policy.validate_phone_history_items([dict(line, phone='0247654321')], 'test'))
            self.settings(alert=True, message='Updated delay warning')
            self.assertIsNotNone(self.policy.validate_phone_history_items([line], 'test'))

    def test_approval_allows_new_number_but_not_explicitly_blocked_number(self):
        self.settings(alert=True)
        self.verify()
        self.login()
        response = self.client.post('/admin/phone-numbers/not-in-database/unblock', data={'phone': self.line['phone']})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.verify()['verified'])
        self.client.post('/admin/phone-numbers/block', data={'phone': self.line['phone'], 'reason': 'test'})
        self.assertFalse(self.verify()['allow_order'])
        self.client.post('/admin/phone-numbers/unblock', data={'phone': self.line['phone']})
        self.assertTrue(self.verify()['allow_order'])

    def test_main_admin_can_save_both_settings_and_message_is_limited(self):
        self.login()
        self.client.post('/admin/phone-numbers/verification-setting', data={'require_existing_mtn_history': 'on'})
        self.client.post('/admin/phone-numbers/first-time-alert-setting', data={
            'first_time_alert_enabled': 'on', 'first_time_alert_message': 'x' * 350})
        doc = self.database.phone_verification_settings.find_one({})
        self.assertTrue(doc['require_existing_mtn_history'])
        self.assertTrue(doc['first_time_alert_enabled'])
        self.assertEqual(len(doc['first_time_alert_message']), 300)

    def test_other_roles_cannot_change_global_settings_or_approve_numbers(self):
        for role in ['admin', 'agent', 'customer']:
            self.login(role)
            for path in ['verification-setting', 'first-time-alert-setting', 'not-in-database/unblock', 'bulk-unblock']:
                response = self.client.post('/admin/phone-numbers/' + path, data={'phone': self.line['phone']})
                self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.phone_verification_settings.count_documents({}), 0)
        self.assertEqual(self.database.not_in_database_phone_numbers.count_documents({}), 0)

    def test_page_has_matching_controls_and_unknown_number_list(self):
        self.settings()
        self.verify()
        self.login()
        response = self.client.get('/admin/phone-numbers?status=not_in_database')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for text in ['First-Time Alert', 'Alert message', 'MTN Phone-History Verification', 'Start Date',
                     'End Date', 'Export PDF', 'Export Excel', 'Not in Database', '0241234567']:
            self.assertIn(text, html)

    def test_bulk_approval_and_network_filtered_excel_pdf(self):
        self.settings()
        self.verify()
        self.login()
        self.client.post('/admin/phone-numbers/bulk-unblock', data={
            'status': 'not_in_database', 'scope': 'all_filtered', 'network': 'MTN'})
        self.assertTrue(self.verify()['allow_order'])
        response = self.client.get('/admin/phone-numbers/export/excel?status=not_in_database&network=MTN')
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.data))
        self.assertEqual(workbook.active['A1'].value, 'MTN')
        self.assertEqual(workbook.active['A2'].value, self.line['phone'])
        response = self.client.get('/admin/phone-numbers/export/pdf?status=not_in_database&network=MTN')
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_cart_rejects_unapproved_numbers_and_preserves_ack(self):
        self.settings(alert=True)
        self.login('agent')
        for path in ['add_bulk', 'replace']:
            response = self.client.post('/api/cart/' + path, json={'items': [self.line]})
            self.assertEqual(response.status_code, 400)
        token = self.verify()['acknowledgement_token']
        response = self.client.post('/api/cart/add_bulk', json={'items': [dict(self.line, phone_history_ack=token)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['items'][0]['phone_history_ack'], token)

    def test_order_list_filters_network_and_dates_and_excludes_social_targets(self):
        self.login()
        self.database.orders.insert_many([
            {'order_id': 'one', 'created_at': datetime(2026, 9, 10), 'items': [{'phone': '0241234567', 'serviceName': 'MTN'}]},
            {'order_id': 'two', 'created_at': datetime(2026, 8, 1), 'items': [{'phone': '0201234567', 'serviceName': 'Telecel'}]},
            {'order_id': 'social', 'created_at': datetime(2026, 9, 10), 'items': [{'phone': 'https://example.com/profile', 'serviceName': 'Boosting'}]},
        ])
        with self.app.test_request_context('/admin/phone-numbers'):
            total, rows = self.admin._fetch_phone_rows('', network='MTN', start_date='2026-09-01', end_date='2026-09-12')
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]['phone'], '0241234567')
            total, rows = self.admin._fetch_phone_rows('')
            self.assertEqual(total, 2)
            self.assertEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
