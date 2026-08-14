"""Typed contracts for deterministic Hermes prompt engineering."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


PROMPT_SPEC_SCHEMA_VERSION = 1
PROMPT_COMPILER_VERSION = 1
PROMPT_VALIDATOR_VERSION = 1
PROMPT_EVAL_ENGINE_VERSION = 1


class PromptComplexity(StrEnum):
    SIMPLE = "SIMPLE"
    COMPLEX = "COMPLEX"


class PromptFormat(StrEnum):
    AUTO = "AUTO"
    TAGGED = "TAGGED"
    MARKDOWN = "MARKDOWN"


class OutputMode(StrEnum):
    TEXT = "TEXT"
    JSON_SCHEMA = "JSON_SCHEMA"
    TYPED = "TYPED"
    TAGGED = "TAGGED"


class DiagnosticSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class PromptFailureClass(StrEnum):
    CONTEXT_MISSING = "CONTEXT_MISSING"
    INSTRUCTION_MISSING = "INSTRUCTION_MISSING"
    ORDERING_ERROR = "ORDERING_ERROR"
    AMBIGUOUS_CONSTRAINT = "AMBIGUOUS_CONSTRAINT"
    MISSING_EXAMPLE = "MISSING_EXAMPLE"
    OUTPUT_CONTRACT_ERROR = "OUTPUT_CONTRACT_ERROR"
    TOOL_POLICY_ERROR = "TOOL_POLICY_ERROR"
    MODEL_CAPABILITY_LIMIT = "MODEL_CAPABILITY_LIMIT"


class PromptContractError(ValueError):
    """Fail-closed prompt error exposing only stable diagnostics."""

    def __init__(
        self, code: str, diagnostics: tuple["PromptDiagnostic", ...] = ()
    ) -> None:
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PromptContextBlock:
    source_id: str
    content: str
    relevant: bool = True
    necessary: bool = True
    authoritative: bool = True
    current: bool = True


@dataclass(frozen=True, slots=True)
class PromptInputBlock:
    source_id: str
    content: str
    trusted: bool = False


@dataclass(frozen=True, slots=True)
class PromptOutputContract:
    mode: OutputMode
    schema_id: str | None
    instructions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptSpec:
    schema_version: int
    prompt_id: str
    prompt_version: str
    template_version: str
    eval_set_version: str
    complexity: PromptComplexity
    task_context: str
    role: str | None
    success_criteria: tuple[str, ...]
    background_context: tuple[PromptContextBlock, ...]
    dynamic_input: tuple[PromptInputBlock, ...]
    instructions: tuple[str, ...]
    execution_order: tuple[str, ...]
    examples: tuple[str, ...]
    constraints: tuple[str, ...]
    failure_behaviour: tuple[str, ...]
    immediate_task: str
    output_contract: PromptOutputContract | None
    critical_reminders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model_id: str
    model_family: str
    supports_structured_output: bool
    supports_tool_calls: bool
    supports_vision: bool
    supports_extended_reasoning: bool
    supports_prefill: bool
    context_limit: int
    preferred_prompt_format: PromptFormat = PromptFormat.TAGGED


@dataclass(frozen=True, slots=True)
class PromptDiagnostic:
    code: str
    severity: DiagnosticSeverity
    section: str


@dataclass(frozen=True, slots=True)
class PromptValidationResult:
    valid: bool
    errors: tuple[PromptDiagnostic, ...]
    warnings: tuple[PromptDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class PromptProvenance:
    prompt_id: str
    prompt_version: str
    prompt_digest: str
    prompt_template_version: str
    eval_set_version: str
    model_id: str
    model_family: str
    context_source_ids: tuple[str, ...]
    output_schema_version: str | None


@dataclass(frozen=True, slots=True)
class CompiledPrompt:
    text: str
    format: PromptFormat
    section_names: tuple[str, ...]
    provenance: PromptProvenance


@dataclass(frozen=True, slots=True)
class PromptEvalCaseResult:
    case_id: str
    status: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptEvalRunResult:
    engine_version: int
    dataset_version: str
    status: str
    total: int
    passed: int
    failed: int
    blocked: int
    corpus_digest: str
    cases: tuple[PromptEvalCaseResult, ...]
