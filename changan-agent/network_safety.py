"""外部网页读取：使用已检查的地址连接，避免校验后重新解析 DNS。"""

from __future__ import annotations

import http.client
import ipaddress
import socket
from urllib.parse import urljoin, urlparse


def resolve_public_url(url, allowed_domains=None):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("只允许不含账号密码的 http/https 网页地址")
    host = parsed.hostname.lower().rstrip(".")
    if allowed_domains and not any(host == d or host.endswith("." + d) for d in allowed_domains):
        raise RuntimeError(f"domain is not approved: {host}")
    try:
        values = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as exc:
        raise RuntimeError("网页域名无法解析") from exc
    addresses = list(dict.fromkeys(item[4][0] for item in values))
    if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast for address in addresses):
        raise RuntimeError("private or local network URL is blocked")
    return parsed, addresses


def safe_http_get(url, allowed_domains=None, max_redirects=5, max_bytes=50000):
    for redirect in range(max_redirects + 1):
        parsed, addresses = resolve_public_url(url, allowed_domains)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = cls(parsed.hostname, port, timeout=20)
        # HTTP Host 与 TLS SNI 仍使用原域名，仅底层连接固定为已验证的公网 IP。
        connection._create_connection = lambda address, timeout, source_address=None, ips=addresses, port=port: socket.create_connection((ips[0], port), timeout, source_address)
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={"User-Agent": "Changan/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == max_redirects:
                    raise RuntimeError("网页重定向超过限制或缺少目标地址")
                url = urljoin(url, location)
                continue
            if response.status >= 400:
                raise RuntimeError(f"网页返回 HTTP {response.status}")
            data = response.read(max_bytes + 1)
            return {"ok": True, "status": response.status, "url": url, "content": data[:max_bytes].decode("utf-8", errors="replace"), "truncated": len(data) > max_bytes}
        finally:
            connection.close()
    raise RuntimeError("网页重定向超过限制")
