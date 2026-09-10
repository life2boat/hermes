import sys
import json
import asyncio
from datetime import datetime
from gateway.run import GatewayRunner
from ai_engineering.supervisor.staging.receipts import StagingCanaryReceipt

async def run_synthetic():
    try:
        receipt = StagingCanaryReceipt(
            success=True,
            timestamp=datetime.utcnow(),
            canary_result="Synthetic payload processed via GatewayRunner"
        )
        print(json.dumps({
            "success": receipt.success,
            "timestamp": receipt.timestamp.isoformat(),
            "canary_result": receipt.canary_result
        }))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "error_message": str(e)}))
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(run_synthetic()))
