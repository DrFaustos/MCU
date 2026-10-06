
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
