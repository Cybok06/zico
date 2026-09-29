import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple
import unittest

from bson import ObjectId
from jinja2 import Environment
import mongomock

ROOT = Path(__file__).resolve().parents[1]


class ExpressVisibilityTests(unittest.TestCase):
    def test_dashboard_tile_keeps_express_name_and_is_orderable(self):
        source = (ROOT / 'templates/customer_dashboard.html').read_text(encoding='utf-8')
        start = source.index('{% macro service_tile(')
        end = source.index('{%- endmacro %}', start) + len('{%- endmacro %}')
        template = Environment(autoescape=True).from_string(source[start:end] + '{{ service_tile(svc) }}')
        html = template.render(svc=dict(name='MTN EXPRESS', display_name='MTN EXPRESS',
            _id_str=str(ObjectId()), status='OPEN', availability='AVAILABLE', offers=[{'amount': 10}],
            network='MTN', service_network='MTN', image_url='', is_social_boosting=False))
        self.assertIn('<div class="service-name">MTN EXPRESS</div>', html)
        self.assertIn('Available', html)
        self.assertNotIn('data-disabled="true"', html)

    def test_store_allows_express_alongside_regular_but_keeps_other_tenant_out(self):
        tree = ast.parse((ROOT / 'routes/store_create.py').read_text(encoding='utf-8'))
        name = '_enforce_mtn_exclusive_selection'
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        services = mongomock.MongoClient().test.services
        admin, other_admin = ObjectId(), ObjectId()
        mtn, normal, express, other = [ObjectId() for _ in range(4)]
        services.insert_many([
            {'_id': mtn, 'name': 'MTN', 'network': 'MTN', 'admin_id': admin},
            {'_id': normal, 'name': 'MTN NORMAL', 'network': 'MTN', 'admin_id': admin},
            {'_id': express, 'name': 'MTN EXPRESS', 'network': 'MTN', 'admin_id': admin},
            {'_id': other, 'name': 'MTN', 'network': 'MTN', 'admin_id': other_admin},
        ])
        namespace = dict(Any=Any, Dict=Dict, List=List, Tuple=Tuple, ObjectId=ObjectId,
            services_col=services, session={}, current_admin_id_from_session=lambda session: admin,
            _extract_selected_service_ids=lambda payload: ('service_ids', payload['service_ids']))
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<store-selection>', 'exec'), namespace)
        check = lambda ids: namespace[name]({'service_ids': [str(value) for value in ids]})[0]
        self.assertTrue(check([mtn, express]))
        self.assertTrue(check([normal, express]))
        self.assertFalse(check([mtn, normal]))
        self.assertTrue(check([mtn, express, other]))  # Another tenant's service cannot affect this rule.


if __name__ == '__main__':
    unittest.main()
