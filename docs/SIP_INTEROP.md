# Совместимость SIP с аппаратным парком ВКС (interop)

Документ про **тонкую настройку SIP**, от которой зависит, «соединился и
молчит» терминал или работает. Кодек-специфичное — в
[ARCHITECTURE.md](ARCHITECTURE.md) и `docs/VIDEO_STATUS.md`, SRTP/NAT/ICE —
в `docs/AI_CONTEXT.md` и `mcuclient/sip_engine.py`.

## Где жить настройке

`config.json` → `sip.interop`:

```json
"interop": {
  "prack": "off",
  "session_timer": "optional",
  "session_expires_sec": 1800,
  "min_session_expires_sec": 900,
  "hold_type": "rfc3264",
  "rtcp_mux": "off"
}
```

Прошивает всё `SipEngine._configure_account_interop()`
(`mcuclient/sip_engine.py`) — вызывается из `_start_account()` **после**
`_configure_account_nat()`. Строки конфига в enum'ы pjsua2 превращают чистые
функции `prack_use_value()` / `session_timer_value()` / `hold_type_value()`:
они читают константы **по ИМЕНАМ** (`PJSUA_100REL_OPTIONAL`, …), потому что
числовые значения enum'ов между сборками PJSIP не совпадают.

Секция может отсутствовать целиком или быть заполненной частично — в обоих
случаях берётся значение по умолчанию (старые конфиги не ломаются).

## Почему по умолчанию именно так

Принцип один: **новое предлагаем, но не навязываем**. Любое «mandatory» в
смешанном парке = отказ в звонке у части терминалов.

| Ключ | Значения | По умолчанию | Что ломается, если выбрать неверно |
|------|----------|--------------|-------------------------------------|
| `prack` | `off`, `optional`, `mandatory` | `off` | `optional`/`mandatory` — терминалы без 100rel не доживают до 200 OK; на нашем стенде MCU<->MCU включённый 100rel ещё и роняет первый DTMF-тон (см. ниже) |
| `session_timer` | `inactive`, `optional`, `required`, `always` | `optional` | `inactive` — CUCM и ряд SBC рвут сессию через 15-30 минут, заодно протухает NAT-binding |
| `session_expires_sec` | `0` (не предлагать) … `86400` | `1800` | слишком мало — лишний UPDATE-трафик; `min_session_expires_sec` меньше 90 запрещён RFC 4028 |
| `hold_type` | `rfc3264`, `rfc2543` | `rfc3264` | старые Polycom/Sony не понимают hold по RFC 3264 — им ставят `rfc2543` |
| `rtcp_mux` | `off`, `on` | `off` | `on` на старых шлюзах ломает медиа (без RTP при «успешном» звонке) |

## Когда что включать

* **Закрытый контур, только наш софт** — можно `session_timer: required`:
  сессия гарантированно обновляется. С `prack` — см. предупреждение ниже.
* **CUCM / Cisco / «звонок живёт 15 минут»** — оставить
  `session_timer: optional`, уменьшить `session_expires_sec` до 600,
  `min_session_expires_sec` до 90 (RFC 4028 минимум).
* **Старый Polycom/Sony: hold не работает, звук не возвращается** —
  `hold_type: rfc2543`.
* **Мало портов / симметричный NAT** — `rtcp_mux: on`, но проверять на
  реальном терминале: обратимая поломка медиа, а не ошибка конфигурации.
* **`a=update-connection` / UPDATE-метод** — `AccountCallConfig.updateUse` в
  нашей сборке pjsua2 2.16 **отсутствует** (проверено зондом), поэтому не
  настраивается.

## prack: почему по умолчанию `off`, а не `optional`

PRACK меняет порядок обмена 1xx/200 OK. Если **оба** конца на pjsip и 100rel
включён, тоны RFC 4733 приходят со сдвигом: `run_two_instance_dtmf_test.sh`
вместо `1984#` слышал `1184#`/`184#`. На базе без interop-настроек тот же
стенд проходит. Поэтому дефолт = `off` (это же дефолт pjsip), а `optional`
включают точечно, когда нужен гарантированный 183 с early media, и
**обязательно** перепрогоняют DTMF-стенд.

## Грабли, закреплённые тестами (`tests/test_sip_interop.py`)

1. **`mediaConfig.rtcpMuxEnabled` принимает строго `bool`.** Запись `1`/`0`
   бросает `TypeError` в SWIG-обёртке. Пишем только `True`, и только когда
   `rtcp_mux: on` (по умолчанию поле не трогаем вовсе).
