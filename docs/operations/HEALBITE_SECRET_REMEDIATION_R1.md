# HealBite Secret Remediation R1

This runbook tooling splits legacy environment variables from hermes-bot into safe runtime env and protected production secrets, recreating the bot using race-safe filesystem updates.

## Modules

- `safe_fs.py`: safe publication primitives
- `parent_dir.py`: safe parent directory creation
- `process_identity.py`: container validation
- `env_split.py`: configuration extraction

## Usage

Use `ops.secret_remediation_r1.executor.run_remediation()`
