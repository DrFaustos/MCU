/**
 * Smoke-тест sidecar без браузера и без Python.
 *
 * Проверяет: worker поднимается, комната создаётся, WebRtcTransport и
 * PlainTransport создаются, RTP-capabilities отдаются. Этого достаточно,
 * чтобы убедиться, что нативный mediasoup-worker собран и работает.
 *
 * Запуск: npm run smoke
 */

import { startWorkers, shutdownWorkers } from '../src/worker.js';
import { Room } from '../src/room.js';
import { logger } from '../src/logger.js';

async function main() {
  logger.info('Smoke: запуск worker\'ов');
  await startWorkers();

  logger.info('Smoke: создание комнаты');
  const room = await new Room().init();
  const caps = room.rtpCapabilities();
  if (!caps || !Array.isArray(caps.codecs) || caps.codecs.length === 0) {
    throw new Error('rtpCapabilities пуст — worker не собрал кодеки');
  }
  logger.info(`Smoke: кодеки — ${caps.codecs.map((c) => c.mimeType).join(', ')}`);

  logger.info('Smoke: WebRtcTransport');
  const web = await room.createWebRtcTransport();
  if (!web.iceParameters || !web.dtlsParameters) {
    throw new Error('WebRtcTransport без ICE/DTLS параметров');
  }
  logger.info(`Smoke: ICE-кандидатов — ${web.iceCandidates.length}`);

  logger.info('Smoke: PlainTransport (RTP-мост)');
  const plain = await room.createPlainTransport();
  logger.info(`Smoke: PlainTransport слушает ${plain.tuple.localIp}:${plain.tuple.localPort}`);

  await room.close();
  await shutdownWorkers();
  logger.info('Smoke: OK');
}

main().catch((err) => {
  logger.error(`Smoke провален: ${err.message}`, { stack: err.stack });
  process.exit(1);
});
