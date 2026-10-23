"""Global MTN phone-history policy managed by Azico's main admin."""
from datetime import datetime
import re

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from db import db

phone_history_bp = Blueprint('phone_history', __name__)
SETTINGS_ID = 'PHONE_HISTORY_SETTINGS'
DEFAULT_MESSAGE = 'This is a New number, order delivery may take 24hrs'


def normalize_phone(value):
    digits = re.sub(r'\D+', '', str(value or ''))
    if digits.startswith('233') and len(digits) == 12:
        digits = '0' + digits[3:]
    elif len(digits) == 9:
        digits = '0' + digits
    return digits


def verification_settings():
    doc = db['phone_verification_settings'].find_one({'_id': SETTINGS_ID}) or {}
    return {
        # Existing installations keep accepting numbers until the main admin enables this.
        'require_existing_mtn_history': bool(doc.get('require_existing_mtn_history', False)),
        'first_time_alert_enabled': bool(doc.get('first_time_alert_enabled', False)),
        'first_time_alert_message': str(doc.get('first_time_alert_message') or DEFAULT_MESSAGE)[:300],
    }


def _service(item):
    raw = item.get('serviceId') or item.get('service_id')
    if not raw:
        return {}
    ids = [raw]
    try:
        ids.append(ObjectId(str(raw)))
    except Exception:
        pass
    return db['services'].find_one({'_id': {'$in': ids}}) or {}


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='phone-history-ack')


def check_phone(item, source='verify_api', settings=None):
    settings = verification_settings() if settings is None else settings
    phone = normalize_phone(item.get('phone'))
    result = {'success': True, 'phone': phone, 'required': False, 'verified': False,
              'allow_order': True, 'warning': False, 'first_time_alert': False,
              'warning_message': '', 'message': '', 'enforcement_enabled': False}
    if not settings['require_existing_mtn_history']:
        return result
    service = _service(item)
    network = ' '.join(str(service.get(k) or '') for k in ('name', 'network', 'service_network')).lower()
    if 'mtn' not in network:
        return result
    result.update(required=True, enforcement_enabled=True)
    if not re.fullmatch(r'0\d{9}', phone):
        return dict(result, success=False, allow_order=False, message='Phone must be 0xxxxxxxxx.')
    variants = [phone, phone[1:], '233' + phone[1:], '+233' + phone[1:]]
    if db['blocked_phone_numbers'].find_one({'normalized_phone': {'$in': variants}, 'is_active': True}):
        return dict(result, allow_order=False, message='This phone number is blocked. Contact support.')
    history = db['orders'].find_one({'$or': [
        {'items.phone': {'$in': variants}}, {'phone': {'$in': variants}},
        {'customer_phone': {'$in': variants}},
    ]}, {'_id': 1})
    # The USSD service also maintains the shared phone-number registry.
    registered = db['phone_numbers'].find_one({'phone_number': {'$in': variants}}, {'_id': 1})
    if history or registered:
        return dict(result, verified=True, message='Number verified.')
    now = datetime.utcnow()
    missing = db['not_in_database_phone_numbers']
    missing.update_one({'normalized_phone': phone}, {
        '$set': {'phone': phone, 'normalized_phone': phone, 'last_seen_at': now,
                 'last_source': source, 'last_service_name': service.get('name', ''),
                 'last_service_network': service.get('service_network', ''),
                 'last_network': service.get('network', ''), 'network_label': 'MTN'},
        '$setOnInsert': {'first_seen_at': now}, '$inc': {'seen_count': 1},
        '$addToSet': {'sources': source},
    }, upsert=True)
    override = missing.find_one({'normalized_phone': phone, 'allow_order_override': True})
    if override:
        return dict(result, verified=True, message='Number approved for ordering.')
    if settings['first_time_alert_enabled']:
        message = settings['first_time_alert_message']
        token = _serializer().dumps({'phone': phone, 'service': str(service['_id']), 'message': message})
        return dict(result, warning=True, first_time_alert=True, message=message,
                    warning_message=message, acknowledgement_token=token)
    return dict(result, allow_order=False, message='Number not in our database so you cannot place an order.')


def validate_phone_history_items(items, source):
    """Recheck at the server before cart/payment/provider work, including saved carts."""
    settings = verification_settings()
    if not settings['require_existing_mtn_history']:
        return None
    for item in items:
        result = check_phone(item, source, settings)
        if not result['allow_order']:
            return result['message']
        if result['warning']:
            try:
                ack = _serializer().loads(item.get('phone_history_ack', ''), max_age=86400)
                expected = _serializer().loads(result['acknowledgement_token'], max_age=86400)
                if ack != expected:
                    raise BadSignature('Acknowledgement does not match this number/service/message')
            except BadSignature:
                return 'Please confirm the first-time number alert before ordering: ' + result['phone']
    return None


@phone_history_bp.post('/api/phone/verify-existing-order')
def verify_existing_order_phone():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, allow_order=False, message='Invalid verification request.'), 400
    if not _service(payload):
        return jsonify(success=False, allow_order=False, message='Service not found.'), 400
    result = check_phone(payload, str(payload.get('source') or 'verify_api')[:80])
    response = jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response
