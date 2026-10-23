"""Offline reset-link authorization and lifecycle tests; never connect to MongoDB."""
import ast
from datetime import datetime, timedelta
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from bson import ObjectId
from flask import Flask, flash, redirect, url_for
import mongomock
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = Path(__file__).resolve().parents[1]


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = mongomock.MongoClient()['reset_test']
        cls.original = sys.modules.pop('reset', None)
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=cls.database)}):
            cls.reset = importlib.import_module('reset')
        cls.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        cls.app.config.update(TESTING=True, SECRET_KEY='offline-reset-tests')
        cls.app.register_blueprint(cls.reset.reset_bp)
        cls.app.add_url_rule('/login', endpoint='login.login', view_func=lambda: 'login')
        cls.app.add_url_rule('/<slug>/login', endpoint='admin_auth_pages.branded_login', view_func=lambda slug: 'login')
        # Load the actual retired handlers without importing their unrelated integrations.
        for filename, name, path in [('login.py', 'forgot_password', '/forgot-password'),
                                      ('admin_auth_pages.py', 'branded_forgot_password', '/<slug>/forgot-password')]:
            tree = ast.parse((ROOT / filename).read_text())
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            fn.decorator_list = []
            namespace = dict(flash=flash, redirect=redirect, url_for=url_for, _slugify=lambda x: x)
            exec(compile(ast.Module(body=[fn], type_ignores=[]), filename, 'exec'), namespace)
            cls.app.add_url_rule(path, view_func=namespace[name], methods=['GET', 'POST'])

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('reset', None)
        if cls.original is not None:
            sys.modules['reset'] = cls.original

    def setUp(self):
        for collection in self.database.list_collection_names():
            self.database[collection].delete_many({})
        self.client = self.app.test_client()
        self.admin, self.other, self.main, self.agent, self.customer = [ObjectId() for _ in range(5)]
        self.database.users.insert_many([
            {'_id': self.admin, 'role': 'admin'}, {'_id': self.other, 'role': 'admin'},
            {'_id': self.main, 'role': 'main_admin'},
            {'_id': self.agent, 'role': 'agent', 'admin_id': self.admin, 'password': generate_password_hash('oldpass')},
            {'_id': self.customer, 'role': 'customer', 'admin_id': self.admin},
        ])
        self.database.auth_pages.insert_one({'admin_id': self.admin, 'slug': 'my-brand'})
        self.login(self.admin)

    def login(self, user_id, role='admin'):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=str(user_id), role=role)

    def generate(self, user_id=None, **extra):
        return self.client.post('/admin/reset/generate', json={'user_id': str(user_id or self.agent), **extra})

    def path(self, user_id=None):
        response = self.generate(user_id)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['reset_url'].split('azico.site')[1]

    def submit(self, path, password='newpass', confirm='newpass'):
        return self.client.post(path, data={'password': password, 'confirm': confirm})

    def test_admin_can_reset_own_agent_and_customer(self):
        for uid in [self.agent, self.customer]:
            path = self.path(uid)
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Confirm New Password', response.data)
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
            self.assertNotIn(b'chatbit', response.data)

    def test_tenant_and_role_authorization(self):
        self.login(self.other)
        self.assertEqual(self.generate().status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.generate(self.other).status_code, 404)
        self.assertEqual(self.generate(self.main).status_code, 404)
        self.login(self.agent, 'agent')
        self.assertEqual(self.generate().status_code, 403)
        self.login(ObjectId())
        self.assertEqual(self.generate().status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.generate().status_code, 403)

    def test_main_admin_can_reset_subadmin_but_not_main_admin(self):
        self.login(self.main, 'main_admin')
        path = self.path(self.admin)
        response = self.submit(path)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, 'https://azico.site/login')
        self.assertEqual(self.generate(self.main).status_code, 404)

    def test_expiry_is_exactly_12_hours_even_with_ttl_override(self):
        now = datetime(2026, 10, 23, 12)
        with patch.object(self.reset, '_now', return_value=now):
            response = self.generate(ttl_hours=999)
        rec = self.database.password_resets.find_one()
        self.assertEqual(rec['expires_at'], now + timedelta(hours=12))
        self.assertTrue(response.json['expires_at'].endswith('Z'))
        path = response.json['reset_url'].split('azico.site')[1]
        with patch.object(self.reset, '_now', return_value=now + timedelta(hours=12)):
            self.assertEqual(self.client.get(path).status_code, 400)
            self.assertEqual(self.submit(path).status_code, 400)

    def test_success_is_single_use_and_redirects_to_branded_login(self):
        path = self.path()
        with self.client.session_transaction() as session:
            session.clear()
        response = self.submit(path, ' newpass ', ' newpass ')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, 'https://zishop.site/my-brand/login')
        user = self.database.users.find_one({'_id': self.agent})
        self.assertTrue(check_password_hash(user['password'], ' newpass '))
        self.assertNotIn('password_reset_token_hash', user)
        self.assertEqual(self.submit(path).status_code, 400)
        self.assertEqual(self.client.get(path).status_code, 400)

    def test_new_link_invalidates_previous_link(self):
        old, new = self.path(), self.path()
        self.assertEqual(self.submit(old).status_code, 400)
        self.assertEqual(self.submit(new).status_code, 303)

    def test_password_validation_does_not_consume_link(self):
        path = self.path()
        self.assertEqual(self.submit(path, 'abc', 'abc').status_code, 400)
        self.assertEqual(self.submit(path, 'newpass', 'different').status_code, 400)
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.submit(path).status_code, 303)

    def test_unknown_or_deleted_account_and_invalid_token(self):
        self.assertEqual(self.generate(ObjectId()).status_code, 404)
        self.assertEqual(self.client.post('/admin/reset/generate', json={'user_id': 'bad'}).status_code, 400)
        self.assertEqual(self.client.get('/reset/invalid').status_code, 400)
        path = self.path()
        self.database.users.update_one({'_id': self.agent}, {'$set': {'deleted': True}})
        self.assertEqual(self.submit(path).status_code, 400)
        self.assertEqual(self.generate().status_code, 404)

    def test_branded_login_is_required_and_slug_resolved_at_use(self):
        path = self.path()
        self.database.auth_pages.update_one({'admin_id': self.admin}, {'$set': {'slug': 'new-brand'}})
        self.assertEqual(self.submit(path).location, 'https://zishop.site/new-brand/login')
        self.database.auth_pages.delete_many({})
        self.assertEqual(self.generate().status_code, 400)

    def test_reassignment_invalidates_link(self):
        path = self.path()
        self.database.users.update_one({'_id': self.agent}, {'$set': {'admin_id': self.other}})
        self.assertEqual(self.submit(path).status_code, 400)

    def test_replaced_token_cannot_win_a_concurrent_password_update(self):
        path = self.path()
        original_update = self.database.users.update_one
        def replace_then_update(query, update):
            original_update({'_id': self.agent}, {'$set': {'password_reset_token_hash': 'replacement'}})
            return original_update(query, update)
        with patch.object(self.database.users, 'update_one', side_effect=replace_then_update):
            self.assertEqual(self.submit(path).status_code, 400)
        self.assertTrue(check_password_hash(self.database.users.find_one({'_id': self.agent})['password'], 'oldpass'))

    def test_reset_customer_can_login_only_to_own_brand(self):
        from flask import request, session, abort
        tree = ast.parse((ROOT / 'admin_auth_pages.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'branded_login')
        fn.decorator_list = []
        namespace = dict(request=request, session=session, abort=abort, flash=flash,
                         redirect=redirect, url_for=url_for, users_col=self.database.users,
                         auth_pages_col=self.database.auth_pages, _slugify=lambda x: x,
                         _branding_payload=lambda doc: {}, check_password_hash=check_password_hash,
                         log_login_event=lambda *args, **kwargs: None,
                         get_admin_doc=lambda oid: {}, get_maintenance_status=lambda doc: {},
                         render_template=lambda *args, **kwargs: 'login rejected')
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'admin_auth_pages.py', 'exec'), namespace)
        self.database.users.update_one({'_id': self.customer}, {'$set': {'username': 'customer-login'}})
        path = self.path(self.customer)
        self.assertEqual(self.submit(path).status_code, 303)
        # Register the redirect endpoint without importing the dashboard integrations.
        with self.app.test_request_context('/my-brand/login', method='POST', data={'username': 'customer-login', 'password': 'newpass'}):
            with patch.dict(namespace, url_for=lambda endpoint: '/customer/dashboard'):
                response = namespace['branded_login']('my-brand')
            self.assertEqual(response.location, '/customer/dashboard')
            self.assertEqual(session['user_id'], str(self.customer))
        self.database.auth_pages.insert_one({'admin_id': self.other, 'slug': 'other-brand'})
        with self.app.test_request_context('/other-brand/login', method='POST', data={'username': 'customer-login', 'password': 'newpass'}):
            self.assertEqual(namespace['branded_login']('other-brand'), 'login rejected')
            self.assertNotIn('user_id', session)

    def test_retired_public_flows_cannot_reset_password(self):
        for path, target in [('/forgot-password', '/login'), ('/my-brand/forgot-password', '/my-brand/login')]:
            for method in [self.client.get, self.client.post]:
                response = method(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, target)
        self.assertEqual(self.database.password_resets.count_documents({}), 0)


if __name__ == '__main__':
    unittest.main()
