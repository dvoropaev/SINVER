"""CSRF regression tests; run with python -m unittest discover -s tests -v."""
import ast
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import tempfile
import unittest

from flask import render_template_string, session

import sinver


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"method": attrs.get("method", "get"), "tokens": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("name") == "csrf_token":
            self.current["tokens"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class CSRFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.sqlite"
        self.backups = Path(self.temp.name) / "backups"
        sinver.init_database(self.db, sinver.INIT_SQL_PATH)
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                INSERT INTO roles VALUES (1, 'test', NULL, '#000000');
                INSERT INTO hostings(id, name) VALUES (1, 'test');
                INSERT INTO zones(id, zone, mname, rname, serial, refresh, retry, expire, minimum)
                    VALUES (1, 'example.test', 'ns.example.test', 'admin.example.test', '1', '1', '1', '1', '1');
                INSERT INTO servers(id, hostname) VALUES (1, 'test');
                INSERT INTO ip_addresses(id, ip_type, ip, server_id) VALUES (1, 'IPv4', '192.0.2.1', 1);
                INSERT INTO subdomains(id, record_type, subdomain, zone_id) VALUES (1, 'A', 'www', 1);
            """)
        self.app = sinver.create_app(self.db, self.backups)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def token(self, client=None):
        client = client or self.client
        self.assertEqual(client.get('/roles').status_code, 200)
        with client.session_transaction() as session:
            return session['csrf_token']

    def test_all_post_routes_reject_missing_and_invalid_tokens_without_side_effects(self):
        token = self.token()
        before = self.db.read_bytes()
        self.app.config['POWERDNS_PLAN_CACHE']['sentinel'] = {'preview': {}}
        for rule in self.app.url_map.iter_rules():
            if 'POST' not in rule.methods:
                continue
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f'<int:{arg}>', '1')
            for value in (None, '', 'wrong', 'токен', token + 'x'):
                with self.subTest(url=url, token=value):
                    data = {} if value is None else {'csrf_token': value}
                    response = self.client.post(url, data=data)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(b'CSRF validation failed', response.data)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.backups.exists())
        self.assertIn('sentinel', self.app.config['POWERDNS_PLAN_CACHE'])

    def test_token_is_bound_to_session_and_requires_cookie(self):
        token = self.token()
        other = self.app.test_client()
        self.assertNotEqual(token, self.token(other))
        for client in (other, self.app.test_client(use_cookies=False)):
            self.assertEqual(client.post('/roles/create', data={'csrf_token': token}).status_code, 400)

    def test_valid_token_allows_create_edit_delete_and_multiple_tabs(self):
        token = self.token()
        self.assertEqual(token, self.token())
        for url, data in (
            ('/roles/create', {'role_name': 'created', 'color': '#ffffff'}),
            ('/roles/2', {'role_name': 'edited', 'color': '#ffffff'}),
            ('/roles/2/delete', {}),
        ):
            with self.subTest(url=url):
                response = self.client.post(url, data={**data, 'csrf_token': token})
                self.assertEqual(response.status_code, 302)
                with sqlite3.connect(self.db) as conn:
                    row = conn.execute('SELECT role_name FROM roles WHERE id=2').fetchone()
                self.assertEqual(row, None if url.endswith('/delete') else (data['role_name'],))
        self.assertTrue(self.backups.exists())

    def test_query_parameter_cannot_supply_token(self):
        token = self.token()
        self.assertEqual(self.client.post('/roles/create?csrf_token=' + token).status_code, 400)

    def test_rendered_forms_and_safe_requests(self):
        token = self.token()
        for resource in ('servers', 'ips', 'subdomains', 'roles', 'hostings', 'zones', 'dns'):
            urls = ['/' + resource]
            if resource != 'dns':
                urls.append('/' + resource + '/1')
            for url in urls:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    parser = FormParser()
                    parser.feed(response.get_data(as_text=True))
                    post_forms = [form for form in parser.forms if form['method'] == 'post']
                    self.assertTrue(post_forms)
                    for form in post_forms:
                        self.assertEqual(len(form['tokens']), 1)
                        self.assertEqual(form['tokens'][0]['value'], token)
                        self.assertEqual(form['tokens'][0]['type'], 'hidden')
        self.assertEqual(self.client.head('/roles').status_code, 200)
        self.assertEqual(self.client.options('/roles/create').status_code, 200)
        # A new client receives the hardened session cookie on its first form page.
        cookie = self.app.test_client().get('/roles').headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_conditional_dns_confirmation_forms_render_session_token(self):
        tree = ast.parse(Path(sinver.__file__).read_text())
        template = next(
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and '<h2>PowerDNS preview</h2>' in node.value
        )
        token = self.token()
        for require_force in (False, True):
            with self.subTest(require_force=require_force), self.app.test_request_context('/dns'):
                session['csrf_token'] = token
                html = render_template_string(
                    template, zones_rows=[], selected_zone_id=1,
                    preview={'error': None, 'require_force': require_force, 'plan_token': 'plan'},
                )
                parser = FormParser()
                parser.feed(html)
                self.assertEqual(len(parser.forms), 3)
                for form in parser.forms:
                    self.assertEqual(len(form['tokens']), 1)
                    self.assertEqual(form['tokens'][0]['value'], token)
                self.assertIn('value="1"' if require_force else 'value="plan"', html)

    def test_restart_invalidates_old_session_and_returns_400(self):
        token = self.token()
        cookie = self.client.get_cookie('session').value
        restarted = sinver.create_app(self.db, self.backups).test_client()
        restarted.set_cookie('session', cookie)
        self.assertEqual(restarted.post('/roles/create', data={'csrf_token': token}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
