import unittest
from unittest.mock import Mock, patch

from network_safety import resolve_public_url, safe_http_get


class NetworkTests(unittest.TestCase):
    def test_rejects_private_ipv6_and_mapped_ipv4(self):
        for address in ("127.0.0.1", "10.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.169.254"):
            with patch("network_safety.socket.getaddrinfo", return_value=[(0, 0, 0, "", (address, 80))]):
                with self.assertRaises(RuntimeError):
                    resolve_public_url("http://example.com/")

    def test_connects_to_validated_ip_with_original_host(self):
        instances = []
        class Connection:
            def __init__(self, host, port, timeout):
                self.host, self.port = host, port
                instances.append(self)
            def request(self, *args, **kwargs):
                self._create_connection((self.host, self.port), 20)
            def getresponse(self):
                return Mock(status=200, read=Mock(return_value=b"hello"))
            def close(self):
                pass
        with patch("network_safety.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 80))]), patch("network_safety.socket.create_connection") as connect, patch("network_safety.http.client.HTTPSConnection", Connection):
            result = safe_http_get("https://example.com/a")
        self.assertEqual(result["content"], "hello")
        self.assertEqual(instances[0].host, "example.com")
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    def test_domain_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "approved"):
            resolve_public_url("https://example.com.attacker.com/", {"example.com"})
