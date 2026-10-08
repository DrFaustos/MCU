"""Чат-стенд: SIP MESSAGE между двумя MCU (текст доезжает целым).

Unit-тесты проверяют только вызовы pjsua2, но не то, какой текст реально
приходит второй стороне. Ровно так и жил баг: читали ``prm.rdata.wholeMsg``
(ВЕСЬ SIP-пакет: стартовая строка, заголовки, тело) вместо ``prm.msgBody``,
а фейк в тестах повторял то же предположение — и оставался «зелёным».
Здесь проверка настоящая: два процесса, реальный SIP, сверка текста.

Использование:
  python3 two_instance_chat.py listen <port>
  python3 two_instance_chat.py call <port> <target_port>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.models import CallState  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.pump import pump  # noqa: E402

#: Кириллица + пробелы + двоеточие: если код возьмёт wholeMsg целиком, в
#: тексте окажутся заголовки SIP, и точное сравнение ниже это поймает.
TEXT = "привет из MCU: чат 1234"


def _mk(port: int) -> SipEngine:
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = port
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.1/32"]
    return SipEngine(cfg)


def _confirmed(events) -> bool:
    return any(e == "call.confirmed" for e, _ in events) or any(
        e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events
    )


def _incoming_texts(events) -> "list[str]":
    """Тексты входящих сообщений в порядке приёма."""
    return [
        p.get("content", "")
        for e, p in events
        if e == "chat.message" and not p.get("outgoing")
    ]


def _confirmed_pid(engine) -> "int | None":
    """id первого подтверждённого участника — в него и шлём MESSAGE."""
    room = getattr(engine, "room", None)
    if room is None:
        return None
    items = getattr(room, "participants", None)
    # Room держит dict[id, Participant]; list допускаем на случай фейка в
    # стенде — иначе Pyright правомерно не может сузить тип.
    peers = items.values() if isinstance(items, dict) else (items or [])
    for p in list(peers):
        if getattr(p, "state", None) is CallState.CONFIRMED:
            return p.id
    return None


def main(argv: "list[str]") -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2

    mode = argv[0]
    port = int(argv[1])
    engine = _mk(port)
    events: "list[tuple[str, dict]]" = []
    engine.events.subscribe(lambda e, p: events.append((e, dict(p))))
    engine.start()
    print(f"[i] instance up on {port}, mode={mode}", flush=True)

    if mode == "listen":
        # Ждём только через pump: без libHandleEvents() пакеты не
        # разбираются (PJSIP поднят с threadCnt=0), и onInstantMessage не
        # выстрелит никогда.
        got = pump(engine, 40, lambda: any(e == "call.incoming" for e, _ in events))
        if not got:
            print("[!] входящий вызов не получен", flush=True)
            engine.stop()
            return 1
        print("[+] входящий вызов получен", flush=True)
        pump(engine, 10, lambda: _confirmed(events))
        if not _confirmed(events):
            print("[!] CONFIRMED не достигнут", flush=True)
            engine.stop()
            return 1
        print("[+] CONFIRMED (входящий)", flush=True)
        pump(engine, 20, lambda: TEXT in _incoming_texts(events))
        heard = _incoming_texts(events)
        if TEXT in heard:
            print(f"[+] чат принят: {TEXT!r}", flush=True)
            engine.stop()
            return 0
        print(f"[!] услышано {heard!r}, ожидалось {TEXT!r}", flush=True)
        engine.stop()
        return 1

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15094
        uri = f"sip:{target_port}@127.0.0.1:{target_port}"
        cid = engine.call(uri)
        print(f"[i] called {uri}, id={cid}", flush=True)
        pump(engine, 25, lambda: _confirmed(events))
        if not _confirmed(events):
            print("[!] вызов не подтверждён", flush=True)
            engine.stop()
            return 1
        print("[+] CONFIRMED (исходящий)", flush=True)
        pid = _confirmed_pid(engine)
        if pid is None:
            print("[!] не найден подтверждённый участник", flush=True)
            engine.stop()
            return 1
        if not engine.send_message(pid, TEXT):
            print("[!] send_message вернул False", flush=True)
            engine.stop()
            return 1
        print(f"[i] отправлено в вызов {pid}: {TEXT!r}", flush=True)
        # Статус доставки — приятный бонус, но не условие успеха: решает то,
        # что текст приняла вторая сторона.
        pump(engine, 8, lambda: bool(engine.chat_history) and
             engine.chat_history[-1].status == "delivered")
        hist = engine.chat_history
        print(f"[i] статус доставки: {hist[-1].status if hist else '?'}", flush=True)
        # Дать стеку дозаписать статус и не умереть на полупакетах.
        pump(engine, 2, lambda: False)
        engine.stop()
        return 0

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    # os._exit: не даём интерпретатору разрушать pjsua2 при выходе — иначе
    # деструкторы _Call дёргают pjsua_call_set_user_data на уже разрушенном
    # Endpoint (см. two_instance_call.py).
    import os as _os

    _code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _os._exit(_code)
