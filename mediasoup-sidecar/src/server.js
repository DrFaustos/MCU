/**
 * HTTP control API mediasoup-sidecar.
 *
 * Python-приложение (System of Record) управляет медиа через этот API:
 * создаёт комнаты, транспорты, producer'ы/consumer'ы. Медиа через этот HTTP
 * НЕ идёт — только управление; сам RTP/WebRTC маршрутизирует mediasoup-worker.
 *
 * Авторизация: заголовок `Authorization: Bearer <MCU_MEDIASOUP_TOKEN>`,
 * если токен задан. Слушаем только 127.0.0.1 по умолчанию (наружу не торчит).
 */

import http from 'node:http';
import { config } from './config.js';
import { logger } from './logger.js';
import { startWorkers, shutdownWorkers, workerStats } from './worker.js';
import { Room } from './room.js';

/** roomId -> Room */
const rooms = new Map();

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(data),
  });
  res.end(data);
}

function authorized(req) {
  if (!config.authToken) return true;
  const header = req.headers['authorization'] || '';
  return header === `Bearer ${config.authToken}`;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) {
        reject(new Error('Тело запроса слишком велико'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!body) return resolve({});
      try {
        resolve(JSON.parse(body));
      } catch (e) {
        reject(new Error(`Некорректный JSON: ${e.message}`));
      }
    });
    req.on('error', reject);
  });
}

function getRoom(roomId) {
  const room = rooms.get(roomId);
  if (!room) throw new Error(`Комната ${roomId} не найдена`);
  return room;
}

// --- обработчики -----------------------------------------------------------

