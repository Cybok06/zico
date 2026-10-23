"""Receipt return links follow the viewer's session, never the order owner's role."""
from flask import session, url_for

from tenant import is_admin_role


def receipt_navigation(order):
    role = str(session.get('role') or '').strip().lower()
    if session.get('user_id'):
        if is_admin_role(role):
            return {'receipt_return_url': url_for('admin_dashboard.admin_dashboard'),
                    'receipt_return_label': 'Dashboard'}
        if role in {'agent', 'customer'}:
            return {'receipt_return_url': url_for('customer_dashboard.customer_dashboard'),
                    'receipt_return_label': 'Dashboard'}
    if order.get('store_slug'):
        return {'receipt_return_url': url_for('stores.store_public_page', slug=order['store_slug']),
                'receipt_return_label': 'Back to Store'}
    return {'receipt_return_url': '/', 'receipt_return_label': 'Home'}
