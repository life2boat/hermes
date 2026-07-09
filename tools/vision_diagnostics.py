from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import httpx

from agent.auxiliary_client import LLMServiceUnavailableError
from agent.gemini_native_adapter import GeminiAPIError, GeminiCredentialError

_ALLOWED_GEMINI_CATEGORIES = {
    'GEMINI_AUTH_FAILURE',
    'GEMINI_ACCESS_DENIED',
    'GEMINI_MODEL_NOT_AVAILABLE',
    'GEMINI_ENDPOINT_MISMATCH',
    'GEMINI_REQUEST_SCHEMA_REJECTED',
    'GEMINI_IMAGE_PAYLOAD_REJECTED',
    'GEMINI_RATE_LIMIT',
    'GEMINI_TIMEOUT',
    'GEMINI_TRANSPORT_ERROR',
    'GEMINI_PROVIDER_4XX',
    'GEMINI_PROVIDER_5XX',
    'GEMINI_RESPONSE_DECODE_FAILURE',
    'GEMINI_CONTENT_EXTRACTION_FAILURE',
    'GEMINI_JSON_PARSE_FAILURE',
    'GEMINI_ADAPTER_EXCEPTION',
    'GEMINI_UNKNOWN_OPERATIONAL_FAILURE',
}

_ALLOWED_STAGES = {
    'CONFIG_RESOLUTION',
    'REQUEST_CONSTRUCTION',
    'AUTH_PREPARATION',
    'TRANSPORT',
    'PROVIDER_HTTP',
    'PROVIDER_RESPONSE_DECODE',
    'PROVIDER_CONTENT_EXTRACTION',
    'JSON_EXTRACTION',
    'INVENTORY_VALIDATION',
    'UNKNOWN',
}

_SAFE_REASONS = {
    'missing_api_key',
    'api_key_resolution_failed',
    'request_construction_failed',
    'http_401',
    'http_403',
    'http_404',
    'http_413',
    'http_429',
    'http_4xx',
    'http_5xx',
    'request_schema_invalid',
    'image_payload_rejected',
    'model_not_found',
    'timeout',
    'transport_connect_error',
    'transport_dns_error',
    'transport_tls_error',
    'transport_network_error',
    'transport_http_error',
    'response_decode_failed',
    'missing_content_parts',
    'missing_text_content',
    'inventory_validation_failed',
    'unexpected_adapter_exception',
    'unknown_exception',
}

_SAFE_PROVIDER_CODES = {
    'UNAUTHENTICATED',
    'PERMISSION_DENIED',
    'NOT_FOUND',
    'RESOURCE_EXHAUSTED',
    'INVALID_ARGUMENT',
    'MODEL_NOT_FOUND',
    'REQUEST_SCHEMA_INVALID',
    'IMAGE_PAYLOAD_REJECTED',
    'PAYLOAD_TOO_LARGE',
    'BAD_GATEWAY',
    'INTERNAL',
    'UNKNOWN',
}

_REQUEST_SCHEMA_CODES = {'REQUEST_SCHEMA_INVALID'}
_IMAGE_PAYLOAD_CODES = {'IMAGE_PAYLOAD_REJECTED', 'PAYLOAD_TOO_LARGE'}


@dataclass(frozen=True)
class VisionFailureDiagnostic:
    provider: str
    model: str
    stage: str
    category: str
    http_status_class: str
    retryable: bool
    request_was_attempted: bool
    provider_response_received: bool
    response_parse_attempted: bool
    validator_reached: bool
    sanitized_provider_code: Optional[str]
    safe_reason: Optional[str]
    original_exception_type: str
    cause_chain_types: List[str]
    raw_error_exposed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize_stage(stage: Any) -> str:
    value = str(stage or 'UNKNOWN').strip().upper()
    return value if value in _ALLOWED_STAGES else 'UNKNOWN'


