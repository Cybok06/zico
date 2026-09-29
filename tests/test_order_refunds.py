import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

from bson import ObjectId
from order_refunds import RefundError, plan_refund, refund_order_wallets


ADMIN, AGENT, ORDER = ObjectId(), ObjectId(), ObjectId()


def order(store=False, multiple=False):
    items = [{'amount': 100, 'admin_base_amount': 70, 'selling_amount': 90,
              'line_status': 'processing'}]
    if multiple:
        items.append({'amount': 50, 'admin_base_amount': 35, 'selling_amount': 45,
                      'line_status': 'processing'})
    factor = 1.5 if multiple else 1
    debits = [{'user_id': ADMIN, 'amount': 70 * factor, 'labels': ['admin_base_debit']}]
    if not store:
        debits.append({'user_id': AGENT, 'amount': 90 * factor, 'labels': ['agent_purchase_debit']})
    return {'_id': ORDER, 'order_id': 'ORDER-test', 'admin_id': ADMIN, 'user_id': AGENT,
            'status': 'processing', 'paid_from': 'paystack' if store else 'wallet',
            'charged_amount': 100 * factor, 'wallet_debit_status': 'completed',
            'wallet_debits': debits, 'items': items}


class Collection:
    def __init__(self, database, name):
        self.database, self.name = database, name

    def find_one(self, query, *, session):
        assert session.active
        return next((deepcopy(d) for d in self.database.data[self.name]
                     if all(d.get(k) == v for k, v in query.items())), None)

    def update_one(self, query, update, *, session):
        assert session.active
        for doc in self.database.data[self.name]:
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(deepcopy(update.get('$set', {})))
                for k, v in update.get('$inc', {}).items():
                    doc[k] = doc.get(k, 0) + v
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def insert_one(self, doc, *, session):
        assert session.active
        if self.database.fail == self.name:
            raise RuntimeError('simulated ledger failure')
        self.database.data[self.name].append(deepcopy(doc))


class Session:
    def __init__(self, database):
        self.database, self.active = database, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def with_transaction(self, callback):
        snapshot = deepcopy(self.database.data)
        self.active = True
        try:
            if self.database.retry:
                callback(self)
                self.database.data = deepcopy(snapshot)
            return callback(self)
        except Exception:
            self.database.data = snapshot
            raise
        finally:
            self.active = False


class Database:
    def __init__(self, doc):
        self.fail, self.retry = None, False
        self.client = self
        self.data = {'orders': [doc], 'balances': [
            {'_id': ObjectId(), 'user_id': ADMIN, 'amount': 10, 'admin_id': ADMIN},
            {'_id': ObjectId(), 'user_id': AGENT, 'amount': 20, 'admin_id': ADMIN},
        ], 'transactions': [], 'balance_logs': []}

    def __getitem__(self, name):
        return Collection(self, name)

    def start_session(self):
        return Session(self)


