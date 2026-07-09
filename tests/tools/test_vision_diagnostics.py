from __future__ import annotations

import json
import socket
import ssl

import httpx
import pytest

from agent.gemini_native_adapter import GeminiAPIError, GeminiCredentialError, gemini_http_error
from tools.vision_diagnostics import (
    build_gemini_content_extraction_diagnostic,
    build_gemini_inventory_validation_diagnostic,
    classify_gemini_failure,
)


class _DummyResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


def _http_error(status_code: int, *, status: str, reason: str | None = None, message: str = 'RAW_PROVIDER_MESSAGE_SECRETLIKE_BASE64'):
    details = []
    if reason is not None:
        details.append(
            {
                '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                'reason': reason,
                'metadata': {'service': 'generativelanguage.googleapis.com', 'resource': 'projects/example'},
            }
        )
    response = _DummyResponse(
        status_code=status_code,
        payload={
            'error': {
                'code': status_code,
                'message': message,
                'status': status,
                'details': details,
            }
        },
    )
    return gemini_http_error(response)


def _transport_error(kind: str):
    request = httpx.Request('POST', 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent')
    if kind == 'connect':
        return httpx.ConnectError('synthetic connect failure', request=request)
    if kind == 'dns':
        err = httpx.ConnectError('synthetic dns failure', request=request)
        err.__cause__ = socket.gaierror(8, 'synthetic name error')
        return err
    if kind == 'tls':
        err = httpx.ConnectError('synthetic tls failure', request=request)
        err.__cause__ = ssl.SSLError('synthetic tls error')
        return err
    if kind == 'timeout':
        return httpx.ReadTimeout('synthetic timeout', request=request)
    raise AssertionError(kind)


@pytest.mark.parametrize(
    ('error', 'stage', 'category', 'retryable', 'request_attempted', 'response_received', 'validator_reached', 'safe_reason'),
    [
        (GeminiCredentialError('Gemini native client requires an API key, but none was provided.'), 'CONFIG_RESOLUTION', 'GEMINI_AUTH_FAILURE', False, False, False, False, 'missing_api_key'),
        (GeminiCredentialError('Gemini native client could not resolve an API key at request time.'), 'AUTH_PREPARATION', 'GEMINI_AUTH_FAILURE', False, False, False, False, 'api_key_resolution_failed'),
        (_http_error(401, status='UNAUTHENTICATED'), 'PROVIDER_HTTP', 'GEMINI_AUTH_FAILURE', False, True, True, False, 'http_401'),
        (_http_error(403, status='PERMISSION_DENIED'), 'PROVIDER_HTTP', 'GEMINI_ACCESS_DENIED', False, True, True, False, 'http_403'),
        (_http_error(404, status='NOT_FOUND', reason='MODEL_NOT_FOUND'), 'PROVIDER_HTTP', 'GEMINI_MODEL_NOT_AVAILABLE', False, True, True, False, 'model_not_found'),
        (_http_error(400, status='INVALID_ARGUMENT', reason='REQUEST_SCHEMA_INVALID'), 'PROVIDER_HTTP', 'GEMINI_REQUEST_SCHEMA_REJECTED', False, True, True, False, 'request_schema_invalid'),
        (_http_error(413, status='INVALID_ARGUMENT', reason='IMAGE_PAYLOAD_REJECTED'), 'PROVIDER_HTTP', 'GEMINI_IMAGE_PAYLOAD_REJECTED', False, True, True, False, 'image_payload_rejected'),
        (_http_error(429, status='RESOURCE_EXHAUSTED', reason='RESOURCE_EXHAUSTED'), 'PROVIDER_HTTP', 'GEMINI_RATE_LIMIT', True, True, True, False, 'http_429'),
        (_http_error(500, status='INTERNAL'), 'PROVIDER_HTTP', 'GEMINI_PROVIDER_5XX', True, True, True, False, 'http_5xx'),
        (_transport_error('connect'), 'TRANSPORT', 'GEMINI_TRANSPORT_ERROR', True, True, False, False, 'transport_connect_error'),
        (_transport_error('dns'), 'TRANSPORT', 'GEMINI_TRANSPORT_ERROR', True, True, False, False, 'transport_dns_error'),
        (_transport_error('tls'), 'TRANSPORT', 'GEMINI_TRANSPORT_ERROR', True, True, False, False, 'transport_tls_error'),
        (_transport_error('timeout'), 'TRANSPORT', 'GEMINI_TIMEOUT', True, True, False, False, 'timeout'),
        (GeminiAPIError('Gemini response JSON decode failed', code='gemini_invalid_json', stage='PROVIDER_RESPONSE_DECODE', status_code=200, request_was_attempted=True, provider_response_received=True, response_parse_attempted=True, retryable=False, safe_reason='response_decode_failed'), 'PROVIDER_RESPONSE_DECODE', 'GEMINI_RESPONSE_DECODE_FAILURE', False, True, True, False, 'response_decode_failed'),
        (build_gemini_content_extraction_diagnostic(model='gemini-2.5-flash'), 'PROVIDER_CONTENT_EXTRACTION', 'GEMINI_CONTENT_EXTRACTION_FAILURE', False, True, True, False, 'missing_text_content'),
        (build_gemini_inventory_validation_diagnostic(model='gemini-2.5-flash'), 'INVENTORY_VALIDATION', 'GEMINI_JSON_PARSE_FAILURE', False, True, True, True, 'inventory_validation_failed'),
        (GeminiAPIError('Gemini request construction failed', code='gemini_request_construction_failed', stage='REQUEST_CONSTRUCTION', request_was_attempted=False, provider_response_received=False, response_parse_attempted=False, retryable=False, safe_reason='request_construction_failed'), 'REQUEST_CONSTRUCTION', 'GEMINI_ADAPTER_EXCEPTION', False, False, False, False, 'request_construction_failed'),
        (RuntimeError('synthetic unexpected RAW_PROVIDER_MESSAGE_SECRETLIKE_BASE64'), 'UNKNOWN', 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE', False, False, False, False, 'unknown_exception'),
    ],
)
def test_gemini_failure_classification_matrix(error, stage, category, retryable, request_attempted, response_received, validator_reached, safe_reason):
    diagnostic = classify_gemini_failure(error, model='gemini-2.5-flash')

    assert diagnostic.stage == stage
    assert diagnostic.category == category
    assert diagnostic.retryable is retryable
    assert diagnostic.request_was_attempted is request_attempted
    assert diagnostic.provider_response_received is response_received
    assert diagnostic.validator_reached is validator_reached
    assert diagnostic.safe_reason == safe_reason

    encoded = json.dumps(diagnostic.as_dict(), ensure_ascii=False)
    assert 'RAW_PROVIDER_MESSAGE_SECRETLIKE_BASE64' not in encoded
    assert 'AIza' not in encoded
    assert 'base64' not in encoded.lower()


def test_wrapped_http_error_preserves_typed_status_without_raw_message():
    raw = _http_error(403, status='PERMISSION_DENIED', message='REAL_PROVIDER_REASON_SECRETLIKE')
    wrapped = RuntimeError('wrapper')
    wrapped.__cause__ = raw

    diagnostic = classify_gemini_failure(wrapped, model='gemini-2.5-flash')

    assert diagnostic.original_exception_type == 'RuntimeError'
    assert diagnostic.category == 'GEMINI_ACCESS_DENIED'
    assert diagnostic.stage == 'PROVIDER_HTTP'
    assert diagnostic.cause_chain_types[:2] == ['RuntimeError', 'GeminiAPIError']
    assert 'REAL_PROVIDER_REASON_SECRETLIKE' not in json.dumps(diagnostic.as_dict(), ensure_ascii=False)


def test_multi_wrapped_http_error_preserves_provider_signal():
    raw = _http_error(429, status='RESOURCE_EXHAUSTED', reason='RESOURCE_EXHAUSTED')
    middle = ValueError('middle wrapper')
    middle.__cause__ = raw
    outer = RuntimeError('outer wrapper')
    outer.__cause__ = middle

    diagnostic = classify_gemini_failure(outer, model='gemini-2.5-flash')

    assert diagnostic.category == 'GEMINI_RATE_LIMIT'
    assert diagnostic.stage == 'PROVIDER_HTTP'
    assert diagnostic.cause_chain_types[:3] == ['RuntimeError', 'ValueError', 'GeminiAPIError']


def test_cyclic_cause_chain_is_bounded_and_safe():
    first = RuntimeError('first')
    second = RuntimeError('second')
    first.__cause__ = second
    second.__cause__ = first

    diagnostic = classify_gemini_failure(first, model='gemini-2.5-flash')

    assert diagnostic.category == 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE'
    assert len(diagnostic.cause_chain_types) <= 8
    assert diagnostic.cause_chain_types.count('RuntimeError') >= 1
