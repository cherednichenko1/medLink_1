import os
import subprocess
import sys
import unittest

class ProductionConfigTests(unittest.TestCase):
    def run_app(self,url,secure,code='import app'):
        env=dict(os.environ,MEDLINK_ENV='production',PUBLIC_BASE_URL=url,SESSION_COOKIE_SECURE=secure,
                 FLASK_SECRET_KEY='test-only-secret-at-least-thirty-two-characters-long')
        return subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,timeout=10)

    def test_production_refuses_insecure_configuration(self):
        for url,secure in [('http://example.test','true'),('https://example.test','false')]:
            self.assertNotEqual(self.run_app(url,secure).returncode,0)

    def test_production_redirects_http_forms_to_configured_https(self):
        code="from app import app; r=app.test_client().get('/loginPage',headers={'Host':'attacker.test'}); assert r.status_code==302; assert r.location=='https://example.test/loginPage'"
        r=self.run_app('https://example.test','true',code)
        self.assertEqual(r.returncode,0,r.stderr.decode())