class RefundTests(unittest.TestCase):
    def refund(self, db, indices=None):
        return refund_order_wallets(orders_collection=db['orders'], order_id=ORDER,
                                   indices=indices, actor_admin_id=ObjectId())

    def test_dashboard_credits_original_two_debits_not_selling_total(self):
        db = Database(order())
        self.assertEqual(self.refund(db), 1)
        self.assertEqual([d['amount'] for d in db.data['balances']], [80, 110])
        self.assertEqual([d['amount'] for d in db.data['transactions']], [70, 90])
        self.assertEqual(len(db.data['balance_logs']), 2)
        self.assertEqual(db.data['orders'][0]['status'], 'refunded')

    def test_store_only_credits_admin(self):
        db = Database(order(store=True))
        self.refund(db)
        self.assertEqual([d['amount'] for d in db.data['balances']], [80, 20])
        self.assertEqual(len(db.data['transactions']), 1)

    def test_partial_then_whole_only_refunds_remaining_amounts(self):
        db = Database(order(multiple=True))
        self.assertEqual(self.refund(db, [0]), 1)
        self.assertEqual([d['amount'] for d in db.data['balances']], [80, 110])
        self.assertEqual(db.data['orders'][0]['status'], 'processing')
        self.assertEqual(self.refund(db), 1)
        self.assertEqual([d['amount'] for d in db.data['balances']], [115, 155])

    def test_repeated_line_and_whole_requests_are_noops(self):
        db = Database(order(multiple=True))
        self.refund(db, [0])
        self.assertEqual(self.refund(db, [0]), 0)
        self.refund(db)
        snapshot = deepcopy(db.data)
        self.assertEqual(self.refund(db), 0)
        self.assertEqual(self.refund(db, [1]), 0)
        self.assertEqual(db.data, snapshot)

    def test_all_individual_lines_complete_order(self):
        db = Database(order(store=True, multiple=True))
        self.refund(db, [0])
        self.refund(db, [1])
        self.assertEqual(db.data['orders'][0]['status'], 'refunded')
        self.assertEqual([d['amount'] for d in db.data['balances']], [115, 20])

    def test_failure_rolls_back_wallets_ledgers_and_status(self):
        for collection in ['transactions', 'balance_logs']:
            db = Database(order())
            original = deepcopy(db.data)
            db.fail = collection
            with self.assertRaises(RuntimeError):
                self.refund(db)
            self.assertEqual(db.data, original)

    def test_missing_second_wallet_rolls_back_first_credit(self):
        db = Database(order())
        db.data['balances'].pop()
        original = deepcopy(db.data)
        with self.assertRaises(RefundError):
            self.refund(db)
        self.assertEqual(db.data, original)

    def test_transaction_callback_retry_credits_once(self):
        db = Database(order())
        db.retry = True
        self.refund(db)
        self.assertEqual([d['amount'] for d in db.data['balances']], [80, 110])
        self.assertEqual(len(db.data['transactions']), 2)

    def test_wallet_lookup_uses_original_user_not_acting_admin(self):
        db = Database(order())
        db.data['balances'][0]['admin_id'] = ObjectId()
        self.refund(db)
        self.assertEqual(len(db.data['balances']), 2)
        self.assertEqual(db.data['balances'][0]['amount'], 80)

    def test_missing_debit_evidence_fails_without_writes(self):
        db = Database(order())
        del db.data['orders'][0]['wallet_debits']
        original = deepcopy(db.data)
        with self.assertRaises(RefundError):
            self.refund(db)
        self.assertEqual(db.data, original)

    def test_legacy_partial_refund_is_not_guessed(self):
        doc = order(multiple=True)
        doc['items'][0].update(refunded_at='old', refunded_amount=100)
        with self.assertRaises(RefundError):
            plan_refund(doc)

    def test_mismatched_line_amounts_reject_partial_but_allow_exact_whole(self):
        doc = order(multiple=True)
        doc['items'][0]['admin_base_amount'] = 999
        with self.assertRaises(RefundError):
            plan_refund(doc, [0])
        self.assertEqual([r['amount'] for r in plan_refund(doc)[0]], [105, 135])

    def test_shared_wallet_labels_allocate_both_debits(self):
        doc = order(multiple=True)
        doc['wallet_debits'] = [{'user_id': ADMIN, 'amount': 240,
                                'labels': ['admin_base_debit', 'agent_purchase_debit']}]
        self.assertEqual(plan_refund(doc, [0])[0], [{'user_id': ADMIN, 'amount': 160}])

    def test_completed_orders_are_refundable(self):
        db = Database(order())
        db.data['orders'][0]['status'] = 'completed'
        self.assertEqual(self.refund(db), 1)

    def test_nonfinite_amounts_rejected(self):
        for amount in ['NaN', 'Infinity', -10]:
            doc = order()
            doc['wallet_debits'][0]['amount'] = amount
            with self.assertRaises(RefundError):
                plan_refund(doc)

    def test_status_entrypoints_delegate_without_importing_live_app(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'admin_orders.py').read_text(encoding='utf-8'))
        names = {'_apply_status_change', '_apply_line_status_change'}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        calls = []
        namespace = {'List': list, 'Tuple': tuple, 'ObjectId': ObjectId, 'orders_col': None,
                     'datetime': __import__('datetime').datetime,
                     '_parse_line_id': lambda value: ('main', ORDER, 0),
                     '_clear_dashboard_cache_safely': lambda: None,
                     'refund_order_wallets': lambda **kw: calls.append(kw) or 1}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<entrypoints>', 'exec'), namespace)
        self.assertEqual(namespace['_apply_status_change']([ORDER], 'refunded'), (1, []))
        self.assertEqual(namespace['_apply_line_status_change']([f'{ORDER}:0'], 'refunded'), (1, []))
        self.assertEqual(calls[1]['indices'], {0})


if __name__ == '__main__':
    unittest.main()
