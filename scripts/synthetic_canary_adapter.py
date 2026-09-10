import sys
import json
import asyncio
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from gateway.run import GatewayRunner
from gateway.config import GatewayConfig, PlatformConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

async def run_synthetic():
    try:
        config = GatewayConfig()
        runner = GatewayRunner(config)
        
        # Add a dummy webhook adapter so it passes early checks
        runner.adapters[Platform.WEBHOOK] = "dummy_adapter"
        
        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id='synthetic-123',
            user_id='user-123',
            user_name='synthetic_user',
            chat_type='private'
        )
        
        event = MessageEvent(text='/status')
        event.source = source
        event.message_id = 'msg-1'
        
        res = await runner._handle_message(event)
        
        if not res or "Hermes Gateway Status" not in res:
            raise ValueError(f'Handler failed or returned unexpected response: {res}')
            
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
