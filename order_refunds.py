"""Reverse recorded order wallet debits, atomically with refund status and ledgers."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation

from bson import ObjectId


class RefundError(ValueError):
    pass


def _cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return int((amount * 100).quantize(Decimal('1')))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise RefundError('Invalid recorded wallet amount.')


def _status(value):
    value = str(value or '').strip().lower()
    return 'delivered' if value == 'completed' else value


def _rows_by_user(rows):
    result = {}
    for row in rows:
        try:
            uid = ObjectId(str(row['user_id']))
            amount = _cents(row['amount'])
        except (KeyError, TypeError, ValueError):
            raise RefundError('Invalid recorded wallet debit/refund.')
        result[uid] = result.get(uid, 0) + amount
    return result


def plan_refund(order, indices=None):
    """Use saved debits, never current prices or the public selling total."""
    if order.get('refunded_at') or _status(order.get('status')) == 'refunded':
        return [], {}, []
    if 'wallet_debits' not in order or not isinstance(order['wallet_debits'], list):
        raise RefundError('Order has no recorded wallet debits; reconcile its purchase ledger before refunding.')
    if order.get('wallet_debit_status') not in (None, 'completed'):
        raise RefundError('Order wallet debit was not completed.')
    debits = order['wallet_debits']
    totals = _rows_by_user(debits)
    items = order.get('items') or []
    already = {}
    for item in items:
        if item.get('refunded_at') or _status(item.get('line_status')) == 'refunded':
            if 'wallet_refunds' not in item:
                raise RefundError('Order has a legacy line refund; reconcile it before issuing another refund.')
            for uid, amount in _rows_by_user(item['wallet_refunds']).items():
                already[uid] = already.get(uid, 0) + amount
    if any(amount > totals.get(uid, 0) for uid, amount in already.items()):
        raise RefundError('Recorded refunds exceed the original wallet debits.')

    def rows(amounts):
        return [{'user_id': uid, 'amount': amount / 100} for uid, amount in amounts.items() if amount]

    if indices is None:
        return rows({uid: amount - already.get(uid, 0) for uid, amount in totals.items()}), {}, list(range(len(items)))

    selected = sorted(set(indices))
    if any(i < 0 or i >= len(items) for i in selected):
        raise RefundError('Refund line item not found.')
    selected = [i for i in selected if not items[i].get('refunded_at') and _status(items[i].get('line_status')) != 'refunded']
    if not selected:
        return [], {}, []
    allocations = [{} for _ in items]
    if len(items) == 1:
        allocations[0] = totals.copy()
    else:
        for debit in debits:
            uid = ObjectId(str(debit['user_id']))
            labels = set(debit.get('labels') or [debit.get('label')])
            if not labels or not labels <= {'admin_base_debit', 'agent_purchase_debit'}:
                raise RefundError('Cannot allocate this wallet debit to individual lines; refund the whole order.')
            allocated = []
            for item in items:
                if str(item.get('line_status') or '').startswith('skipped') or _cents(item.get('amount', 0)) == 0:
                    amount = 0
                else:
                    amount = sum(_cents(item.get(field)) for label, field in (
                        ('admin_base_debit', 'admin_base_amount'),
                        ('agent_purchase_debit', 'selling_amount'),
                    ) if label in labels)
                allocated.append(amount)
            if sum(allocated) != _cents(debit['amount']):
                raise RefundError('Line amounts do not match the recorded wallet debit; refund the whole order or reconcile first.')
            for allocation, amount in zip(allocations, allocated):
                allocation[uid] = allocation.get(uid, 0) + amount
    credits = {}
    line_rows = {}
    for i in selected:
        if _status(items[i].get('line_status')) in {'cancelled', 'canceled'}:
            raise RefundError('Cancelled line cannot be refunded through a status change.')
        line_rows[i] = rows(allocations[i])
        for uid, amount in allocations[i].items():
            credits[uid] = credits.get(uid, 0) + amount
    if any(amount + already.get(uid, 0) > totals.get(uid, 0) for uid, amount in credits.items()):
        raise RefundError('Refund would exceed the original wallet debit.')
    return rows(credits), line_rows, selected


def refund_order_wallets(*, orders_collection, order_id, indices=None, actor_admin_id=None,
                         reason='manual', api_status=None):
    """Transaction retries serialize simultaneous whole-order and line refunds.

    Every write uses the same MongoDB session. A failed wallet/ledger write
    aborts the refund and status change together; there is no non-atomic fallback.
    """
    database = orders_collection.database

    def apply(session):
        order = orders_collection.find_one({'_id': order_id}, session=session)
        if not order:
            raise RefundError('Order not found.')
        if order.get('refunded_at') or _status(order.get('status')) == 'refunded':
            return 0
        if _status(order.get('status')) not in {'pending', 'processing', 'delivered'}:
            raise RefundError('Order status does not allow a refund.')
        credits, line_rows, selected = plan_refund(order, indices)
        if indices is not None and not selected:
            return 0
        now = datetime.utcnow()
        items = deepcopy(order.get('items') or [])
        for i in selected:
            item = items[i]
            if item.get('refunded_at') or _status(item.get('line_status')) == 'refunded':
                continue
            item.update(line_status='refunded', refunded_at=now, provider_status_checked_at=now)
            if indices is not None:
                item['wallet_refunds'] = line_rows[i]
                item['refunded_amount'] = sum(row['amount'] for row in line_rows[i])
            if api_status:
                item['api_status'] = api_status
        complete = indices is None or all(_status(item.get('line_status')) == 'refunded' for item in items)
        update = {'items': items, 'updated_at': now, 'wallet_refund_version': 1}
        if complete:
            update.update(status='refunded', refunded_at=now)
        elif _status(order.get('status')) == 'delivered':
            update['status'] = 'processing'
        previous = _rows_by_user(order.get('wallet_refunds') or [])
        for uid, amount in _rows_by_user(credits).items():
            previous[uid] = previous.get(uid, 0) + amount
        update['wallet_refunds'] = [{'user_id': uid, 'amount': amount / 100} for uid, amount in previous.items()]
        # Write the order first: competing transactions conflict on this document.
        orders_collection.update_one({'_id': order_id}, {'$set': update}, session=session)
        for row in credits:
            balances = database['balances']
            # Checkout identifies wallets by user_id, not by the acting admin.
            wallet = balances.find_one({'user_id': row['user_id']}, session=session)
            if not wallet:
                raise RefundError('Original debited wallet no longer exists; reconcile before refunding.')
            amount = row['amount']
            before = float(wallet.get('amount') or 0)
            result = balances.update_one({'_id': wallet['_id']}, {
                '$inc': {'amount': amount}, '$set': {'updated_at': now},
            }, session=session)
            if result.matched_count != 1:
                raise RefundError('Original wallet could not be credited.')
            meta = {'order_db_id': order_id, 'actor_admin_id': actor_admin_id,
                    'item_indices': selected if indices is not None else None,
                    'note': f'{reason} refund of recorded wallet debit', 'refund_version': 1}
            database['transactions'].insert_one({
                **row, 'admin_id': order.get('admin_id'), 'amount': amount,
                'reference': order.get('order_id'), 'status': 'success', 'type': 'refund',
                'gateway': 'Wallet', 'currency': 'GHS', 'created_at': now,
                'verified_at': now, 'meta': meta,
            }, session=session)
            database['balance_logs'].insert_one({
                **row, 'admin_id': order.get('admin_id'), 'action': 'refund',
                'delta': amount, 'amount_before': before, 'amount_after': round(before + amount, 2),
                'currency': 'GHS', 'order_id': order.get('order_id'),
                'source': 'admin_order_refund', 'created_at': now, 'meta': meta,
            }, session=session)
        return len(selected) if indices is not None else 1

    with database.client.start_session() as session:
        return session.with_transaction(apply)
