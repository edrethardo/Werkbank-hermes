"""An approval button authorizes its delivered message and exact request."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import approval


def adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token='test-token'))
    a._bot = AsyncMock()
    a._bot.send_message.side_effect = [SimpleNamespace(message_id=42, message_thread_id=51), SimpleNamespace(message_id=43, message_thread_id=51)]
    a._callback_authorized = AsyncMock(return_value=True)
    a._edit_md_quiet = AsyncMock()
    return a


def query(message_id=42):
    return SimpleNamespace(message=SimpleNamespace(message_id=message_id), answer=AsyncMock(),
                          from_user=SimpleNamespace(first_name='tester'))


@pytest.mark.asyncio
async def test_second_card_clicked_first_resolves_second_request_only():
    a = adapter()
    key = 'telegram:exact-card-test'
    first = SimpleNamespace(event=threading.Event(), data={'request_id':'A'}, result=None)
    second = SimpleNamespace(event=threading.Event(), data={'request_id':'B'}, result=None)
    with approval._lock:
        approval._gateway_queues[key] = [first,second]
    try:
        await a.send_exec_approval('12345','operation A',key,metadata={'approval_request_id':'A','thread_id':'51'})
        await a.send_exec_approval('12345','operation B',key,metadata={'approval_request_id':'B','thread_id':'51'})
        ids = list(a._approval_state)
        cb = {'chat_id':'12345','thread_id':'51'}
        await a._handle_exec_approval_callback(query(43),'ea:once:'+ids[1],cb)
        assert second.event.is_set() and second.result == 'once'
        assert not first.event.is_set() and first.result is None
        await a._handle_exec_approval_callback(query(42),'ea:deny:'+ids[0],cb)
        assert first.event.is_set() and first.result == 'deny'
        assert key not in approval._gateway_queues
    finally:
        approval.unregister_gateway_notify(key)


@pytest.mark.asyncio
async def test_old_card_after_adapter_restart_never_resolves_new_request(monkeypatch):
    old, new = adapter(), adapter()
    await old.send_exec_approval('12345','old','s',metadata={'approval_request_id':'OLD','thread_id':'51'})
    await new.send_exec_approval('12345','new','s',metadata={'approval_request_id':'NEW','thread_id':'51'})
    old_token, new_token = next(iter(old._approval_state)), next(iter(new._approval_state))
    assert old_token != new_token
    spy = AsyncMock()
    monkeypatch.setattr(approval,'resolve_gateway_approval',spy)
    await new._handle_exec_approval_callback(query(),'ea:once:'+old_token,{'chat_id':'12345','thread_id':'51'})
    spy.assert_not_called()
    assert new_token in new._approval_state


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('chat_id','other'),('thread_id','52'),('message_id',99)])
async def test_wrong_delivery_does_not_consume_or_resolve_card(monkeypatch,field,value):
    a = adapter()
    await a.send_exec_approval('12345','op','s',metadata={'approval_request_id':'R','thread_id':'51'})
    token = next(iter(a._approval_state))
    spy = AsyncMock()
    monkeypatch.setattr(approval,'resolve_gateway_approval',spy)
    cb = {'chat_id':'12345','thread_id':'51'}
    q = query()
    if field == 'message_id':
        q.message.message_id = value
    else:
        cb[field] = value
    await a._handle_exec_approval_callback(q,'ea:once:'+token,cb)
    spy.assert_not_called()
    assert token in a._approval_state


@pytest.mark.asyncio
async def test_legacy_unbound_card_never_resolves_fifo(monkeypatch):
    a = adapter()
    a._approval_state['1'] = 'telegram:old-card'
    spy = AsyncMock()
    monkeypatch.setattr(approval,'resolve_gateway_approval',spy)
    await a._handle_exec_approval_callback(query(),'ea:once:1',{'chat_id':'12345','thread_id':'51'})
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_card_cannot_escalate_beyond_offered_choices(monkeypatch):
    a = adapter()
    await a.send_exec_approval('12345','op','s',allow_permanent=False,smart_denied=True,
                               metadata={'approval_request_id':'R','thread_id':'51'})
    token = next(iter(a._approval_state))
    spy = AsyncMock()
    monkeypatch.setattr(approval,'resolve_gateway_approval',spy)
    await a._handle_exec_approval_callback(query(),'ea:always:'+token,{'chat_id':'12345','thread_id':'51'})
    spy.assert_not_called()
    assert token in a._approval_state
