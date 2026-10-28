"""WB-1338 (fork: werkbank): a delegated child's spawns carry HERMES_DELEGATION_ID / _TASK /
HERMES_PARENT_SESSION_ID — set from the child itself, stripped everywhere else."""

from unittest.mock import patch

from agent import delegation_identity as di
from agent.delegation_context import delegated_child_context, delegated_child_subprocess_env
from tools.delegate_tool_child_run import _ChildRun


class _Child:
    provider = "openrouter"
    model = "m"
    _delegation_id = "deleg_ab12cd34"
    _parent_session_id = "20260924_005851_4ee726"
    session_id = "20261028_154807_354623"


def test_bound_child_spawn_gets_the_three_names():
    with delegated_child_context(_Child.session_id), di.bound(_Child(), 3):
        env = delegated_child_subprocess_env({"PATH": "/usr/bin"})
    assert env["HERMES_DELEGATION_ID"] == "deleg_ab12cd34"
    assert env["HERMES_DELEGATION_TASK"] == "3"
    assert env["HERMES_PARENT_SESSION_ID"] == "20260924_005851_4ee726"


def test_stale_values_are_stripped_outside_a_bound_child():
    inherited = {"PATH": "/usr/bin", "HERMES_DELEGATION_ID": "deleg_00000000",
                 "HERMES_DELEGATION_TASK": "9", "HERMES_PARENT_SESSION_ID": "x"}
    with delegated_child_context("s"):           # a child context without identity (e.g. a test double)
        env = delegated_child_subprocess_env(dict(inherited))
    assert not set(di.KEYS) & set(env)


def test_bad_ids_are_not_exported():
    class Odd(_Child):
        _delegation_id = "../../etc"
        _parent_session_id = "a b"
    assert di.identity_of(Odd(), 0) is None
    assert di.identity_of(_Child(), -1) is None
    assert di.identity_of(_Child(), True) is None
    class NoParent(_Child):
        _parent_session_id = None
    assert di.identity_of(NoParent(), 0) == {"HERMES_DELEGATION_ID": "deleg_ab12cd34",
                                            "HERMES_DELEGATION_TASK": "0"}


def test_child_run_binds_its_identity_for_its_turn():
    """The real child-run path: a terminal spawned during the child's turn sees its own ids."""
    seen = {}

    class Child(_Child):
        def run_conversation(self, user_message, **kw):
            seen["env"] = delegated_child_subprocess_env({})
            return {"final_response": "ok", "completed": True}

    class Parent:
        _current_task_id = None

    run = _ChildRun(Child(), Parent(), 2, "goal", None, None)
    with patch("tools.delegate_tool_child_run._create_isolated_worktree", return_value=None):
        run.seed_workspace()
        result, failure, _ = run.await_child()
    assert failure is None and result["final_response"] == "ok"
    assert seen["env"]["HERMES_DELEGATION_ID"] == "deleg_ab12cd34"
    assert seen["env"]["HERMES_DELEGATION_TASK"] == "2"
    assert di.current() is None                  # nothing left behind on the caller's context