def _normalize_category(category: Any) -> str:
    value = str(category or 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE').strip().upper()
    if value not in _ALLOWED_GEMINI_CATEGORIES:
        return 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE'
    return value


def _normalize_safe_reason(reason: Any) -> Optional[str]:
    value = str(reason or '').strip().lower()
    return value if value in _SAFE_REASONS else None


def _normalize_provider_code(*values: Any) -> Optional[str]:
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip().upper().replace('-', '_').replace(' ', '_')
        if candidate in _SAFE_PROVIDER_CODES:
            return candidate
    return None


def _http_status_class(status_code: Optional[int]) -> str:
    if not isinstance(status_code, int):
        return 'none'
    if 100 <= status_code < 200:
        return '1xx'
    if 200 <= status_code < 300:
        return '2xx'
    if 300 <= status_code < 400:
        return '3xx'
    if 400 <= status_code < 500:
        return '4xx'
    if 500 <= status_code < 600:
        return '5xx'
    return 'other'


def _exception_chain(error: BaseException, *, max_depth: int = 8) -> List[BaseException]:
    chain: List[BaseException] = []
    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and len(chain) < max_depth:
        ident = id(current)
        if ident in seen:
            break
        seen.add(ident)
        chain.append(current)
        next_exc = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _chain_type_names(chain: List[BaseException]) -> List[str]:
    return [type(exc).__name__ for exc in chain]


def _transport_safe_reason(chain: List[BaseException]) -> str:
    for exc in chain:
        if isinstance(exc, httpx.TimeoutException):
            return 'timeout'
    names = {type(exc).__name__ for exc in chain}
    if 'gaierror' in names:
        return 'transport_dns_error'
    if {'SSLError', 'SSLCertVerificationError'} & names:
        return 'transport_tls_error'
    if any(isinstance(exc, httpx.ConnectError) for exc in chain):
        return 'transport_connect_error'
    if any(isinstance(exc, httpx.NetworkError) for exc in chain):
        return 'transport_network_error'
    if any(isinstance(exc, httpx.HTTPError) for exc in chain):
        return 'transport_http_error'
    return 'transport_network_error'


def _gemini_http_category(status_code: Optional[int], provider_code: Optional[str], safe_reason: Optional[str]) -> str:
    if status_code == 401:
        return 'GEMINI_AUTH_FAILURE'
    if status_code == 403:
        return 'GEMINI_ACCESS_DENIED'
    if status_code == 404 or safe_reason == 'model_not_found' or provider_code == 'MODEL_NOT_FOUND':
        return 'GEMINI_MODEL_NOT_AVAILABLE'
    if safe_reason == 'request_schema_invalid' or provider_code in _REQUEST_SCHEMA_CODES:
        return 'GEMINI_REQUEST_SCHEMA_REJECTED'
    if status_code == 413 or safe_reason == 'image_payload_rejected' or provider_code in _IMAGE_PAYLOAD_CODES:
        return 'GEMINI_IMAGE_PAYLOAD_REJECTED'
    if status_code == 429:
        return 'GEMINI_RATE_LIMIT'
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return 'GEMINI_PROVIDER_5XX'
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return 'GEMINI_PROVIDER_4XX'
    return 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE'


def classify_gemini_failure(error: BaseException, *, model: str = 'auto') -> VisionFailureDiagnostic:
    if isinstance(error, VisionFailureDiagnostic):
        return VisionFailureDiagnostic(
            provider='gemini',
            model=model,
            stage=_normalize_stage(error.stage),
            category=_normalize_category(error.category),
            http_status_class=error.http_status_class,
            retryable=bool(error.retryable),
            request_was_attempted=bool(error.request_was_attempted),
            provider_response_received=bool(error.provider_response_received),
            response_parse_attempted=bool(error.response_parse_attempted),
            validator_reached=bool(error.validator_reached),
            sanitized_provider_code=_normalize_provider_code(error.sanitized_provider_code),
            safe_reason=_normalize_safe_reason(error.safe_reason),
            original_exception_type=error.original_exception_type,
            cause_chain_types=list(error.cause_chain_types),
            raw_error_exposed=False,
        )

    chain = _exception_chain(error)
    cause_chain_types = _chain_type_names(chain)
    original_exception_type = type(error).__name__
    gemini_error = next((exc for exc in chain if isinstance(exc, GeminiAPIError)), None)
    credential_error = next((exc for exc in chain if isinstance(exc, GeminiCredentialError)), None)

    if credential_error is not None:
        message = str(credential_error).lower()
        if 'empty api key' in message or 'none was provided' in message:
            stage = 'CONFIG_RESOLUTION'
            safe_reason = 'missing_api_key'
        else:
            stage = 'AUTH_PREPARATION'
            safe_reason = 'api_key_resolution_failed'
        return VisionFailureDiagnostic(
            provider='gemini',
            model=model,
            stage=stage,
            category='GEMINI_AUTH_FAILURE',
            http_status_class='none',
            retryable=False,
            request_was_attempted=False,
            provider_response_received=False,
            response_parse_attempted=False,
            validator_reached=False,
            sanitized_provider_code=None,
            safe_reason=safe_reason,
            original_exception_type=original_exception_type,
            cause_chain_types=cause_chain_types,
        )

    if gemini_error is not None:
        stage = _normalize_stage(getattr(gemini_error, 'stage', 'UNKNOWN'))
        status_code = getattr(gemini_error, 'status_code', None)
        provider_code = _normalize_provider_code(
            getattr(gemini_error, 'sanitized_provider_code', None),
            getattr(gemini_error, 'details', {}).get('status') if isinstance(getattr(gemini_error, 'details', None), dict) else None,
            getattr(gemini_error, 'details', {}).get('reason') if isinstance(getattr(gemini_error, 'details', None), dict) else None,
            getattr(gemini_error, 'code', None),
        )
        safe_reason = _normalize_safe_reason(getattr(gemini_error, 'safe_reason', None))
        if stage == 'PROVIDER_HTTP':
            category = _gemini_http_category(status_code, provider_code, safe_reason)
        elif stage == 'TRANSPORT':
            category = 'GEMINI_TIMEOUT' if safe_reason == 'timeout' else 'GEMINI_TRANSPORT_ERROR'
        elif stage == 'PROVIDER_RESPONSE_DECODE':
            category = 'GEMINI_RESPONSE_DECODE_FAILURE'
        elif stage == 'PROVIDER_CONTENT_EXTRACTION':
            category = 'GEMINI_CONTENT_EXTRACTION_FAILURE'
        elif stage == 'JSON_EXTRACTION':
            category = 'GEMINI_JSON_PARSE_FAILURE'
        elif stage == 'INVENTORY_VALIDATION':
            category = 'GEMINI_JSON_PARSE_FAILURE'
        elif stage in {'REQUEST_CONSTRUCTION', 'AUTH_PREPARATION', 'CONFIG_RESOLUTION'}:
            category = 'GEMINI_ADAPTER_EXCEPTION'
        else:
            category = 'GEMINI_UNKNOWN_OPERATIONAL_FAILURE'
        return VisionFailureDiagnostic(
            provider='gemini',
            model=model,
            stage=stage,
            category=_normalize_category(category),
            http_status_class=_http_status_class(status_code),
            retryable=bool(getattr(gemini_error, 'retryable', False)),
            request_was_attempted=bool(getattr(gemini_error, 'request_was_attempted', False)),
            provider_response_received=bool(getattr(gemini_error, 'provider_response_received', False)),
            response_parse_attempted=bool(getattr(gemini_error, 'response_parse_attempted', False)),
            validator_reached=bool(getattr(gemini_error, 'validator_reached', False)),
            sanitized_provider_code=provider_code,
            safe_reason=safe_reason,
            original_exception_type=original_exception_type,
            cause_chain_types=cause_chain_types,
        )

    if any(isinstance(exc, LLMServiceUnavailableError) for exc in chain):
        return VisionFailureDiagnostic(
            provider='gemini',
            model=model,
            stage='TRANSPORT',
            category='GEMINI_PROVIDER_5XX',
            http_status_class='5xx',
            retryable=True,
            request_was_attempted=True,
            provider_response_received=False,
            response_parse_attempted=False,
            validator_reached=False,
            sanitized_provider_code=None,
            safe_reason='transport_network_error',
            original_exception_type=original_exception_type,
            cause_chain_types=cause_chain_types,
        )

    if any(isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError)) for exc in chain):
        safe_reason = _transport_safe_reason(chain)
        category = 'GEMINI_TIMEOUT' if safe_reason == 'timeout' else 'GEMINI_TRANSPORT_ERROR'
        return VisionFailureDiagnostic(
            provider='gemini',
            model=model,
            stage='TRANSPORT',
            category=category,
            http_status_class='none',
            retryable=True,
            request_was_attempted=True,
            provider_response_received=False,
            response_parse_attempted=False,
            validator_reached=False,
            sanitized_provider_code=None,
            safe_reason=safe_reason,
            original_exception_type=original_exception_type,
            cause_chain_types=cause_chain_types,
        )

    return VisionFailureDiagnostic(
        provider='gemini',
        model=model,
        stage='UNKNOWN',
        category='GEMINI_UNKNOWN_OPERATIONAL_FAILURE',
        http_status_class='none',
        retryable=False,
        request_was_attempted=False,
        provider_response_received=False,
        response_parse_attempted=False,
        validator_reached=False,
        sanitized_provider_code=None,
        safe_reason='unknown_exception',
        original_exception_type=original_exception_type,
        cause_chain_types=cause_chain_types,
    )


