"""Computer use integration package for autonomous supervisor."""

from ai_engineering.supervisor.computer_use.antigravity import (
    ComputerUseAntigravityAdapter,
)
from ai_engineering.supervisor.computer_use.contracts import (
    ComputerUseActionType,
    ComputerUseTask,
    ComputerUseTaskError,
    UIActionProposal,
    UIActionReceipt,
    UIActionStatus,
    UIScenario,
    VisualEvidence,
    VisualEvidenceError,
    validate_computer_use_task,
    validate_visual_evidence,
)
from ai_engineering.supervisor.computer_use.driver import (
    ComputerUseDriver,
    FakeComputerUseDriver,
)
from ai_engineering.supervisor.computer_use.scenarios import (
    HermesGUITestRunner,
)

__all__ = [
    "ComputerUseActionType",
    "ComputerUseTask",
    "ComputerUseTaskError",
    "UIActionProposal",
    "UIActionReceipt",
    "UIActionStatus",
    "UIScenario",
    "VisualEvidence",
    "VisualEvidenceError",
    "validate_computer_use_task",
    "validate_visual_evidence",
    "ComputerUseDriver",
    "FakeComputerUseDriver",
    "ComputerUseAntigravityAdapter",
    "HermesGUITestRunner",
]
