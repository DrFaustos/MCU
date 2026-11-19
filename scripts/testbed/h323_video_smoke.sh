#!/usr/bin/env bash
# Дымовая проверка видео-транспорта mcu_h323d внутри контейнера: сокет кадров
# поднимается, вызывается по имени, хост живёт при закрытом/умершем клиенте.
set -uo pipefail
# Репо в контейнере = /work; скрипт лежит в scripts/testbed, поэтому вверх ДВА
# уровня (один «..» из scripts/testbed ведёт в scripts, где нет tools/).
cd "$(dirname "$0")/../.."

BIN=tools/h323d/build/mcu_h323d
SOCK=/tmp/mcu_video_smoke.sock
VSOCK=/tmp/mcu_video_smoke.sock.video
rm -f "$SOCK" "$VSOCK"

echo "--- --help (проверяем, что флаги видео в справке) ---"
"$BIN" --help | tail -8

echo "--- запуск хоста ---"
"$BIN" --socket "$SOCK" --port 1720 --no-auto-answer > /tmp/mcu_video_smoke.log 2>&1 &
HOST=$!
rc=1
for _ in $(seq 1 60); do
    [ -S "$SOCK" ] && [ -S "$VSOCK" ] && break
    sleep 0.2
done
grep -n "видео-сокет" /tmp/mcu_video_smoke.log || echo "НЕТ СТРОКИ про видео-сокет в логе"
ls -l "$VSOCK" 2>/dev/null || echo "НЕТ СОКЕТА КАДРОВ"

python3 - "$SOCK" "$VSOCK" <<'PY'
import base64, json, socket, sys, time

sock, vsock = sys.argv[1], sys.argv[2]


def readline(f):
    return json.loads(f.readline().strip() or "{}")


c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
c.settimeout(10)
c.connect(sock)
f = c.makefile("rwb")  # r+ для socket.makefile запрещён (только r/w/b)

ready = readline(f)
print("готовое событие:", ready.get("event"))

# Команды по несуществующему вызову: хост обязан ответить ошибкой, а не упасть.
for cmd in ({"cmd": "video.out", "token": "call-nope", "data": base64.b64encode(b"\x00" * 64).decode(),
             "width": 16, "height": 16},
            {"cmd": "video.out", "token": "call-nope", "data": base64.b64encode(b"\x00" * 64).decode()},
            {"cmd": "video.pattern", "token": "call-nope", "on": "0"}):
    f.write((json.dumps(cmd) + "\n").encode())
    f.flush()
    print(cmd["cmd"], "->", readline(f))

# Бинарный сокет кадров: подключаемся и проверяем, что хост это видит.
v = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
v.settimeout(10)
v.connect(vsock)
print("бинарный сокет кадров: подключился")
time.sleep(0.3)

# Убиваем читателя кадров и оставляем хост живым: закрытый сокет не имеет права
# уронить ни хост, ни аудио-контракт.
v.close()
time.sleep(0.5)
f.write((json.dumps({"cmd": "ping"}) + "\n").encode())
f.flush()
print("ping после смерти видеоклиента:", readline(f).get("event"))
PY
RC=$?

sleep 0.3
kill -TERM $HOST 2>/dev/null
wait $HOST 2>/dev/null
echo "--- лог хоста (хвост) ---"
tail -6 /tmp/mcu_video_smoke.log
echo "--- сокеты после останова (не должно быть) ---"
left=""
for s in "$SOCK" "$VSOCK"; do
    [ -e "$s" ] && left="$left $s"
done
if [ -n "$left" ]; then echo "ОСТАЛИСЬ сокеты:$left"; exit 1; fi
echo "оба сокета сняты"
exit $RC
