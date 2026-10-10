/**
 * Комната mediasoup: router + транспорты + producer'ы/consumer'ы.
 *
 * Роли транспортов:
 *  • WebRtcTransport — сюда подключается браузер (mediasoup-client);
 *  • PlainTransport  — сюда Python льёт RTP от pjsua2 (SIP/H.323-участник)
 *    и отсюда же забирает RTP для него.
 *
 * Симулкаст: браузер-публикатор при produce(encodings=[3 слоя]) создаёт
 * несколько spatial/temporal слоёв. Каждый consumer сам выбирает слой через
 * setPreferredLayers — это и есть масштабируемость mediasoup.
 */

import { config } from './config.js';
import { logger } from './logger.js';
import { pickWorker } from './worker.js';

let nextRoomId = 1;

/** Медиа-кодеки, которые разрешаем. Порядок = приоритет. */
const mediaCodecs = [
  // Видео: VP8 — основной для симулкаста в браузерах (Chrome/Firefox).
  { kind: 'video', mimeType: 'video/VP8', clockRate: 90000, parameters: {} },
  // H264 — для Safari и аппаратных терминалов (если дойдём до моста).
  {
    kind: 'video',
    mimeType: 'video/H264',
    clockRate: 90000,
    parameters: {
      'packetization-mode': 1,
      'profile-level-id': '42e01f',
      'level-asymmetry-allowed': 1,
    },
  },
  // Аудио: Opus — стандарт WebRTC.
  { kind: 'audio', mimeType: 'audio/opus', clockRate: 48000, channels: 2, parameters: {} },
  // Аудио: G.711 µ-law — кодек RTP-моста. Python льёт в PlainTransport ровно
  // PCMU 8 кГц моно (mcuclient/mediasoup_rtp_bridge.PLAIN_RTP_PARAMETERS), а
  // роутер принимает только заявленное здесь: без этой строки produce_plain
  // отбивается 400 «unsupported codec [mimeType:audio/PCMU, payloadType:0]»
  // и SIP-терминал в браузерах не слышно вовсе. Сверяется тестом
  // tests/test_mediasoup_rtp_bridge.py::test_router_media_codecs_advertise_the_bridge_codec
  { kind: 'audio', mimeType: 'audio/PCMU', clockRate: 8000, channels: 1, parameters: {} },
];

export class Room {
  constructor() {
    this.id = `room-${nextRoomId++}`;
    this.router = null;
    this.transports = new Map(); // transportId -> transport
    this.producers = new Map(); // producerId -> producer
    this.consumers = new Map(); // consumerId -> consumer
    this.closed = false;
  }

  async init() {
    const worker = pickWorker();
    this.router = await worker.createRouter({ mediaCodecs });
    logger.info(`Комната ${this.id}: router создан`);
    return this;
  }

  rtpCapabilities() {
    return this.router.rtpCapabilities;
  }

  /** WebRtcTransport для браузера. */
  async createWebRtcTransport({ enablingUdp = true, enablingTcp = true } = {}) {
    const { listenIp, announcedIp } = config;
    const transport = await this.router.createWebRtcTransport({
      listenInfos: [
        { protocol: 'udp', ip: listenIp, announcedAddress: announcedIp || undefined },
        { protocol: 'tcp', ip: listenIp, announcedAddress: announcedIp || undefined },
      ],
      enableUdp: enablingUdp,
      enableTcp: enablingTcp,
      preferUdp: true,
      initialAvailableOutgoingBitrate: 1_000_000,
    });
    transport.on('dtlsstatechange', (state) => {
      if (state === 'closed') transport.close();
    });
    this.transports.set(transport.id, transport);
    return transport;
  }

  /** PlainTransport — RTP-мост к pjsua2 (SIP/H.323-участник). */
  async createPlainTransport({ rtcpMux = true, comedia = false } = {}) {
    const { listenIp } = config;
    const transport = await this.router.createPlainTransport({
      listenIp,
      rtcpMux,
      comedia,
      enableSrtp: false,
    });
    this.transports.set(transport.id, transport);
    logger.info(`Комната ${this.id}: PlainTransport ${transport.id}`);
    return transport;
  }

  getTransport(id) {
    const t = this.transports.get(id);
    if (!t) throw new Error(`Транспорт ${id} не найден в комнате ${this.id}`);
    return t;
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    for (const c of this.consumers.values()) {
      try { c.close(); } catch { /* уже закрыт */ }
    }
    for (const p of this.producers.values()) {
      try { p.close(); } catch { /* уже закрыт */ }
    }
    for (const t of this.transports.values()) {
      try { t.close(); } catch { /* уже закрыт */ }
    }
    this.consumers.clear();
    this.producers.clear();
    this.transports.clear();
    try { this.router?.close(); } catch { /* уже закрыт */ }
    logger.info(`Комната ${this.id}: закрыта`);
  }

  stats() {
    return {
      id: this.id,
      transports: this.transports.size,
      producers: this.producers.size,
      consumers: this.consumers.size,
      closed: this.closed,
    };
  }
}

export const ROOM_MEDIA_CODECS = mediaCodecs;
