
## pjsua2 2.16 API: проверенные грабли (зонд по реальной сборке)

* `UaConfig.stunServer` — **НЕ строка**, а `std::vector<std::string>` (`pjsua2.StringVector`).
  `ua.stunServer = "stun:..."` → `TypeError`. Любая ошибка в `_configure_nat` отменяет **весь**
  блок NAT (STUN/ICE/TURN/`maxCalls`/`natTypeInSdp`) молча, т.к. `_start_pjsip` ловит исключения.
  Пишем через `mcuclient.sip_engine.make_string_vector()`. У `StringVector` нет `assign(list)`
  (`assign()` требует 2 аргумента) — только `append()`/`clear()`.
* TURN-константы в 2.16 называются `PJ_TURN_TP_UDP/TCP/TLS`, а **не** `PJ_TURN_TRANSPORT_*`;
  `PJ_ICE_SESS_TRICKLE_{DISABLED,HALF,FULL}` = 0/1/2. Порядок enum'ов между сборками не
  гарантирован → всегда доставать константу **по имени** (`_pj_enum`), числа не хардкодить.
* `UaConfig` в 2.16 **не имеет** `enableIce`/`turn*`. ICE/TURN живут только в
  `AccountConfig.natConfig` (`iceEnabled`, `iceTrickle`, `turnEnabled/turnServer/...`,
  `contactRewriteMethod`, `udpKaIntervalSec`, `iceManualHost`, `iceNoRtcp`).
  `ua.enableIce = True` — тихий мёртвый Python-атрибут.
* SRTP: `AccountConfig.mediaConfig.srtpUse` (+ `srtpOpt`, `srtpSecureSignaling`).
  `AccountConfig` НЕ имеет srtp/ice/turn на верхнем уровне.
* `CallMediaInfo` полей SRTP-шифрования не имеет (`audioConfSlot, dir, index, status, type,
  videoCapDev, videoIncomingWindowId, videoWindow`); `CallMediaSrtpInfo`/`VideoMediaStream`
  в сборке отсутствуют — статус SRTP из callInfo не прочитать.
* `EpConfig` = `logConfig, medConfig, uaConfig, writeObject` (не `med`/`ua`).
  `uaConfig.maxCalls` существует и это UA-конфиг, не AccountConfig.
* `natConfig.turnServer` ждёт `"HOST:PORT"` **без** схемы: `turn:host:3478?transport=udp`
  проглатывается молча, relay-кандидат не поднимется → `normalize_turn_server()`/
  `normalize_stun_server()`.
<!-- source: agent -->

## DTMF при threadCnt=0: проверенные грабли (зонд + стенд MCU<->MCU)

* `Call.sendDtmf(param)` с `prm.digits="1984#"` и `Call.dialDtmf("1984#")` доносят до
  адресата **ровно один** тон «1». Внутренняя очередь тонов pjsip при `threadCnt=0`
  не разыгрывается. Единственный рабочий способ RFC 4733 — **тон за тоном + накачка
  `libHandleEvents` между тонами** (`DtmfService._send_tones`).
* `engine.process_events(0.16)` — это **НЕ** «спать 160 мс»: `libHandleEvents()`
  возвращается на первом же обработанном пакете. Паузу между тонами надо держать по
  `time.monotonic()` маленькими шагами (`DTMF_PUMP_STEP_SEC=0.02`), иначе следующий
  тон уходит сразу и адресат обрезает предыдущий (теряется символ).
* Рабочий темп (замерен): `duration>=80 мс`, пауза `>=0.10 с`; в коде 120 мс / 0.16 с.
  При duration=60/пауза=0.08 тон теряется.
* **SIP INFO** (`PJSUA_DTMF_METHOD_SIP_INFO`) доставляет всю строку одним
  сообщением — паузы не нужны, дежурный путь для старых шлюзов/SBC.
* Перед накачкой из своего потока обязателен `libRegisterThread` — из «чужого»
  потока `libHandleEvents` = нативный abort без трейсбэка. DtmfService принимает
  `register_thread` и регистрирует поток один раз (по `threading.get_ident()`).
* Флаги `OnDtmfEventParam`: 0 = begin, 1 = repeat (бит MORE), 3 = repeat+end. Один
  тон = ~8 callback'ов; новый тон только когда бит MORE сброшен. `onDtmfDigit`
  приходит на каждый тон с `duration=4294967295` — дедуп с onDtmfEvent обязателен.
* Стенд: `scripts/testbed/run_two_instance_dtmf_test.sh` → `[+] DTMF MCU<->MCU OK`.
  Unit-тесты этого не ловят (они проверяют только вызовы pjsua2, не RTP).
<!-- source: agent -->
