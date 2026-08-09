'''Durable-boundary isolation for multimodal image payloads.'''

from __future__ import annotations

import copy
from datetime import datetime
import json

from agent import background_review
from agent.background_review import spawn_background_review_thread
from agent.context_compressor import ContextCompressor
from agent.agent_runtime_helpers import convert_to_trajectory_format
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.message_sanitization import (
    DURABLE_IMAGE_PLACEHOLDER,
    sanitize_durable_multimodal_payload,
)
from agent.tool_dispatch_helpers import _trajectory_normalize_msg
from hermes_state import SessionDB
from run_agent import AIAgent


_CACHE_PATH = '/tmp/hermes/cache/images/private-user/photo.png'
_SCREENSHOT_CACHE_PATH = (
    '/tmp/hermes/cache/screenshots/private-session/browser_screenshot_abc.png'
)
_BROWSER_SCREENSHOT_PATH = (
    r'C:\Users\private\.hermes\browser_screenshots\browser_screenshot_xyz.png'
)
_RELATIVE_SCREENSHOT_PATH = (
    'cache/screenshots/private-session/browser_screenshot_relative.png'
)
_RELATIVE_BROWSER_SCREENSHOT_PATH = './browser_screenshot_relative.png'
_OPAQUE_SCREENSHOT_URL = 'https://private.example.test/blob/opaque-screenshot-id'
_REMOTE_URL = 'https://private.example.test/photo.png?signed=temporary'
_DATA_URL = 'data:image/png;base64,RAW_IMAGE_BYTES'
_DURABLE_REFERENCE_PLACEHOLDER = (
    '[Image reference omitted from durable context]'
)


def _vision_messages():
    return [
        {
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': (
                        'Please describe the receipt. Keep /workspace/project/readme.md.\n'
                        f'Temporary cache: {_CACHE_PATH}\n'
                        f'Screenshot cache: {_SCREENSHOT_CACHE_PATH}\n'
                        f'Browser screenshot: {_BROWSER_SCREENSHOT_PATH}\n'
                        f'Relative screenshot: {_RELATIVE_SCREENSHOT_PATH}\n'
                        f'Browser relative: {_RELATIVE_BROWSER_SCREENSHOT_PATH}\n'
                        f'Opaque screenshot: {_OPAQUE_SCREENSHOT_URL}\n'
                        f'Remote image: {_REMOTE_URL}\n\n'
                        f'[Image attached at: {_CACHE_PATH}]'
                    ),
                },
                {
                    'type': 'image_url',
                    'image_url': {'url': _DATA_URL},
                },
            ],
        },
        {
            'role': 'assistant',
            'content': 'I can inspect it.',
            'tool_calls': [
                {
                    'id': 'call-vision',
                    'type': 'function',
                    'function': {
                        'name': 'vision_analyze',
                        'arguments': json.dumps(
                            {
                                'image_url': _CACHE_PATH,
                                'screenshot_path': _SCREENSHOT_CACHE_PATH,
                                'screenshot_url': _OPAQUE_SCREENSHOT_URL,
                                'question': 'zoom in',
                            }
                        ),
                    },
                },
            ],
        },
        {
            'role': 'tool',
            'tool_call_id': 'call-vision',
            'content': json.dumps(
                {
                    'success': True,
                    'browser_screenshot': _OPAQUE_SCREENSHOT_URL,
                    'screenshots': [_SCREENSHOT_CACHE_PATH],
                    'screenshot_path': _BROWSER_SCREENSHOT_PATH,
                    'screenshot_url': _OPAQUE_SCREENSHOT_URL,
                    'image_url': _REMOTE_URL,
                    'analysis': 'Total is visible.',
                }
            ),
        },
        {
            'role': 'assistant',
            'content': [
                {'type': 'text', 'text': 'Annotated preview'},
                {
                    'type': 'image_url',
                    'image_url': {'url': _REMOTE_URL},
                },
            ],
        },
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Second view'},
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': 'image/jpeg',
                        'data': 'RAW_ANTHROPIC_BYTES',
                    },
                },
            ],
        },
    ]