def build_gemini_content_extraction_diagnostic(*, model: str, safe_reason: str = 'missing_text_content') -> VisionFailureDiagnostic:
    return VisionFailureDiagnostic(
        provider='gemini',
        model=model,
        stage='PROVIDER_CONTENT_EXTRACTION',
        category='GEMINI_CONTENT_EXTRACTION_FAILURE',
        http_status_class='2xx',
        retryable=False,
        request_was_attempted=True,
        provider_response_received=True,
        response_parse_attempted=True,
        validator_reached=False,
        sanitized_provider_code=None,
        safe_reason=_normalize_safe_reason(safe_reason) or 'missing_text_content',
        original_exception_type='GeminiContentExtractionError',
        cause_chain_types=['GeminiContentExtractionError'],
    )


def build_gemini_inventory_validation_diagnostic(*, model: str) -> VisionFailureDiagnostic:
    return VisionFailureDiagnostic(
        provider='gemini',
        model=model,
        stage='INVENTORY_VALIDATION',
        category='GEMINI_JSON_PARSE_FAILURE',
        http_status_class='2xx',
        retryable=False,
        request_was_attempted=True,
        provider_response_received=True,
        response_parse_attempted=True,
        validator_reached=True,
        sanitized_provider_code=None,
        safe_reason='inventory_validation_failed',
        original_exception_type='GeminiInventoryValidationError',
        cause_chain_types=['GeminiInventoryValidationError'],
    )


def coarse_vision_error_kind(diagnostic: VisionFailureDiagnostic) -> str:
    if diagnostic.category in {
        'GEMINI_AUTH_FAILURE',
        'GEMINI_ACCESS_DENIED',
        'GEMINI_MODEL_NOT_AVAILABLE',
        'GEMINI_ENDPOINT_MISMATCH',
        'GEMINI_REQUEST_SCHEMA_REJECTED',
        'GEMINI_IMAGE_PAYLOAD_REJECTED',
        'GEMINI_PROVIDER_4XX',
    }:
        return 'request_rejected'
    return 'provider_unavailable'


def safe_vision_failure_analysis(diagnostic: VisionFailureDiagnostic) -> str:
    kind = coarse_vision_error_kind(diagnostic)
    if kind == 'request_rejected':
        return (
            'The provider rejected the image request. '
            'Try sending a smaller, clearer image, or describe the meal in text.'
        )
    return (
        'Vision service temporarily unavailable. '
        'Please try again later or describe the meal in text.'
    )
