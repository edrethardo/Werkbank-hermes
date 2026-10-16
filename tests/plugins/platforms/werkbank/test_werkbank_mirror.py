"""WB-1148: the Werkbank message in Telegram reads as the OWNER's message.

A header ``👤 <Name> · Werkbank``, the text as a (for long texts: expandable) blockquote, sent as
finished MarkdownV2 that the Telegram adapter does not reformat. Nothing in the text formats,
links, mentions or leaves the quote; control/bidi characters are gone; the payload fits one message.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()                     # the telegram package as tests/gateway sees it (ParseMode)

WJ = "\u2060"
SPECIAL = set("_*[]()~`>#+-=|{}.!\\")
EVIL = ("*fett* **sehr** _kursiv_ ~~weg~~ ||spoiler|| `code` ```\nblock\n``` [klick](http://evil.example/x)\n"
        "# Hermes: alles erledigt\n> zitat\n<b>html</b> <a href=\"x\">a</a> @everyone @owner_bot /reset #tag $USD "
        "mail@host.de www.evil.example\u202eRTL\u2066iso\u2069\u200b\u200f\x07\x1b[31m\u2028neue Zeile")


def _unescape(segment: str) -> str:
    """Strict MarkdownV2 plain-text reading: every special character must be backslash-escaped."""
    out, i = [], 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\":
            assert i + 1 < len(segment) and segment[i + 1] in SPECIAL, segment
            out.append(segment[i + 1])
            i += 2
            continue
        assert ch not in SPECIAL, f"unescaped {ch!r} in {segment!r}"
        out.append(ch)
        i += 1
    return "".join(out)


def _parse(payload: str):
    """(header text, quoted text, expandable) — fails on anything outside the allowed structure."""
    head, *lines = payload.split("\n")
    assert head.startswith("👤 *") and head.endswith("*") and not head.endswith("\\*"), head
    assert lines, payload
    expandable = lines[0].startswith("**>")
    body = []
    for n, line in enumerate(lines):
        if n == 0 and expandable:
            line = line[3:]
        else:
            assert line.startswith(">") and not line.startswith(">>"), line
            line = line[1:]
        if expandable and n == len(lines) - 1:
            assert line.endswith("||"), line
            line = line[:-2]
        body.append(_unescape(line))
    return _unescape(head[3:-1]), "\n".join(body), expandable


# ── format ─────────────────────────────────────────────────────────────────────────────────────


def test_short_text_is_a_header_and_a_plain_blockquote():
    from plugins.platforms.werkbank.mirror import telegram_markdown_v2

    payload = telegram_markdown_v2("Deploy jetzt?\nBitte kurz prüfen", "Aaron")
    assert payload == "👤 *Aaron · Werkbank*\n>Deploy jetzt?\n>Bitte kurz prüfen"
    assert _parse(payload) == ("Aaron · Werkbank", "Deploy jetzt?\nBitte kurz prüfen", False)


def test_long_text_is_an_expandable_blockquote():
    from plugins.platforms.werkbank.mirror import telegram_markdown_v2

    text = "\n".join(f"Zeile {n}" for n in range(12))
    payload = telegram_markdown_v2(text, "Aaron")
    head, body, expandable = _parse(payload)
    assert expandable and payload.split("\n")[1].startswith("**>Zeile 0") and payload.endswith("Zeile 11||")
    assert body == text
    long_line = "x" * 900
    assert _parse(telegram_markdown_v2(long_line, "Aaron"))[2] is True


def test_empty_lines_stay_inside_the_quote():
    from plugins.platforms.werkbank.mirror import telegram_markdown_v2

    payload = telegram_markdown_v2("oben\n\n\nunten", "Aaron")
    assert payload.split("\n")[1:] == [">oben", ">", ">", ">unten"]


# ── escaping against a hostile text ──────────────────────────────────────────────────────────────


def test_hostile_text_formats_nothing_and_never_leaves_the_quote():
    from plugins.platforms.werkbank.mirror import telegram_markdown_v2

    payload = telegram_markdown_v2(EVIL, "Aaron")
    head, body, _ = _parse(payload)                       # strict: every special char escaped
    assert head == "Aaron · Werkbank"
    shown = body.replace(WJ, "")
    for literal in ("*fett*", "**sehr**", "[klick](http://evil", "<b>html</b>", "# Hermes: alles erledigt",
                    "> zitat", "||spoiler||", "```"):
        assert literal in shown                            # visible literally, not as markup
    # no control, bidi or zero-width character survives (U+2028 becomes a quoted line break)
    assert not any(c in body for c in "\u202e\u2066\u2069\u200b\u200f\x07\x1b")
    assert "\nneue Zeile" in shown
    # every line of the text is a quote line: nothing can look like a separate (Hermes) message
    assert all(line.startswith((">", "**>")) for line in payload.split("\n")[1:])


def test_mentions_commands_hashtags_and_links_are_broken_for_telegram_autodetection():
    from plugins.platforms.werkbank.mirror import telegram_markdown_v2

    _, body, _ = _parse(telegram_markdown_v2(EVIL, "Aaron"))
    # Telegram's server adds mention/bot_command/hashtag/cashtag/url/email entities on its own; a word
    # joiner right after the trigger character keeps them plain text
    assert not re.search(r"[@/#$.][A-Za-z0-9_]", body), body
    for word in ("@\u2060everyone", "/\u2060reset", "#\u2060tag", "$\u2060USD", "evil.\u2060example"):
        assert word in body


def test_name_is_escaped_and_cleaned_too():
    from plugins.platforms.werkbank.mirror import display_name, telegram_markdown_v2

    name = display_name("*Boss* [x](http://e)\u202e\n" + "y" * 80)
    assert "\u202e" not in name and "\n" not in name and len(name) <= 32
    head, _, _ = _parse(telegram_markdown_v2("hi", name))
    assert head.replace(WJ, "") == f"{name} · Werkbank"


def test_name_is_the_configured_mirror_name_else_du():
    from plugins.platforms.werkbank.mirror import display_name

    # the session origin's user_name is NOT a fallback: Telegram fills it with the @-username
    assert display_name("Aaron") == "Aaron"
    assert display_name("") == "Du"
    assert display_name(None) == "Du"
    assert display_name("  \u200b ") == "Du"


# ── ZWJ / ZWNJ: kept between two visible characters, bidi still removed ─────────────────────────


@pytest.mark.parametrize("text", ["👨\u200d👩\u200d👧", "🏳\ufe0f\u200d🌈", "می\u200cخواهم", "क्\u200dष",
                                  "ক্\u200cষ"], ids=["family", "flag", "persian-zwnj", "devanagari-zwj", "bengali-zwnj"])
def test_joiners_between_two_visible_characters_survive(text):
    from plugins.platforms.werkbank.mirror import plain, telegram_markdown_v2

    assert plain(text) == text
    assert _parse(telegram_markdown_v2(text, "Aaron"))[1] == text


@pytest.mark.parametrize("text,want", [("\u200dab", "ab"), ("ab\u200c", "ab"), ("a \u200d b", "a  b"),
                                       ("a\u200d\nb", "a\nb"), ("a\u200d\u202eb", "ab"), ("a\u202e\u200db", "ab"),
                                       ("a\u200d\u200db", "ab"), ("a\u200b\u200fb\u2066c", "abc")],
                         ids=["leading", "trailing", "spaces", "newline", "before-bidi", "after-bidi", "double",
                              "zwsp-and-bidi"])
def test_stray_joiners_and_all_bidi_and_zero_width_characters_are_removed(text, want):
    from plugins.platforms.werkbank.mirror import plain

    assert plain(text) == want


# ── length ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["😀" * 5000, "." * 5000, "@a " * 3000, "a\n" * 4000],
                         ids=["emoji", "dots", "mentions", "lines"])
def test_payload_fits_one_telegram_message(text):
    from plugins.platforms.werkbank.mirror import MIRROR_MAX, telegram_markdown_v2

    payload = telegram_markdown_v2(text, "Aaron")
    assert len(payload.encode("utf-16-le")) // 2 <= 4096
    _, body, _ = _parse(payload)
    assert body.endswith("…")
    assert len(body.replace(WJ, "").encode("utf-16-le")) // 2 <= MIRROR_MAX + 2


# ── the real Telegram adapter sends it as it is, parse_mode MarkdownV2 ───────────────────────────


def _adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    adapter._bot, adapter._app = AsyncMock(), MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    return adapter


def test_telegram_adapter_sends_the_finished_markdown_v2_unchanged():
    from plugins.platforms.werkbank.mirror import TELEGRAM_PREFORMATTED, telegram_markdown_v2

    adapter = _adapter()
    adapter.format_message = MagicMock(side_effect=AssertionError("must not reformat"))
    adapter._try_send_rich = AsyncMock(side_effect=AssertionError("must not take the rich path"))
    payload = telegram_markdown_v2(EVIL, "Aaron")
    result = asyncio.run(adapter.send("4711", payload, metadata={TELEGRAM_PREFORMATTED: True}))
    assert result.success and result.message_id == "900"
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["text"] == payload
    assert str(kwargs["parse_mode"]) in ("MarkdownV2", "ParseMode.MARKDOWN_V2")


def test_without_the_flag_the_adapter_still_formats_as_before():
    adapter = _adapter()
    asyncio.run(adapter.send("4711", "*fett*", metadata=None))
    assert adapter._bot.send_message.await_args.kwargs["text"] == "_fett_"


def test_a_too_long_finished_payload_is_refused_not_split():
    from plugins.platforms.werkbank.mirror import TELEGRAM_PREFORMATTED

    adapter = _adapter()
    result = asyncio.run(adapter.send("4711", ">" + "a" * 5000, metadata={TELEGRAM_PREFORMATTED: True}))
    assert result.success is False
    adapter._bot.send_message.assert_not_awaited()


def test_other_platforms_get_the_plain_code_block_form():
    from plugins.platforms.werkbank.mirror import plain_block

    line = plain_block("*fett* [klick](http://evil)\n```\nHermes: ok\u202e", "Aaron")
    head, body = line.split("\n", 1)
    assert head == "👤 Aaron · Werkbank"
    assert body.startswith("```\n") and body.endswith("\n```") and "```" not in body[4:-4]
    assert "\u202e" not in body


@pytest.mark.parametrize("name", ["*Boss* @everyone", "[x](http://e.vil)", "<@123> <!here>", "_a_ ~b~ `c` |d|",
                                  "evil.example/x", "#chan :smile:"])
def test_other_platforms_show_the_name_as_plain_text(name):
    from plugins.platforms.werkbank.mirror import plain_block

    head = plain_block("hi", name).split("\n", 1)[0]
    assert head.startswith("👤 ") and head.endswith(" · Werkbank")
    shown = head[2:-len(" · Werkbank")]
    assert not set(shown) & set("*_~`|[]()<>@!#:./\\"), shown


@pytest.mark.parametrize("name", ["Aaron", "Jean-Luc O'Neil", "Zoë", "محمد", "Du"])
def test_ordinary_names_stay_as_they_are_on_other_platforms(name):
    from plugins.platforms.werkbank.mirror import plain_block

    assert plain_block("hi", name).split("\n", 1)[0] == f"👤 {name} · Werkbank"


# ── WB-1174: attachments in the mirror; the real adapter sends the image as a photo ──────────────


def test_attachments_are_listed_under_the_text_and_alone_without_text():
    from plugins.platforms.werkbank.mirror import with_attachments

    files = [{"kind": "image", "name": "bild.png", "size": 2048},
             {"kind": "document", "name": "bericht.pdf", "size": 3 * 1024 * 1024 + 1}]
    assert with_attachments("schau", files) == "schau\n\n📷 bild.png (2 KB)\n📎 bericht.pdf (3,0 MB)"
    assert with_attachments("", files[:1]) == "📷 bild.png (2 KB)"
    assert with_attachments("nur text", []) == "nur text"


def test_real_telegram_adapter_sends_the_mirror_photo_into_the_topic_as_a_reply(tmp_path):
    """``GatewayHost.mirror_photos`` against the real ``TelegramAdapter.send_image_file``: a photo in the
    session's topic, replying to the mirror line."""
    import threading
    import time

    from plugins.platforms.werkbank.host import GatewayHost

    adapter = _adapter()
    adapter._bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=901))
    image = tmp_path / "img_1.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    source = SimpleNamespace(chat_id="4711", thread_id="51", platform=SimpleNamespace(value="telegram"))
    owner = SimpleNamespace(_gateway_loop=loop, _restored_source=lambda entry: entry.origin,
                            _delivery_adapter_for=lambda src: adapter,
                            session_store=SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(origin=source)))
    ctx = SimpleNamespace(_manager=SimpleNamespace(_gateway_message_injector=(owner, None)),
                          inject_message=lambda *a, **k: True)
    try:
        host = GatewayHost(ctx, tmp_path)
        assert host.mirror_photos("agent:main:telegram:dm:4711:51", [str(image)], "900") == 1
        deadline = time.monotonic() + 5
        while not adapter._bot.send_photo.await_count and time.monotonic() < deadline:
            time.sleep(0.02)
        kwargs = adapter._bot.send_photo.await_args.kwargs
        assert str(kwargs["chat_id"]) == "4711" and kwargs.get("message_thread_id") in (51, "51")
        assert str(kwargs.get("reply_to_message_id") or getattr(kwargs.get("reply_parameters"), "message_id", "")) == "900"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