def _assert_no_image_leak(payload) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, default=str)
    assert _CACHE_PATH not in rendered
    assert _SCREENSHOT_CACHE_PATH not in rendered
    assert _BROWSER_SCREENSHOT_PATH not in rendered
    assert _RELATIVE_SCREENSHOT_PATH not in rendered
    assert _RELATIVE_BROWSER_SCREENSHOT_PATH not in rendered
    assert _OPAQUE_SCREENSHOT_URL not in rendered
    assert _REMOTE_URL not in rendered
    assert _DATA_URL not in rendered
    assert 'RAW_IMAGE_BYTES' not in rendered
    assert 'RAW_ANTHROPIC_BYTES' not in rendered
    assert (
        DURABLE_IMAGE_PLACEHOLDER in rendered
        or _DURABLE_REFERENCE_PLACEHOLDER in rendered
    )


def test_sanitizer_copies_payload_without_changing_foreground_request():
    messages = _vision_messages()

    durable = sanitize_durable_multimodal_payload(messages)

    _assert_no_image_leak(durable)
    assert '/workspace/project/readme.md' in json.dumps(durable)
    assert 'Please describe the receipt.' in json.dumps(durable)
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL
    assert _CACHE_PATH in messages[0]['content'][0]['text']
    assert durable is not messages
    assert durable[0] is not messages[0]
    assert sanitize_durable_multimodal_payload(b'raw-image') == (
        '[Binary payload omitted from durable context]'
    )


def test_optional_json_snapshot_omits_images_but_live_messages_still_have_them(
    tmp_path,
):
    agent = object.__new__(AIAgent)
    agent._session_json_enabled = True
    agent._session_messages = []
    agent.logs_dir = tmp_path
    agent.session_id = 'vision-cleanup'
    agent.model = 'test-model'
    agent.base_url = 'https://provider.example.test'
    agent.platform = 'test'
    agent.session_start = datetime(2026, 1, 1)
    agent._cached_system_prompt = ''
    agent.tools = []
    agent.verbose_logging = False
    messages = _vision_messages()

    agent._save_session_log(messages)

    saved = json.loads(
        (tmp_path / 'session_vision-cleanup.json').read_text(encoding='utf-8')
    )
    _assert_no_image_leak(saved['messages'])
    assert 'Please describe the receipt.' in json.dumps(saved['messages'])
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL


def test_sqlite_session_rows_sanitize_every_message_field(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('vision-durable', source='cli')
    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent._last_flushed_db_idx = 0
    agent.session_id = 'vision-durable'
    messages = _vision_messages()
    messages[1]['reasoning_details'] = {
        'image_path': _CACHE_PATH,
        'screenshot_paths': [_SCREENSHOT_CACHE_PATH],
        'screenshot_urls': [_OPAQUE_SCREENSHOT_URL],
        'preview': _DATA_URL,
    }
    messages[1]['codex_message_items'] = [
        {
            'type': 'input_image',
            'image_url': _REMOTE_URL,
            'browser_screenshot': _BROWSER_SCREENSHOT_PATH,
        }
    ]

    try:
        agent._flush_messages_to_session_db(messages)
        rows = db.get_messages(agent.session_id)
    finally:
        db.close()

    _assert_no_image_leak(rows)
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL
    assert _CACHE_PATH in messages[1]['tool_calls'][0]['function']['arguments']


def test_context_compression_never_forwards_or_persists_image_references():
    compressor = object.__new__(ContextCompressor)
    compressor._previous_summary = None
    messages = _vision_messages()

    serialized = compressor._serialize_for_summary(messages)
    fallback = compressor._build_static_fallback_summary(
        messages,
        reason='synthetic test failure',
    )

    _assert_no_image_leak(serialized)
    _assert_no_image_leak(fallback)
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL


def test_trajectory_artifact_sanitizes_full_message_without_mutating_live_turn():
    messages = _vision_messages()
    trajectory_message = {
        **messages[1],
        'content': messages[0]['content'],
        'reasoning_details': {'image_path': _CACHE_PATH},
    }

    durable = _trajectory_normalize_msg(trajectory_message)

    _assert_no_image_leak(durable)
    assert trajectory_message['content'][1]['image_url']['url'] == _DATA_URL
    assert _CACHE_PATH in trajectory_message['tool_calls'][0]['function']['arguments']


def test_full_trajectory_converter_sanitizes_user_query_and_all_tool_shapes():
    class _TrajectoryAgent:
        @staticmethod
        def _format_tools_for_system_message():
            return '[]'

    messages = [
        {'role': 'user', 'content': 'inspect'},
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [
                {
                    'id': 'call-shot',
                    'type': 'function',
                    'function': {
                        'name': 'browser_screenshot',
                        'arguments': json.dumps(
                            {
                                'screenshot_path': _SCREENSHOT_CACHE_PATH,
                                'screenshot_url': _OPAQUE_SCREENSHOT_URL,
                            }
                        ),
                    },
                }
            ],
        },
        {
            'role': 'tool',
            'tool_call_id': 'call-shot',
            'content': json.dumps(
                {
                    'browser_screenshots': [_OPAQUE_SCREENSHOT_URL],
                    'screenshots': [_SCREENSHOT_CACHE_PATH],
                    'screenshot_paths': [_BROWSER_SCREENSHOT_PATH],
                    'screenshot_urls': [_OPAQUE_SCREENSHOT_URL],
                }
            ),
        },
        {'role': 'assistant', 'content': 'done'},
    ]
    user_query = json.dumps(
        {
            'screenshot_path': _SCREENSHOT_CACHE_PATH,
            'screenshot_url': _OPAQUE_SCREENSHOT_URL,
        }
    )
    live_messages = copy.deepcopy(messages)
    live_user_query = user_query

    trajectory = convert_to_trajectory_format(
        _TrajectoryAgent(),
        messages,
        user_query,
        completed=True,
    )

    _assert_no_image_leak(trajectory)
    assert _SCREENSHOT_CACHE_PATH in user_query
    assert _OPAQUE_SCREENSHOT_URL in user_query
    assert messages == live_messages
    assert user_query == live_user_query


