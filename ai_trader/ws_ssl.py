"""
WebSocket TLS helpers.

`websocket-client` 默认依赖 Python/OpenSSL 的系统 CA 路径。
在部分 macOS Python 安装中，这个默认路径可能不存在，导致 HTTPS 正常
（requests 使用 certifi），但 WSS 证书校验失败。
"""
import os
import ssl


def get_websocket_sslopt():
    """Return sslopt for websocket-client using an explicit CA bundle."""
    ca_file = _resolve_ca_file()
    sslopt = {"cert_reqs": ssl.CERT_REQUIRED}
    if ca_file:
        sslopt["ca_certs"] = ca_file
    return sslopt


def _resolve_ca_file():
    # Respect explicit user overrides first so corporate/self-hosted CAs still work.
    for env_name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.getenv(env_name)
        if path and os.path.isfile(path):
            return path

    try:
        import certifi
        return certifi.where()
    except Exception:
        return None
