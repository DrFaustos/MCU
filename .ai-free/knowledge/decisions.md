
## DTMF в MCU (решение)

* Аппаратные ВКС/телефоны передают номер зала и PIN **только** DTMF (RFC 2833
  telephone-event), поэтому в МСУ добавлены приём и отправка тонов.
* API: `mcuclient/dtmf.py` (чистая логика: `normalize_digits`, `validate_digits`,
  `DtmfEvent`, `DtmfHistory`) + `mcuclient/dtmf_service.py` (`DtmfService`, DI:
  pj_module, is_available, get_participant, list_participant_ids, find_by_call).
  SipEngine — фасад: `send_dtmf(digits, participant_id=None, method='auto')`,
  `dtmf_history`.
* Метод `auto`: `sendDtmf(RFC2833)` → при ошибке `sendDtmf(INFO)` → без констант
  метода `dialDtmf`. Только один метод = interop-проблема: старый шлюз не слышит
  RFC2833, часть Polycom не слышит INFO.
* Константы метода — по ИМЕНИ: `PJSUA_DTMF_METHOD_RFC2833` / `..._SIP_INFO`.
* События: `dtmf.digits` (in/out), `dtmf.error`. HTTP: `GET/POST /api/dtmf`.

## Грабль: property vs method в фейках

`SipEngine.chat_history` — @property, а web_server вызывал `engine.chat_history()`
→ GET /api/chat отдавал 500. Фейки движка в ~12 тестах объявляли **метод**, поэтому
баг жил только в бою. Правило: фейк обязан повторять ФОРМУ API (property vs method,
тип поля), иначе тесты врут. То же с `UaConfig.stunServer` (vector, не str).
<!-- source: agent -->
