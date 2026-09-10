import sys
import json
import asyncio
from datetime import datetime
from gateway.run import GatewayRunner
from gateway.config import GatewayConfig, PlatformConfig
from gateway.events import MessageEvent, MessageSource, Platform
from unittest.mock import AsyncMock

async def run_synthetic():
    try:
        config = GatewayConfig()
        runner = GatewayRunner(config)
        
        from gateway.platforms.telegram import TelegramAdapter
        t_config = PlatformConfig(enabled=True, token='dummy')
        adapter = TelegramAdapter(t_config)
        adapter.send = AsyncMock(return_value=True)
        runner.adapters[Platform.TELEGRAM] = adapter
        
        source = MessageSource(
            platform=Platform.TELEGRAM,
            chat_id='synthetic-123',
            user_id='user-123',
            username='synthetic_user',
            message_id='msg-1',
            chat_type='private'
        )
        event = MessageEvent(
            source=source,
            text='/menu',
            date=datetime.utcnow()
        )
        
        await runner.handle_message(event)
        
        if not adapter.send.called:
            raise ValueError('Adapter send was not called. Handler failed.')
            
        print(json.dumps({
            'success': True,
            'timestamp': datetime.utcnow().isoformat(),
            'canary_result': 'Synthetic payload processed via GatewayRunner'
        }))
        return 0
    except Exception as e:
        print(json.dumps({'success': False, 'error_message': str(e)}))
        return 1

if __name__ == '__main__':
    sys.exit(asyncio.run(run_synthetic()))
