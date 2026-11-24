#!/usr/bin/env bash
# Самодиагностика virtterm: запуск с профилем железа, чтение `ready`, проверка
# таблицы возможностей и корректный shutdown. Нужен контейнер mcu-dev-h323
# (там собран бинарь); из pytest запускается как skip-тест без контейнера.
#
#   ./scripts/testbed/hw_emu/selftest_virtterm.sh [old-polycom]
#
# RC=0 — ready получено, term_profile/faststart/tunneling соответствуют
# профилю; RC=1 — иное.
set -u
PROFILE="${1:-generic}"
SOCK="${VT_SOCK:-/tmp/virtterm-selftest.sock}"
PORT="${VT_PORT:-1721}"
BIN="${VT_BIN:-tools/virtterm/build/virtterm}"

rm -f "$SOCK"
"$BIN" --socket "$SOCK" --port "$PORT" --profile "$PROFILE" \
      --no-auto-answer --tx-pattern burst >/tmp/virtterm-selftest.log 2>&1 &
VPID=$!
trap 'kill $VPID 2>/dev/null || true' EXIT

python3 - "$SOCK" "$PROFILE" <<'PYEOF'
import json, socket, sys, time

sock_path, profile = sys.argv[1], sys.argv[2]
deadline = time.time() + 15
s = socket.socket(socket.AF_UNIX)
while time.time() < deadline:
    try:
        s.connect(sock_path)
        break
    except OSError:
        time.sleep(0.3)
else:
    sys.exit("virtterm не поднял IPC-сокет")

s.settimeout(10)
buf = b""
ready = None
while ready is None:
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
    for line in buf.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "ready":
            ready = ev
            break
if ready is None:
    sys.exit(f"нет ready в потоке: {buf[:200]!r}")
print("ready:", json.dumps(ready, ensure_ascii=False))
ok = True
if ready.get("term_profile") != profile:
    print(f"FAIL: term_profile={ready.get('term_profile')} != {profile}")
    ok = False
EXPECT = {
    "generic": ("1", "1"), "old-polycom": ("1", "1"), "sony": ("1", "1"),
    "tandem-old": ("0", "0"), "narrowband": ("1", "1"),
}
fs, tn = EXPECT[profile]
if ready.get("faststart") != fs:
    print(f"FAIL: faststart={ready.get('faststart')} ожидали {fs}")
    ok = False
if ready.get("tunneling") != tn:
    print(f"FAIL: tunneling={ready.get('tunneling')} ожидаем {tn}")
    ok = False
if not ready.get("audio_codecs"):
    print("FAIL: пустой audio_codecs (плагины на месте?)")
    ok = False
s.sendall(b'{"cmd":"shutdown"}\n')
s.close()
sys.exit(0 if ok else 1)
PYEOF
rc=$?
wait "$VPID" 2>/dev/null
exit $rc
