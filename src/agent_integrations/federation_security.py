from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from agent_config.app import (
    FederationMtlsConfig,
    FederationPushSecurityConfig,
    FederationSecuritySchemeConfig,
)


def encode_page_token(kind: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({'kind': kind, 'payload': payload}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_page_token(token: str, expected_kind: str) -> dict[str, Any]:
    padding = '=' * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(f'{token}{padding}'.encode('ascii')).decode('utf-8')
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError('invalid federation page token') from exc
    if payload.get('kind') != expected_kind or not isinstance(payload.get('payload'), dict):
        raise ValueError('invalid federation page token kind')
    return dict(payload['payload'])


def build_security_scheme_payload(config: FederationSecuritySchemeConfig) -> dict[str, Any]:
    payload: dict[str, Any]
    if config.type == 'none':
        payload = {'type': 'noAuth'}
    elif config.type == 'bearer':
        payload = {'type': 'http', 'scheme': 'bearer'}
        if config.bearer_format:
            payload['bearerFormat'] = config.bearer_format
    elif config.type == 'header':
        payload = {'type': 'apiKey', 'in': 'header', 'name': config.header_name}
    elif config.type == 'oauth2':
        payload = {
            'type': 'oauth2',
            'flows': {
                'clientCredentials': {
                    'tokenUrl': config.token_url,
                    'scopes': config.scopes,
                }
            },
        }
        if config.authorization_url:
            payload['flows']['authorizationCode'] = {
                'authorizationUrl': config.authorization_url,
                'tokenUrl': config.token_url,
                'scopes': config.scopes,
            }
    elif config.type == 'oidc':
        payload = {'type': 'openIdConnect', 'openIdConnectUrl': config.openid_config_url}
    else:
        payload = {'type': 'mutualTLS'}
    if config.description:
        payload['description'] = config.description
    if config.audience:
        payload['x-audience'] = config.audience
    return payload


def build_auth_hint_payload(config: FederationSecuritySchemeConfig) -> dict[str, Any]:
    payload = {'name': config.name, 'type': config.type}
    if config.description:
        payload['description'] = config.description
    if config.type in {'bearer', 'header'}:
        payload['header_name'] = config.header_name
    if config.type in {'oauth2', 'oidc'} and config.audience:
        payload['audience'] = config.audience
    return payload


def build_mtls_client_kwargs(config: FederationMtlsConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if config.insecure_skip_verify:
        kwargs['verify'] = False
    elif config.ca_cert:
        kwargs['verify'] = config.ca_cert
    if config.client_cert and config.client_key:
        kwargs['cert'] = (config.client_cert, config.client_key)
    return kwargs


def _is_private_host(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    if lowered in {'localhost', '127.0.0.1', '::1'}:
        return True
    try:
        parsed = ipaddress.ip_address(lowered)
    except ValueError:
        return lowered.endswith('.local')
    return parsed.is_loopback or parsed.is_private or parsed.is_link_local or parsed.is_reserved


def validate_callback_url(callback_url: str, config: FederationPushSecurityConfig) -> None:
    parsed = urlparse(callback_url)
    if parsed.scheme not in {'http', 'https'}:
        raise RuntimeError('callback_url must use http or https')
    if not parsed.hostname:
        raise RuntimeError('callback_url must include a hostname')
    host = parsed.hostname.lower()
    if config.callback_url_policy == 'allowlist':
        allowed = {item.strip().lower() for item in config.callback_allowlist_hosts if item.strip()}
        if host not in allowed:
            raise RuntimeError(f'callback_url host is not in the allowlist: {host}')
        return
    if config.callback_url_policy == 'public_only' and _is_private_host(host):
        raise RuntimeError(f'callback_url host is not public: {host}')


def build_callback_headers(callback_url: str, payload_bytes: bytes, config: FederationPushSecurityConfig) -> dict[str, str]:
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    token = os.environ.get(config.token_env or '', '').strip() if config.token_env else ''
    if token:
        headers[config.token_header] = token
    timestamp = str(int(time.time()))
    if config.require_signature or config.signature_secret_env:
        secret = os.environ.get(config.signature_secret_env or '', '').strip() if config.signature_secret_env else ''
        if not secret:
            raise RuntimeError('push signature secret is not available')
        callback_path = urlparse(callback_url).path or '/'
        signed = timestamp.encode('utf-8') + b'\n' + callback_path.encode('utf-8') + b'\n' + payload_bytes
        digest = hmac.new(secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
        headers[config.signature_header] = f'{config.signature_algorithm}={digest}'
        headers[config.timestamp_header] = timestamp
    if config.audience:
        headers[config.audience_header] = config.audience
    elif config.require_audience:
        raise RuntimeError('push audience is required but not configured')
    return headers


def verify_callback_headers(
    headers: Mapping[str, str],
    payload_bytes: bytes,
    callback_path: str,
    config: FederationPushSecurityConfig,
    *,
    expected_secret: str,
    expected_audience: str | None = None,
    now: int | None = None,
) -> None:
    normalized_headers = {str(key).lower(): value for key, value in headers.items()}

    def _header(name: str) -> str | None:
        return normalized_headers.get(name.lower())

    if config.require_audience or expected_audience:
        audience = _header(config.audience_header)
        if not audience or audience != (expected_audience or config.audience):
            raise RuntimeError('callback audience mismatch')
    if config.require_signature or config.signature_secret_env:
        header_value = _header(config.signature_header)
        if not header_value or '=' not in header_value:
            raise RuntimeError('callback signature header is missing')
        algorithm, digest = header_value.split('=', 1)
        if algorithm != config.signature_algorithm:
            raise RuntimeError('callback signature algorithm mismatch')
        timestamp = _header(config.timestamp_header)
        if not timestamp:
            raise RuntimeError('callback timestamp header is missing')
        current = int(now or time.time())
        if abs(current - int(timestamp)) > int(config.timestamp_tolerance_seconds):
            raise RuntimeError('callback timestamp is outside the tolerance window')
        signed = timestamp.encode('utf-8') + b'\n' + callback_path.encode('utf-8') + b'\n' + payload_bytes
        expected_digest = hmac.new(expected_secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected_digest):
            raise RuntimeError('callback signature mismatch')
