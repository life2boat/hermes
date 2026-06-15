from types import SimpleNamespace

from agent.system_prompt import build_system_prompt_parts


class _FakeRunAgent:
    @staticmethod
    def load_soul_md():
        return ""

    @staticmethod
    def build_environment_hints():
        return ""

    @staticmethod
    def build_context_files_prompt(*, cwd=None, skip_soul=False):
        return ""

    @staticmethod
    def build_nous_subscription_prompt(valid_tool_names=None):
        return ""

    @staticmethod
    def build_skills_system_prompt(**kwargs):
        return ""

    @staticmethod
    def get_toolset_for_tool(tool_name):
        return None


def test_system_prompt_includes_healbite_public_search_guidance(monkeypatch):
    monkeypatch.setattr("agent.system_prompt._ra", lambda: _FakeRunAgent)
    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names={"healbite_public_search"},
        _task_completion_guidance=False,
        _kanban_worker_guidance=None,
        _tool_use_enforcement=False,
        provider="test",
        model="test-model",
        platform="telegram",
        _environment_probe=False,
        _memory_store=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        _memory_manager=None,
        pass_session_id=False,
        session_id="",
    )

    parts = build_system_prompt_parts(agent)

    assert "healbite_public_search" in parts["stable"]
    assert "Do NOT include personal user data" in parts["stable"]
