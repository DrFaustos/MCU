"""Тесты логики чата (без pjsua2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.chat import (  # noqa: E402
    MAX_MESSAGE_LEN,
    ChatHistory,
    normalize_message,
)


def test_normalize_empty():
    assert normalize_message("") is None
    assert normalize_message("   ") is None
    assert normalize_message(None) is None


def test_normalize_trims():
    assert normalize_message("  привет  ") == "привет"


def test_normalize_truncates():
    long = "x" * (MAX_MESSAGE_LEN + 100)
    out = normalize_message(long)
    assert len(out) == MAX_MESSAGE_LEN


def test_history_incoming_outgoing():
    h = ChatHistory()
    h.add_incoming("sip:100@10.0.0.1", "привет")
    h.add_outgoing("ответ")
    msgs = h.messages
    assert len(msgs) == 2
    assert msgs[0].outgoing is False and msgs[0].sender.endswith("10.0.0.1")
    assert msgs[1].outgoing is True and msgs[1].status == "sent"


def test_mark_last_outgoing_status():
    h = ChatHistory()
    h.add_outgoing("a")
    h.add_outgoing("b")
    h.mark_last_outgoing("delivered")
    assert h.messages[-1].status == "delivered"
    assert h.messages[0].status == "sent"


def test_mark_without_outgoing_is_none():
    h = ChatHistory()
    h.add_incoming("peer", "x")
    assert h.mark_last_outgoing("delivered") is None


def test_history_max_items():
    h = ChatHistory(max_items=3)
    for i in range(5):
        h.add_outgoing(f"m{i}")
    assert len(h.messages) == 3
    assert h.messages[0].content == "m2"


def test_clear():
    h = ChatHistory()
    h.add_outgoing("x")
    h.clear()
    assert h.messages == []


def test_as_dict():
    h = ChatHistory()
    m = h.add_incoming("peer", "hi")
    d = m.as_dict()
    assert d["sender"] == "peer" and d["content"] == "hi" and "ts" in d