async function handle(method, path, body) {
  // health
  if (method === 'GET' && path === '/health') {
    return { ok: true, workers: workerStats(), rooms: rooms.size };
  }

  // Комнаты
  if (method === 'POST' && path === '/rooms') {
    const room = await new Room().init();
    rooms.set(room.id, room);
    return { ok: true, roomId: room.id, rtpCapabilities: room.rtpCapabilities() };
  }

  if (method === 'GET' && path === '/rooms') {
    return { ok: true, rooms: [...rooms.values()].map((r) => r.stats()) };
  }

  if (method === 'POST' && path === '/rooms/close') {
    const room = getRoom(body.roomId);
    await room.close();
    rooms.delete(room.id);
    return { ok: true };
  }

  // WebRtcTransport (браузер)
  if (method === 'POST' && path === '/transports/webrtc') {
    const room = getRoom(body.roomId);
    const transport = await room.createWebRtcTransport({
      enablingUdp: body.enableUdp !== false,
      enablingTcp: body.enableTcp !== false,
    });
    return {
      ok: true,
      transportId: transport.id,
      iceParameters: transport.iceParameters,
      iceCandidates: transport.iceCandidates,
      dtlsParameters: transport.dtlsParameters,
      sctpParameters: transport.sctpParameters,
    };
  }

  // PlainTransport (RTP-мост к pjsua2)
  if (method === 'POST' && path === '/transports/plain') {
    const room = getRoom(body.roomId);
    const transport = await room.createPlainTransport({
      rtcpMux: body.rtcpMux !== false,
      comedia: body.comedia === true,
    });
    return {
      ok: true,
      transportId: transport.id,
      ip: transport.tuple.localIp,
      port: transport.tuple.localPort,
      rtcpPort: transport.rtcpTuple ? transport.rtcpTuple.localPort : null,
    };
  }

  // connect транспорта (DTLS для WebRTC)
  if (method === 'POST' && path === '/transports/connect') {
    const room = getRoom(body.roomId);
    const transport = room.getTransport(body.transportId);
    await transport.connect({
      dtlsParameters: body.dtlsParameters,
    });
    return { ok: true };
  }

  // produce (браузер публикует трек)
  if (method === 'POST' && path === '/produce') {
    const room = getRoom(body.roomId);
    const transport = room.getTransport(body.transportId);
    const producer = await transport.produce({
      kind: body.kind,
      rtpParameters: body.rtpParameters,
      appData: body.appData || {},
    });
    room.producers.set(producer.id, producer);
    producer.on('transportclose', () => room.producers.delete(producer.id));
    return { ok: true, producerId: producer.id };
  }

  // RTP in: Python шлёт RTP от pjsua2 в PlainTransport
  if (method === 'POST' && path === '/produce/plain') {
    const room = getRoom(body.roomId);
    const transport = room.getTransport(body.transportId);
    const producer = await transport.produce({
      kind: body.kind,
      rtpParameters: body.rtpParameters,
      appData: body.appData || {},
    });
    room.producers.set(producer.id, producer);
    producer.on('transportclose', () => room.producers.delete(producer.id));
    return { ok: true, producerId: producer.id };
  }

  // consume (зритель подписывается на producer)
  if (method === 'POST' && path === '/consume') {
    const room = getRoom(body.roomId);
    const transport = room.getTransport(body.transportId);
    const consumer = await transport.consume({
      producerId: body.producerId,
      rtpCapabilities: body.rtpCapabilities,
      paused: body.paused === true,
    });
    room.consumers.set(consumer.id, consumer);
    return {
      ok: true,
      consumerId: consumer.id,
      producerId: consumer.producerId,
      kind: consumer.kind,
      rtpParameters: consumer.rtpParameters,
      type: consumer.type,
    };
  }

  // Управление слоями симулкаста: зритель выбирает spatial/temporal
  if (method === 'POST' && path === '/consumer/set-layers') {
    const room = getRoom(body.roomId);
    const consumer = room.consumers.get(body.consumerId);
    if (!consumer) throw new Error(`Consumer ${body.consumerId} не найден`);
    await consumer.setPreferredLayers({
      spatialLayer: body.spatialLayer,
      temporalLayer: body.temporalLayer,
    });
    return { ok: true, preferredLayers: consumer.preferredLayers };
  }

  // keyframe для видео (например, после переподключения)
  if (method === 'POST' && path === '/producer/request-keyframe') {
    const room = getRoom(body.roomId);
    const producer = room.producers.get(body.producerId);
    if (!producer) throw new Error(`Producer ${body.producerId} не найден`);
    await producer.requestKeyFrame();
    return { ok: true };
  }

  if (method === 'POST' && path === '/rooms/stats') {
    const room = getRoom(body.roomId);
    return {
      ok: true,
      room: room.stats(),
      producers: [...room.producers.values()].map((p) => ({
        id: p.id, kind: p.kind, paused: p.paused, score: p.score,
      })),
    };
  }

  throw new Error(`Неизвестный маршрут: ${method} ${path}`);
}

// --- HTTP-сервер -----------------------------------------------------------

const server = http.createServer(async (req, res) => {
  try {
    if (!authorized(req)) {
      return send(res, 401, { ok: false, error: 'Требуется авторизация' });
    }
    const url = new URL(req.url, 'http://localhost');
    const body = req.method === 'POST' ? await readJson(req) : {};
    const result = await handle(req.method, url.pathname, body);
    send(res, 200, result);
  } catch (err) {
    logger.error(`API ошибка ${req.method} ${req.url}: ${err.message}`);
    send(res, 400, { ok: false, error: err.message });
  }
});

async function main() {
  await startWorkers();
  server.listen(config.httpPort, config.httpHost, () => {
    logger.info(`Control API: http://${config.httpHost}:${config.httpPort}`);
  });
}

async function shutdown(signal) {
  logger.info(`Получен ${signal}, останавливаюсь`);
  server.close();
  for (const room of rooms.values()) {
    await room.close().catch(() => {});
  }
  await shutdownWorkers().catch(() => {});
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

main().catch((err) => {
  logger.error(`Не удалось запустить sidecar: ${err.message}`, { stack: err.stack });
  process.exit(1);
});
