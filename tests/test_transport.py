"""Prevent accidental public backend binds and preserve stdio by default."""
import unittest
from chatgpt_actuator.transport import run_options


class TransportTests(unittest.TestCase):
    def test_default_remains_stdio(self):
        self.assertEqual(run_options({}), {'transport': 'stdio'})

    def test_http_is_stateless_json_on_loopback(self):
        options = run_options({'ACTUATOR_MCP_TRANSPORT': 'http', 'ACTUATOR_MCP_PORT': '8190'})
        self.assertEqual(options['port'], 8190)
        self.assertEqual(options['host'], '127.0.0.1')
        self.assertTrue(options['json_response'])
        self.assertTrue(options['stateless_http'])

    def test_invalid_binds_rejected(self):
        for host in ('0.0.0.0', '::', 'example.com'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                run_options({'ACTUATOR_MCP_TRANSPORT': 'http', 'ACTUATOR_MCP_HOST': host})
        for port in ('0', '65536', 'invalid'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                run_options({'ACTUATOR_MCP_TRANSPORT': 'http', 'ACTUATOR_MCP_PORT': port})
