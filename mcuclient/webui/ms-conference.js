'use strict';
//
// mediasoup-путь web-конференции (симулкаст, масштаб).
//
// Это ДОПОЛНЕНИЕ к базовому aiortc-пути в index.html: страница сама решает,
// какой SFU использовать. Файл подключается после основного скрипта, поэтому
// видит его глобальные api()/me/status/renderTiles и т.п.
//
// Требует собранный mediasoup-client.js (window.mediasoupClient) и включённый
// features.web.mediasoup на сервере (иначе /api/mediasoup/* -> 503).

let _msDevice = null;
let _msSend = null;
let _msRecv = null;
let _msEnabled = false;
let _msProducers = [];          // {producerId, kind}
let _msConsumers = {};          // producerId -> {consumer, kind}
let _msRemoteStreams = {};      // participantId -> MediaStream

function msSupported() {
  return !!window.mediasoupClient && !!me;
}

async function msAvailable() {
  try {
    const st = await api('/mediasoup');
    return !!(st && st.available);
  } catch (e) {
    return false;
  }
}

// Создать send/recv транспорты поверх серверного WebRtcTransport.
async function msEnsureTransports() {
  if (_msSend && _msRecv) return;
  const j = await api('/mediasoup/join', 'POST', { participant: me.id });
  if (!_msDevice) {
    const dev = new window.mediasoupClient.Device();
    await dev.load({ routerRtpCapabilities: j.rtpCapabilities });
    _msDevice = dev;
  }
  const tp = j.transport;
  const onConnect = ({ dtlsParameters }, cb, errb) => {
    api('/mediasoup/signal', 'POST', {
      action: 'connect', participant: me.id, dtlsParameters,
    }).then(cb).catch(errb);
  };
  const onProduce = ({ kind, rtpParameters }, cb, errb) => {
    api('/mediasoup/signal', 'POST', {
      action: 'produce', participant: me.id, kind, rtpParameters,
    }).then(r => cb({ id: r.producerId })).catch(errb);
  };
  if (!_msSend) {
    _msSend = _msDevice.createSendTransport({ id: tp.id, iceParameters: tp.iceParameters,
      iceCandidates: tp.iceCandidates, dtlsParameters: tp.dtlsParameters, sctpParameters: tp.sctpParameters });
    _msSend.on('connect', onConnect);
    _msSend.on('produce', onProduce);
  }
  if (!_msRecv) {
    _msRecv = _msDevice.createRecvTransport({ id: tp.id, iceParameters: tp.iceParameters,
      iceCandidates: tp.iceCandidates, dtlsParameters: tp.dtlsParameters, sctpParameters: tp.sctpParameters });
    _msRecv.on('connect', onConnect);
  }
}

// Публикация своей камеры/микрофона в mediasoup.
async function msPublish() {
  if (!msSupported()) { $('rtcHint').textContent = 'mediasoup-client не загружен'; return; }
  await msEnsureTransports();
  const stream = _localStream || await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  _localStream = stream;
  $('localVideo').srcObject = stream;
  for (const track of stream.getTracks()) {
    const prod = await _msSend.produce({ track });
    _msProducers.push(prod);
  }
  $('rtcHint').textContent = 'mediasoup: публикация идёт (' + _msProducers.length + ' трек(а))';
  $('btnPublish').disabled = true;
  $('btnUnpublish').disabled = false;
}

async function msConsume(producerId, kind) {
  if (_msConsumers[producerId]) return;
  await msEnsureTransports();
  const caps = _msDevice.rtpCapabilities;
  const r = await api('/mediasoup/signal', 'POST', {
    action: 'consume', participant: me.id, producerId, rtpCapabilities: caps,
  });
  const consumer = await _msRecv.consume({ id: r.consumerId, producerId,
    kind: r.kind || kind, rtpParameters: r.rtpParameters });
  _msConsumers[producerId] = { consumer, kind };
  await consumer.resume();
  const peerId = _msProducerOwner(producerId);
  if (kind === 'video' && peerId) {
    const v = document.querySelector(`.tile[data-peer="${CSS.escape(peerId)}"] video`);
    if (v) v.srcObject = new MediaStream([consumer.track]);
  } else if (kind === 'audio') {
    let a = document.querySelector(`audio[data-ms="${CSS.escape(producerId)}"]`);
    if (!a) { a = document.createElement('audio'); a.autoplay = true; a.dataset.ms = producerId; document.body.appendChild(a); }
    a.srcObject = new MediaStream([consumer.track]);
  }
}

let _msProducerMap = {};  // producerId -> participantId

function _msProducerOwner(producerId) {
  return _msProducerMap[producerId] || null;
}

// Подписаться на всех, кого ещё не смотрим, и запомнить владельцев.
async function msSync() {
  if (!_msEnabled || !me) return;
  try {
    const r = await api('/mediasoup/signal', 'POST', { action: 'producers', participant: me.id });
    for (const p of (r.producers || [])) {
      _msProducerMap[p.producerId] = p.participantId;
      if (!_msConsumers[p.producerId]) await msConsume(p.producerId, p.kind);
    }
  } catch (e) { /* не критично */ }
}

// Включить/выключить mediasoup-режим (вместо aiortc-пути).
async function msToggle(on) {
  if (on) {
    if (!msSupported()) { alert('mediasoup-client не загружен'); return; }
    _msEnabled = true;
    await msPublish();
    await msSync();
    if (!window._msSyncTimer) window._msSyncTimer = setInterval(msSync, 4000);
  } else {
    _msEnabled = false;
    if (window._msSyncTimer) { clearInterval(window._msSyncTimer); window._msSyncTimer = null; }
    _msProducers.forEach(p => { try { p.close(); } catch (e) {} });
    _msProducers = [];
    try { if (_msSend) _msSend.close(); } catch (e) {}
    try { if (_msRecv) _msRecv.close(); } catch (e) {}
    _msSend = null; _msRecv = null;
    Object.values(_msConsumers).forEach(c => { try { c.consumer.close(); } catch (e) {} });
    _msConsumers = {};
    _msProducerMap = {};
    await rtcUnpublish();
  }
  $('rtcHint').textContent = 'SFU: ' + (on ? 'mediasoup' : 'aiortc');
}

window.msConference = { toggle: msToggle, sync: msSync, available: msAvailable };