2. **Вложенные объекты `AccountConfig` — ссылка, а не копия.**
   `acc_cfg.callConfig.prackUse = …` работает (проверено read-back'ом). Но
   `acc_cfg.stunServer` (uaConfig) при этом **не** строка, а vector —
   см. `docs/AI_CONTEXT.md`.
3. **`Account.create()` без созданного транспорта — assertion в C.**
   `pjsua_acc_add: Assertion 'pjsua_var.tpdata[0].data.ptr != NULL' failed`:
   процесс умирает без исключения. Транспорт создаётся в
   `_configure_transport()` **до** аккаунта — порядок инициализации трогать
   нельзя (см. `tests/test_sip_engine_init_order.py`).
4. **`pj.Account.__init__` принимает только `self`** — конфиг передаётся в
   `create(acc_cfg)`, не в конструктор (в отличие от документации pjsua2
   других сборок).
5. **Отсутствующее поле биндинга ≠ ошибка.** Всё обернуто в `try/except` +
   `hasattr`: урезанная сборка не должна ронять регистрацию.

## Проверка

```sh
python3 -m pytest tests/test_sip_interop.py -q      # 22 passed (2 — на настоящем pjsua2)
scripts/testbed/run_two_instance_test.sh            # звонок MCU<->MCU не сломался
scripts/testbed/run_two_instance_interop_test.sh    # то же, но prack=mandatory + SE=required
scripts/testbed/run_two_instance_dtmf_test.sh       # тоны не должны пропасть
```

`run_two_instance_interop_test.sh` поднимает два процесса со строгим набором
(`prack: mandatory`, `session_timer: required`, `rtcp_mux: on`) и проверяет,
что звонок всё равно доходит до CONFIRMED и в логе нет строк «не применён».
Стенды делят `127.0.0.1` и чувствительны к таймингам — запускать по одному.

В логе старта видна строка: `SIP-interop: prack=optional session_timer=optional
expires=1800s min_se=900s hold=rfc3264 rtcp_mux=off` — по ней проверяем, что
конфиг доехал до стека.


## Регистрация на регистраторе (`sip.registration`) — как попасть в адресную книгу терминала

Всё выше — про то, как *держится* звонок. Но есть вопрос первичнее: как терминал
вообще дозвонится до МСУ. По умолчанию МСУ отвечает только на прямые вызовы по
IP (`sip:MCU-Room@10.0.0.5`). В реальном парке Polycom/Cisco/Sony так не
работают: там набирают **номер зала** из адресной книги, и вызов идёт через
CUCM/АТС. Чтобы МСУ стал таким абонентом, он должен **зарегистрироваться** —
как любой телефон и как RealPresence Desktop/Mobile.

```json
"registration": {
  "enabled": true,
  "registrar": "sip:cucm-1.corp:5060",
  "domain": "vcu.corp",
  "username": "9100",
  "password": "...",
  "expires_sec": 1800,
  "proxies": [],
  "retry_interval_sec": 60
}
```

То же короткими флагами (для проверки с ноутбука без правки конфига):

```
python3 run.py --register sip:voip.corp --reg-user 9100 --reg-domain vcu.corp
# пароль надёжнее передать окружением: MCU_REG_PASSWORD=... (в cmdline его
# видно через ps всем, кто залогинен на машине МСУ)
```

Где что живёт в pjsua2 (проверено зондом по реальной сборке 2.16):

* `acc_cfg.regConfig.registrarUri` / `.timeoutSec` / `.registerOnAdd`;
* креды — `acc_cfg.sipConfig.authCreds`, а **не** `AccountConfig.authCredInfo`;
  у `AuthCredInfo` нет поля `password`, пароль лежит в `data` при
  `dataType = PJSIP_CRED_DATA_PLAIN_PASSWD`;
* outbound-прокси — `acc_cfg.sipConfig.proxies`; в `AccountConfig` полей
  `proxy`/`outbound` нет вообще;
* состояние — `Account.getInfo()`: `regIsActive/regStatus/regLastErr/regExpiresSec`,
  колбэк — `Account.onRegState` (его вешает `RegistrationManager`).

Грабли, из-за которых «включили и не работает»:

* **хост в `idUri` обязан быть доменом, а не IP МСУ.** На CUCM линия ищется как
  `user@domain`; с `sip:9100@10.0.0.5` регистратор отвечает 404/403. Поэтому при
  включённой регистрации `_build_id_uri()` берёт домен (или хост регистратора),
  а без неё — как раньше, IP.
* **`realm='*'`** в креде, а не домен: CUCM часто челленджит realm'ом, не
  совпадающим с доменом, и с жёстким realm получаем 403 без внятной причины.
* **user-часть URI чувствительна к регистру** (RFC 3261 19.1.4), хост — нет.
  Поэтому `normalize_sip_uri()` режет в нижний регистр схему и хост, но не
  пользователя.
* `enabled: true` без `registrar` не проходит валидацию конфига: pjsip иначе
  молча саморегистрируется на хост `idUri` и ловит 403 до бесконечности.
* срок: `expires_sec` меньше 300 рвут некоторые SBC, а без refresh
  (`registerOnAdd`) CUCM снимает линию через expiry секунд — зал перестаёт быть
  набираемым посреди конференции.

Код: `mcuclient/sip_registration.py` (чистые функции + `RegistrationManager`),
прошивка — `SipEngine._start_account()`, статус — `SipEngine.registration` и
`/api/status` → `"registration": {...}`, события — `sip.registration` и
`sip.registration.error` (с человеческой подсказкой: 401 → «проверьте
username/password», 404 → «имя не совпадает с DN», 408 → «сеть/порт»).

Проверка: `python3 -m pytest tests/test_sip_registration.py -q`
(тесты на настоящем pjsua2 помечены `importorskip`, остальные идут на
поддельном модуле с другими числами констант).

Локальный стенд с регистрацией: `scripts/testbed/run_local_sip_testbed.sh`
(Asterisk на 127.0.0.1:15080, абонент `1001`/`test1001`).

> Починено в стенде: `scripts/testbed/asterisk/pjsip.conf` имел у endpoint
> `1001` секции `auth`/`aor` с тем же именем `1001` и без ссылок `auth=`/`aors=`,
> из-за чего REGISTER **всегда** получал 403 (видно в `register_1001_*.log`
> в корне репо с 17 сентября). У Asterisk у endpoint/auth/aor должны быть
> разные имена и явные ссылки.
