from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime

@dataclass
class ComputerUseActivationReceipt:
    version: str = "hermes.computer-use-activation-receipt.v1"
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "UNKNOWN"
    target_window: Optional[str] = None
    windows_found: int = 0
    actions_performed: int = 0
    error_message: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