def test_background_review_receives_only_sanitized_conversation(monkeypatch):
    messages = _vision_messages()
    captured = {}

    def _capture(agent, messages_snapshot, prompt):
        captured['messages'] = messages_snapshot
        captured['prompt'] = prompt

    monkeypatch.setattr(background_review, '_run_review_in_thread', _capture)
    target, prompt = spawn_background_review_thread(
        object(),
        messages,
        review_skills=True,
    )

    target()

    assert captured['prompt'] == prompt
    _assert_no_image_leak(captured['messages'])
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL


class _RecordingMemoryProvider(MemoryProvider):
    @property
    def name(self):
        return 'builtin'

    def __init__(self):
        self.synced = []
        self.queued = []
        self.ended = []

    def initialize(self, session_id='', **kwargs):
        return None

    def is_available(self):
        return True

    def system_prompt_block(self):
        return ''

    def prefetch(self, query, *, session_id=''):
        return ''

    def queue_prefetch(self, query, *, session_id=''):
        self.queued.append(query)

    def sync_turn(
        self,
        user_content,
        assistant_content,
        *,
        session_id='',
        messages=None,
    ):
        self.synced.append(
            {
                'user': user_content,
                'assistant': assistant_content,
                'messages': messages,
            }
        )

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return ''

    def on_session_end(self, messages):
        self.ended.append(messages)


def test_memory_sync_and_session_end_never_receive_image_payloads():
    manager = MemoryManager()
    provider = _RecordingMemoryProvider()
    manager.add_provider(provider)
    messages = _vision_messages()
    user_content = (
        'Please describe the receipt.\n\n'
        f'[Image attached at: {_CACHE_PATH}]'
    )

    manager.sync_all(
        user_content,
        'The total is visible.',
        session_id='vision-cleanup',
        messages=messages,
    )
    manager.queue_prefetch_all(user_content, session_id='vision-cleanup')
    assert manager.flush_pending(timeout=5)
    manager.on_session_end(messages)

    _assert_no_image_leak(provider.synced[0]['messages'])
    assert _CACHE_PATH not in provider.synced[0]['user']
    assert 'Please describe the receipt.' in provider.synced[0]['user']
    assert _CACHE_PATH not in provider.queued[0]
    _assert_no_image_leak(provider.ended[0])
    assert messages[0]['content'][1]['image_url']['url'] == _DATA_URL
    manager.shutdown_all()
