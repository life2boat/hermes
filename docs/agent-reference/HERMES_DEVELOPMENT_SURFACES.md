# Hermes development surfaces reference

Load this reference only when a task changes one of the named application
surfaces. It is not a lifecycle, deployment, or production-authority contract.

## Routing

| Changed surface | Start with |
| --- | --- |
| Agent loop, tool registry, approval handling | `run_agent.py`, `model_tools.py`, `tools/registry.py`, and adjacent tests |
| CLI or slash command | `cli.py`, `hermes_cli/commands.py`, and command tests |
| TUI, dashboard, desktop application | `ui-tui/`, `tui_gateway/`, `apps/desktop/`, and the nearest UI tests |
| Gateway transport or platform adapter | `gateway/`, the platform adapter, and focused gateway tests |
| Tool implementation | tool schema, registry/dispatch, availability checks, and tool tests |
| Configuration or environment loading | `hermes_cli/config.py`, loader call sites, and configuration tests |
| Plugin or skill behavior | the applicable plugin/skill contract and its focused tests |
| Test infrastructure | `tests/conftest.py`, local test helpers, and the target test module |

## Durable development conventions

- Keep dependencies bounded and follow existing package/lockfile conventions.
- Do not hard-code home-directory paths; honor the repository's profile and
  environment resolution contracts.
- Keep tool schemas independent of unavailable neighboring tools. Preserve
  gateway guard behavior for control/approval commands.
- Use behavioral tests for relationships and invariants rather than brittle
  snapshots of changing catalogs, versions, or enumeration counts.
- Keep tests hermetic: do not write to a real user home or call an external
  provider unless the task explicitly authorizes it.

The source tree and its tests are the authority for surface-specific details.
If a surface has its own `AGENTS.md`, skill, ADR, or executable validator, load
that artifact before changing it.
