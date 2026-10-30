import contextlib
import io
import json
import os
import socket
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'script'))
import importlib.util

spec = importlib.util.spec_from_file_location('probe_ca', os.path.join(os.path.dirname(__file__), '..', 'script', 'validar-ca-egress-host-v69.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FakeSocket:
    def __init__(self):
        self.closed = False
    def getsockname(self):
        return ('192.168.0.21', 40211)
    def settimeout(self, value):
        self.timeout = value
    def close(self):
        self.closed = True
    def sendall(self, value):
        self.request = value
    def version(self):
        return 'TLSv1.3'


class ProbeTests(unittest.TestCase):
    def test_tcp_timeout_is_not_tls_failure(self):
        with mock.patch.object(probe.socket, 'create_connection', side_effect=socket.timeout()), contextlib.redirect_stdout(io.StringIO()) as output:
            result = probe.request('example.com', '104.21.44.19', '/api/health')
        self.assertEqual(result, (False, 'TCP'))
        self.assertIn('stage=TCP', output.getvalue())

    def test_tls_error_has_specific_stage(self):
        fake = FakeSocket()
        with mock.patch.object(probe.socket, 'create_connection', return_value=fake), mock.patch.object(probe.ssl, 'create_default_context') as ssl_context, contextlib.redirect_stdout(io.StringIO()) as output:
            ssl_context.return_value.wrap_socket.side_effect = ssl.SSLError('wrong certificate')
            result = probe.request('example.com', '104.21.44.19', '/api/health')
        self.assertEqual(result, (False, 'TLS'))
        self.assertIn('stage=TLS', output.getvalue())

    def test_valid_http_json(self):
        fake = FakeSocket()
        response = mock.Mock(status=200)
        response.read.return_value = json.dumps({'database': True}).encode()
        response.getheader.return_value = 'application/json'
        with mock.patch.object(probe.socket, 'create_connection', return_value=fake), mock.patch.object(probe.ssl, 'create_default_context') as ssl_context, mock.patch.object(probe.http.client, 'HTTPResponse', return_value=response), contextlib.redirect_stdout(io.StringIO()) as output:
            ssl_context.return_value.wrap_socket.return_value = fake
            result = probe.request('example.com', '104.21.44.19', '/api/health')
        self.assertEqual(result, (True, 'OK'))
        self.assertIn('valid=true', output.getvalue())
        self.assertIn('Host: example.com', fake.request.decode())

    def test_invalid_json_fails_verification(self):
        fake = FakeSocket()
        response = mock.Mock(status=200)
        response.read.return_value = b'{"hello": 123}'
        response.getheader.return_value = 'text/html'
        with mock.patch.object(probe.socket, 'create_connection', return_value=fake), mock.patch.object(probe.ssl, 'create_default_context') as ssl_context, mock.patch.object(probe.http.client, 'HTTPResponse', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            ssl_context.return_value.wrap_socket.return_value = fake
            result = probe.request('example.com', '104.21.44.19', '/api/health')
        self.assertEqual(result, (False, 'HTTP'))

    def test_verifier_uses_hostname_and_certificate_validation(self):
        fake = FakeSocket()
        response = mock.Mock(status=200)
        response.read.return_value = b'{}'
        response.getheader.return_value = 'application/json'
        with mock.patch.object(probe.socket, 'create_connection', return_value=fake), mock.patch.object(probe.ssl, 'create_default_context') as context, mock.patch.object(probe.http.client, 'HTTPResponse', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            context.return_value.wrap_socket.return_value = fake
            probe.request('worker.example', '104.21.44.19', '/api/health')
        context.assert_called_once_with()
        context.return_value.wrap_socket.assert_called_once_with(fake, server_hostname='worker.example')


if __name__ == '__main__':
    unittest.main()
