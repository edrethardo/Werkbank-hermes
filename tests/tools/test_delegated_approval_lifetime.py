"""Detached work retains its human notification surface, never consent."""
import threading

import pytest

from tools import approval


def test_parent_turn_end_keeps_detached_notification_surface():
    key = 'telegram:delegated-lifetime-test'
    notify = lambda data: None
    approval.register_gateway_notify(key, notify)
    lease = approval.retain_gateway_notify(key)
    try:
        approval.unregister_gateway_notify(key)
        assert approval._gateway_notify_cb(key) is notify
        assert not approval.is_approved(key, 'execute_code')
    finally:
        lease()
        approval.unregister_gateway_notify(key)
    assert approval._gateway_notify_cb(key) is None


def test_parent_end_does_not_cancel_child_wait_and_reply_resolves_it():
    key = 'telegram:delegated-queue-test'
    approval.register_gateway_notify(key, lambda data: None)
    lease = approval.retain_gateway_notify(key)
    event = threading.Event()
    from types import SimpleNamespace
    entry = SimpleNamespace(event=event, data={'request_id': 'child'}, choice=None, reason=None)
    with approval._lock:
        approval._gateway_queues[key] = [entry]
    try:
        approval.unregister_gateway_notify(key)
        assert not event.is_set()
        assert approval.resolve_gateway_approval(key, 'once', request_id='child') == 1
        assert event.is_set()
    finally:
        lease()
        approval.unregister_gateway_notify(key)


def test_multiple_children_release_independently_and_cleanup_is_idempotent():
    key = 'telegram:delegated-multi-test'
    notify = lambda data: None
    approval.register_gateway_notify(key, notify)
    a, b = approval.retain_gateway_notify(key), approval.retain_gateway_notify(key)
    approval.unregister_gateway_notify(key)
    a(); a()
    assert approval._gateway_notify_cb(key) is notify
    b()
    assert approval._gateway_notify_cb(key) is None


def test_new_parent_turn_wins_and_old_child_cleanup_does_not_remove_it():
    key = 'telegram:delegated-next-turn-test'
    old, new = lambda data: None, lambda data: None
    approval.register_gateway_notify(key, old)
    lease = approval.retain_gateway_notify(key)
    approval.unregister_gateway_notify(key)
    approval.register_gateway_notify(key, new)
    lease()
    assert approval._gateway_notify_cb(key) is new
    approval.unregister_gateway_notify(key)


def test_missing_surface_stays_missing_and_other_session_never_inherits():
    key = 'telegram:delegated-missing-test'
    release = approval.retain_gateway_notify(key)
    assert approval._gateway_notify_cb(key) is None
    release()


@pytest.mark.parametrize('choice', ['once', 'deny'])
def test_execute_code_after_parent_return_notifies_and_waits_for_exact_reply(monkeypatch, choice):
    key = 'telegram:detached-execute-' + choice
    calls = []

    def notify(data):
        calls.append(data)
        assert approval.resolve_gateway_approval('telegram:another-chat', 'once') == 0
        assert approval.resolve_gateway_approval(key, choice, request_id=data['request_id']) == 1

    monkeypatch.setattr(approval, '_presence', lambda: (None, False, True, True))
    monkeypatch.setattr(approval.approval_context, '_get_approval_mode', lambda: 'manual')
    monkeypatch.setattr(approval, '_unattended_contexts', lambda: [])
    monkeypatch.setattr(approval, '_yolo_active', lambda: False)
    monkeypatch.setattr(approval, '_should_skip_container_guards', lambda *a, **k: False)
    token = approval.approval_context.set_current_session_key(key)
    approval.register_gateway_notify(key, notify)
    lease = approval.retain_gateway_notify(key)
    approval.unregister_gateway_notify(key)
    try:
        result = approval.check_execute_code_guard('print("test only")', 'local')
        assert len(calls) == 1
        assert result['approved'] is (choice == 'once')
        assert result.get('status') != 'pending_approval'
    finally:
        lease()
        approval.unregister_gateway_notify(key)
        approval.approval_context.reset_current_session_key(token)


@pytest.mark.parametrize('crash', [False, True])
def test_async_dispatch_retains_and_releases_surface(crash):
    from tools import async_delegation as ad
    key = 'telegram:dispatch-lease-test'
    entered, finish, inspected = threading.Event(), threading.Event(), threading.Event()
    notify = lambda data: None
    ad._reset_for_tests()
    approval.register_gateway_notify(key, notify)

    def runner():
        entered.set()
        assert finish.wait(5)
        assert approval._gateway_notify_cb(key) is notify
        inspected.set()
        if crash:
            raise RuntimeError('test crash')
        return {'status': 'completed', 'summary': 'test only'}

    try:
        handle = ad.dispatch_async_delegation(goal='test', context='', toolsets=[], role='leaf',
            model=None, session_key=key, runner=runner, max_async_children=10)
        assert handle['status'] == 'dispatched'
        assert entered.wait(5)
        approval.unregister_gateway_notify(key)
        assert approval._gateway_notify_cb(key) is notify
        finish.set()
        assert inspected.wait(5)
        import time
        deadline = time.monotonic() + 5
        while approval._gateway_notify_cb(key) is not None and time.monotonic() < deadline:
            time.sleep(.01)
        assert approval._gateway_notify_cb(key) is None
    finally:
        finish.set()
        approval.unregister_gateway_notify(key)
        ad._reset_for_tests()
