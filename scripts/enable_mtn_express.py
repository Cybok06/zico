"""Provision MTN EXPRESS for existing admins using the normal cloning/pricing rules.

Run from zico-main: python scripts/enable_mtn_express.py [--apply]
Existing services and store selections are preserved.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bson import ObjectId
from db import db
from service_admin_pricing import apply_admin_pricing_to_offers, normalize_admin_level

BASE_ID = ObjectId('6aa5abb7813a16b250fc5983')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    base = db.services.find_one({'_id': BASE_ID, 'name': 'MTN EXPRESS', 'admin_id': None})
    if not base:
        raise RuntimeError('Expected MTN EXPRESS base service was not found.')
    admins = list(db.users.find({'role': {'$in': ['admin', 'main_admin']}, 'deleted': {'$ne': True}},
                               {'_id': 1, 'admin_level': 1}))
    created = skipped = 0
    for admin in admins:
        query = {'admin_id': admin['_id'], 'base_service_id': BASE_ID}
        if db.services.find_one(query, {'_id': 1}):
            skipped += 1
            continue
        if db.services.find_one({'admin_id': admin['_id'], 'name': {'$regex': '^MTN EXPRESS$', '$options': 'i'}}):
            raise RuntimeError('An unlinked MTN EXPRESS service already exists; reconcile before provisioning.')
        clone = deepcopy(base)
        clone.pop('_id')
        now = datetime.now(timezone.utc)
        clone.update(admin_id=admin['_id'], base_service_id=BASE_ID,
                     created_at=now, updated_at=now, cloned_at=now)
        clone['offers'] = apply_admin_pricing_to_offers(
            base['offers'], clone['offers'], normalize_admin_level(admin.get('admin_level')))
        if not clone['offers'] or any(not isinstance(o.get('amount'), (int, float)) or o['amount'] <= 0 for o in clone['offers']):
            raise RuntimeError('Missing admin offer pricing; no service inserted for this admin.')
        if args.apply:
            result = db.services.update_one(query, {'$setOnInsert': clone}, upsert=True)
            created += int(result.upserted_id is not None)
        else:
            created += 1
    print(f"{'Created' if args.apply else 'Would create'}: {created}; existing: {skipped}; admins: {len(admins)}")
    if args.apply:
        for admin in admins:
            matches = list(db.services.find({'admin_id': admin['_id'], 'base_service_id': BASE_ID}))
            assert len(matches) == 1
            service = matches[0]
            assert service['name'] == 'MTN EXPRESS' and len(service['offers']) == len(base['offers'])
            assert service.get('agent_visible') is not False and service.get('display_enabled') is not False
        print('Verified one visible MTN EXPRESS service per admin with all offers.')


if __name__ == '__main__':
    main()
