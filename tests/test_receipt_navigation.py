import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import Flask, session
from receipt_navigation import receipt_navigation


class ReceiptNavigationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'offline-test'
        for endpoint, path in [
            ('admin_dashboard.admin_dashboard', '/admin/dashboard'),
            ('customer_dashboard.customer_dashboard', '/customer/dashboard'),
            ('stores.store_public_page', '/store/<slug>'),
        ]:
            self.app.add_url_rule(path, endpoint, lambda **kwargs: 'ok')

    def test_admin_receipt_returns_to_admin_dashboard(self):
        for role in ['main_admin', 'admin']:
            with self.app.test_request_context(base_url='https://azico.site'):
                session.update(user_id='admin-id', role=role)
                self.assertEqual(receipt_navigation({})['receipt_return_url'], '/admin/dashboard')

    def test_agent_receipt_returns_to_customer_dashboard(self):
        for role in ['agent', 'customer']:
            with self.app.test_request_context(base_url='https://zishop.site'):
                session.update(user_id='buyer-id', role=role)
                self.assertEqual(receipt_navigation({})['receipt_return_url'], '/customer/dashboard')

    def test_guest_returns_to_store_without_login(self):
        with self.app.test_request_context(base_url='https://nagmart.store'):
            result = receipt_navigation({'store_slug': 'sample-shop', 'user_id': 'admin-id'})
            self.assertEqual(result['receipt_return_url'], '/store/sample-shop')
            self.assertEqual(result['receipt_return_label'], 'Back to Store')
            self.assertNotIn('user_id', session)

    def test_guest_without_store_gets_public_home(self):
        with self.app.test_request_context():
            self.assertEqual(receipt_navigation({})['receipt_return_url'], '/')

    def test_receipt_routes_preserve_host(self):
        source = Path(__file__).resolve().parents[1] / 'app.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                        and n.name == '_desired_host_for_request')
        for host in ['azico.site', 'www.azico.site', 'zishop.site', 'www.zishop.site', 'nagmart.store']:
            for endpoint in ['checkout.invoice_batch_view', 'checkout.invoice_view']:
                namespace = {'request': SimpleNamespace(endpoint=endpoint, path='/invoice-batch/example', host=host),
                             '_host_only': lambda value: value}
                exec(compile(ast.Module(body=[function], type_ignores=[]), '<host-policy>', 'exec'), namespace)
                self.assertIsNone(namespace['_desired_host_for_request']())


if __name__ == '__main__':
    unittest.main()
