"""Prevent accidental public backend binds and preserve stdio by default."""
import sys
import unittest
from unittest.mock import Mock, patch
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

    def _run_main(self, environ):
        from chatgpt_actuator import server

        admin = Mock()
        with patch.dict('os.environ', environ, clear=True), \
                patch.object(server.dom_host, 'start'), \
                patch.object(server.dom_host, 'stop'), \
                patch.object(server, 'AdminServer', return_value=admin), \
                patch.object(server.mcp, 'run') as run:
            server.main()
        admin.start.assert_called_once_with()
        admin.stop.assert_called_once_with()
        return run.call_args.kwargs

    @unittest.skipUnless(sys.platform == 'win32', 'MCP server startup requires Windows')
    def test_main_forwards_default_stdio_options(self):
        self.assertEqual(self._run_main({}), {'transport': 'stdio'})

    @unittest.skipUnless(sys.platform == 'win32', 'MCP server startup requires Windows')
    def test_main_forwards_http_options_and_optional_gateway_security(self):
        options = self._run_main({
            'ACTUATOR_MCP_TRANSPORT': 'http',
            'ACTUATOR_MCP_HOST': 'localhost',
            'ACTUATOR_MCP_PORT': '8190',
            'ACTUATOR_MCP_HOSTNAME': 'gateway.example.test',
        })
        self.assertEqual(options['transport'], 'streamable-http')
        self.assertEqual(options['host'], 'localhost')
        self.assertEqual(options['port'], 8190)
        self.assertTrue(options['stateless_http'])
        self.assertTrue(options['json_response'])
        security = options['transport_security']
        self.assertIn('localhost:*', security.allowed_hosts)
        self.assertIn('gateway.example.test:*', security.allowed_hosts)
        self.assertEqual(security.allowed_origins, ['https://gateway.example.test'])

    @unittest.skipUnless(sys.platform == 'win32', 'MCP server startup requires Windows')
    def test_main_http_without_gateway_keeps_loopback_host_allowlist(self):
        options = self._run_main({'ACTUATOR_MCP_TRANSPORT': 'http'})
        security = options['transport_security']
        self.assertEqual(security.allowed_hosts, ['127.0.0.1:*', 'localhost:*', '[::1]:*'])
        self.assertEqual(security.allowed_origins, [])
