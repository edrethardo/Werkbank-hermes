"""The Werkbank message as it appears in the session's own chat (WB-1148).

It must read as the OWNER's message, never as a Hermes answer:

    👤 *Aaron · Werkbank*
    >the text, line by line, as a blockquote
    >(expandable — ``**>`` … ``||`` — when the text is long)

For Telegram the plugin builds the finished MarkdownV2 itself and the adapter sends it unchanged
(metadata ``TELEGRAM_PREFORMATTED``): the adapter's own ``format_message`` would turn ``**x**`` or
``[a](b)`` in the text into formatting first. Every MarkdownV2 special character of the text is
backslash-escaped, so nothing formats, links or leaves the quote; a word joiner right after
``@ # $ / .`` (when a word character follows) keeps Telegram's server from turning the text into
mention / hashtag / cashtag / bot-command / URL / e-mail entities. Control, format (bidi,
zero-width), surrogate and unassigned characters are dropped — except ZWJ / ZWNJ between two visible
characters (emoji sequences, Persian / Indic scripts). Other platforms get the earlier form: header
(the name as plain letters, see ``plain_name``) plus a literal code block.

The name is ``plugins.entries.werkbank.mirror_name``, else ``Du`` — never the session origin's
``user_name``: the Telegram adapter fills that with the @-username when there is one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

MIRROR_MAX = 3500                    # UTF-16 units of the shown text (before escaping)
PAYLOAD_MAX = 4000                   # UTF-16 units of the finished MarkdownV2 (Telegram holds 4096)
EXPAND_LINES = 6                     # longer than this: an expandable blockquote
EXPAND_UNITS = 600
NAME_MAX = 32
DEFAULT_NAME = "Du"
HEADER = "👤 {name} · Werkbank"
TELEGRAM_PREFORMATTED = "telegram_preformatted_markdown_v2"   # = TelegramAdapter.PREFORMATTED_MDV2
WORD_JOINER = "\u2060"
_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_AUTO_ENTITY = re.compile(r"([@#$/.])(?=\w)")
_FENCE_RUN = re.compile(r"`{3,}")
_JOINERS = ("\u200c", "\u200d")       # ZWNJ, ZWJ: kept between two visible characters
_DROPPED = ("Cc", "Cf", "Cs", "Co", "Cn")


def _visible(ch: str) -> bool:
    return bool(ch) and not ch.isspace() and unicodedata.category(ch) not in _DROPPED + ("Zl", "Zp")


def plain(text: str) -> str:
    """*text* without control, format (bidi, zero-width) or separator characters; tabs become
    spaces, line breaks stay ``\\n``. A ZWJ / ZWNJ stays when both its neighbours in *text* are
    visible characters (👨‍👩‍👧, می‌خواهم, क्‍ष); anywhere else it goes like every other format character."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for i, ch in enumerate(text):
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append(" ")
        elif unicodedata.category(ch) in ("Zl", "Zp"):
            out.append("\n")
        elif ch in _JOINERS:
            if 0 < i < len(text) - 1 and _visible(text[i - 1]) and _visible(text[i + 1]):
                out.append(ch)
        elif unicodedata.category(ch) not in _DROPPED:
            out.append(ch)
    return "".join(out)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_cap(text: str, limit: int) -> str:
    if utf16_len(text) <= limit:
        return text
    used, cut = 0, 0
    for i, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            cut = i
            break
    return text[:cut].rstrip() + " …"


def display_name(configured: Any) -> str:
    """``plugins.entries.werkbank.mirror_name``, else ``Du`` — one line, cleaned, short. No fallback
    to the session origin's ``user_name``: Telegram puts the @-username there (``username or
    full_name``), and the header would show the handle instead of a name."""
    name = " ".join(plain(configured).split()) if isinstance(configured, str) else ""
    if not name:
        return DEFAULT_NAME
    return name if utf16_len(name) <= NAME_MAX else utf16_cap(name, NAME_MAX - 2)


def plain_name(name: str) -> str:
    """*name* for a platform whose Markdown flavour we do not escape for: letters, digits, spaces,
    ``'`` and ``-`` only — nothing can format, mention (``@`` ``<@…>`` ``<!here>``), link or tag.
    Nothing left: ``Du``."""
    kept = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch in "'-" else " " for ch in name)
    return " ".join(kept.split()) or DEFAULT_NAME


def _escape(text: str) -> str:
    return _MDV2_SPECIAL.sub(r"\\\1", _AUTO_ENTITY.sub(r"\1" + WORD_JOINER, text))


def _quote(body: str) -> str:
    lines = [_escape(line) for line in body.split("\n")]
    if len(lines) > EXPAND_LINES or utf16_len(body) > EXPAND_UNITS:
        return "**>" + "\n>".join(lines) + "||"
    return ">" + "\n>".join(lines)


def _body(text: str) -> str:
    return plain(text).strip("\n ")


def telegram_markdown_v2(text: str, name: str) -> str:
    """The finished Telegram MarkdownV2 message (one message: at most ``PAYLOAD_MAX`` UTF-16 units)."""
    header = "👤 *" + _escape(f"{name} · Werkbank") + "*"
    body = _body(text)
    limit = MIRROR_MAX
    while True:
        shown = utf16_cap(body, limit)
        payload = header + "\n" + _quote(shown)
        over = utf16_len(payload) - PAYLOAD_MAX
        if over <= 0 or limit <= 0:
            return payload
        # one shown unit costs 1–3 payload units (backslash, word joiner after ``.``/``@``…, ``>`` per
        # line): cutting half the overshoot (≥ 16) never cuts far too much and ends in a few rounds
        limit -= max(over // 2, 16)


def plain_block(text: str, name: str) -> str:
    """Other platforms: the header, then the text as a literal code block. A run of three backticks
    in the text is broken up so it cannot close the block."""
    body = _FENCE_RUN.sub(lambda m: "`\u2009" * (len(m.group()) - 1) + "`", _body(text))
    return HEADER.format(name=plain_name(name)) + "\n```\n" + utf16_cap(body, MIRROR_MAX) + "\n```"


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB".replace(".", ",")
    return f"{max(1, round(n / 1024))} KB"


def with_attachments(text: str, files) -> str:
    """WB-1174: the mirrored text plus one line per attachment (``📷`` image, ``📎`` file, name and
    size); the images follow as photos replying to the mirror. No attachments: *text* unchanged."""
    if not files:
        return text
    lines = "\n".join(("📷 " if f.get("kind") == "image" else "📎 ") + f"{f.get('name')} ({_size(int(f.get('size') or 0))})"
                      for f in files)
    return f"{text.rstrip()}\n\n{lines}" if text.strip() else lines


def for_platform(platform: Optional[str], text: str, name: str):
    """``(content, extra metadata)`` for a send into a chat on *platform*."""
    if platform == "telegram":
        return telegram_markdown_v2(text, name), {TELEGRAM_PREFORMATTED: True}
    return plain_block(text, name), {}
