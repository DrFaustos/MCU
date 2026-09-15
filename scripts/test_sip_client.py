#!/usr/bin/env python3
"""Тестовый SIP-клиент для дымовой проверки MCU Client.

Звонит на sip:<room>@<host>:<port> и печатает состояние вызова.
Использует тот же pjsua2, что и MCU Client.

    python scripts/test_sip_client.py 127.0.0.1 5060 mcu
"""

from __future__ import annotations

import sys
import time

import pjsua2 as pj

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = sys.argv[2] if len(sys.argv) > 2 else "5060"
USER = sys.argv[3] if len(sys.argv) > 3 else "mcu"


class Account(pj.Account):
    def onRegState(self, prm):  # noqa: N802
        print(f"[acct] reg state: {prm.code} {prm.reason}")


class Call(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(acc, call_id)
        self.connected = False

    def onCallState(self, prm):  # noqa: N802
        info = self.getInfo()
        print(f"[call] state={info.stateText} code={info.lastStatusCode} "
              f"reason={info.lastReason}")
        if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True

    def onCallMediaState(self, prm):  # noqa: N802
        print("[call] media state changed")


def main() -> int:
    ep = pj.Endpoint()
    cfg = pj.EpConfig()
    cfg.logConfig.level = 3
    cfg.logConfig.consoleLevel = 3
    ep.libCreate()
    ep.libInit(cfg)

    tcfg = pj.TransportConfig()
    tcfg.port = 5070
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
    ep.libStart()

    acc = Account()
    acfg = pj.AccountConfig()
    acfg.idUri = "sip:test@127.0.0.1"
    acc.create(acfg)

    uri = f"sip:{USER}@{HOST}:{PORT}"
    print(f"[test] звоню на {uri}")
    call = Call(acc)
    prm = pj.CallOpParam(True)
    prm.opt.audioCount = 1
    prm.opt.videoCount = 0
    try:
        call.makeCall(uri, prm)
    except Exception as exc:  # noqa: BLE001
        print(f"[test] ошибка makeCall: {exc}")
        ep.libDestroy()
        return 1

    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.5)
        if call.connected:
            break

    if call.connected:
        print("[test] УСПЕХ: вызов установлен (CONFIRMED)")
        hprm = pj.CallOpParam()
        call.hangup(hprm)
        time.sleep(1)
        ep.libDestroy()
        return 0

    print("[test] НЕУДАЧА: вызов не подтверждён")
    ep.libDestroy()
    return 2


if __name__ == "__main__":
    sys.exit(main())
