"""WB-950: ``wb:`` taps reach the Werkbank spool only when switched on."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _adapter(extra=None):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra=extra or {}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _update(data, user_id="777"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.message_thread_id = 51
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "T"
    update = MagicMock()
    update.callback_query = query
    return update, query


@pytest.mark.asyncio
async def test_off_by_default_writes_nothing(tmp_path):
    adapter = _adapter()
    update, query = _update("wb:WB-1:A")
    adapter._handle_wb_tap = AsyncMock()
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await adapter._handle_callback_query(update, MagicMock())
    query.answer.assert_not_called()
    adapter._handle_wb_tap.assert_not_called()


@pytest.mark.asyncio
async def test_on_writes_one_spool_file(tmp_path):
    spool = tmp_path / "spool"
    adapter = _adapter({"wb_tap_spool": str(spool)})
    update, query = _update("wb:WB-1:A")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await adapter._handle_callback_query(update, MagicMock())
    files = [p for p in spool.iterdir() if p.suffix == ".json"]
    assert len(files) == 1
    got = json.loads(files[0].read_text())
    assert got["data"] == "wb:WB-1:A" and got["user_id"] == "777"
    assert got["thread_id"] == 51
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_user_writes_nothing(tmp_path):
    spool = tmp_path / "spool"
    adapter = _adapter({"wb_tap_spool": str(spool)})
    update, query = _update("wb:WB-1:A", user_id="999")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}):
        await adapter._handle_callback_query(update, MagicMock())
    assert not spool.exists() or list(spool.iterdir()) == []


@pytest.mark.asyncio
async def test_other_prefixes_unchanged(tmp_path):
    spool = tmp_path / "spool"
    adapter = _adapter({"wb_tap_spool": str(spool)})
    update, query = _update("zz:nothing")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await adapter._handle_callback_query(update, MagicMock())
    assert not spool.exists()
