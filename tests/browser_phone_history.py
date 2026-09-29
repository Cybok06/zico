"""Opt-in headless browser smoke test using only an in-memory database."""
from pathlib import Path
import threading
import re

from flask import render_template_string, session
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from test_phone_history import PhoneHistoryTests


def main():
    PhoneHistoryTests.setUpClass()
    fixture = PhoneHistoryTests()
    fixture.setUp()
    fixture.settings(enabled=False)
    app = fixture.app
    app.static_folder = str(Path(__file__).resolve().parents[1] / 'static')
    # Use Azico's real sidebar; unregistered navigation destinations are inert in this fixture.
    app.jinja_loader = app.jinja_loader.loaders[1]
    app.url_build_error_handlers.append(lambda error, endpoint, values: '/fixture/' + endpoint)
    sidebar = (Path(__file__).resolve().parents[1] / 'templates/main_admin_sidebar.html').read_text(encoding='utf-8')
    counters = dict.fromkeys(re.findall(r'(\w+_count) > 0', sidebar), 0)
    app.context_processor(lambda: {**counters, 'endpoint_exists': lambda endpoint: False, 'internal_chat_unread_count': lambda: 0})

    @app.before_request
    def local_login():
        session.update(role='main_admin', user_id='000000000000000000000001', username='Main Admin')

    @app.get('/fixture/phone-form')
    def phone_form():
        return render_template_string('''<!doctype html><html><body>
        <input id="phone" value="0241234567"><button id="add">Add to Cart</button><div id="result"></div>
        <script src="/static/phone_history.js"></script>
        <script>
        document.getElementById('add').onclick = async () => {
          const line = {serviceId: {{ service_id|tojson }}, phone: document.getElementById('phone').value};
          const ok = await verifyPhoneHistory(line, document.getElementById('phone'), false);
          document.getElementById('result').textContent = ok ? 'added' : 'cancelled';
          window.savedLine = line;
        };
        </script></body></html>''', service_id=str(fixture.mtn))

    server = make_server('127.0.0.1', 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    output = Path(__file__).resolve().parents[1] / 'artifacts'
    output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width': 1600, 'height': 1100})
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/admin/phone-numbers', wait_until='networkidle')
            page.get_by_text('MTN Phone-History Verification', exact=True).wait_for()
            assert page.locator('.hero').bounding_box()['x'] >= 286
            page.screenshot(path=str(output / 'azico-phone-numbers-desktop.png'), full_page=True, animations='disabled')
            page.locator('#requireExistingMtnHistory').check()
            page.get_by_role('button', name='Save Setting').click()
            page.locator('#firstTimeAlertEnabled').check()
            page.locator('#firstTimeAlertMessage').fill('New MTN number: delivery may take 24 hours.')
            page.get_by_role('button', name='Save Alert').click()
            assert fixture.database.phone_verification_settings.find_one({})['first_time_alert_enabled']
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(output / 'azico-phone-numbers-mobile.png'), full_page=True, animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile page overflows'
            page.goto(base + '/fixture/phone-form')
            page.get_by_role('button', name='Add to Cart').click()
            page.get_by_role('button', name='Cancel', exact=True).click()
            page.wait_for_function("document.getElementById('result').textContent === 'cancelled'")
            page.get_by_role('button', name='Add to Cart').click()
            page.get_by_role('button', name='Proceed', exact=True).click()
            page.wait_for_function("document.getElementById('result').textContent === 'added'")
            assert page.evaluate('!!window.savedLine.phone_history_ack')
            assert not errors, errors
            browser.close()
        print('Browser checks passed: settings save, desktop/mobile layout, Cancel, Proceed and acknowledgement.')
    finally:
        server.shutdown()
        thread.join(timeout=5)
        PhoneHistoryTests.tearDownClass()


if __name__ == '__main__':
    main()
