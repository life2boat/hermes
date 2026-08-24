import json
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).parent.parent
DEPLOY_CONFIG_PATH = ROOT / 'deploy' / 'hermes-production.json'
WORKFLOWS_DIR = ROOT / '.github' / 'workflows'

def test_required_ci_workflows_trigger_unconditionally_on_main() -> None:
    with open(DEPLOY_CONFIG_PATH, 'r', encoding='utf-8') as f:
        deploy_config = json.load(f)
    
    required_workflows = set(deploy_config.get('provenance', {}).get('required_ci_workflows', []))
    assert required_workflows
    
    workflow_files = list(WORKFLOWS_DIR.glob('*.yml')) + list(WORKFLOWS_DIR.glob('*.yaml'))
    
    found_workflows = {}
    for wf_path in workflow_files:
        with open(wf_path, 'r', encoding='utf-8') as f:
            try:
                wf_data = yaml.safe_load(f)
            except Exception:
                continue
        if wf_data and 'name' in wf_data:
            found_workflows[wf_data['name']] = wf_data

    for req_wf in required_workflows:
        assert req_wf in found_workflows
        wf = found_workflows[req_wf]
        
        on_triggers = wf.get('on')
        if on_triggers is None:
            on_triggers = wf.get(True, {})
            
        if isinstance(on_triggers, str):
            assert on_triggers == 'push'
            continue
        if isinstance(on_triggers, list):
            assert 'push' in on_triggers
            continue
            
        assert isinstance(on_triggers, dict)
        
        assert 'push' in on_triggers, f"'{req_wf}' must have 'push'"
        push_trigger = on_triggers['push']
        
        if push_trigger is None:
            continue
            
        branches = push_trigger.get('branches')
        if branches is not None:
            assert 'main' in branches
            
        paths = push_trigger.get('paths')
        paths_ignore = push_trigger.get('paths-ignore')
        
        assert paths is None, f"{req_wf} has paths"
        assert paths_ignore is None, f"{req_wf} has paths-ignore"
