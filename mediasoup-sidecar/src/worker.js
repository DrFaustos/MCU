/**
 * Пул mediasoup worker'ов.
 *
 * mediasoup-worker — это C++ процесс, который делает всю медиа-работу
 * (RTP, ICE, DTLS, SRTP, симулкаст). Один worker эффективно использует одно
 * ядро, поэтому на многопроцессорной машине поднимаем несколько и раскидываем
 * комнаты между ними (round-robin).
 *
 * Роутер (router) создаётся на комнату и живёт ВНУТРИ одного worker'а —
 * перекинуть роутер между worker'ами нельзя (только pipeToRouter, что не
 * нужно на одной машине для базового сценария).
 */

import mediasoup from 'mediasoup';
import os from 'node:os';
import { config } from './config.js';
import { logger } from './logger.js';

let workers = [];
let nextWorker = 0;

//: Уровни, которые принимает C++ mediasoup-worker. 'info' и прочее недопустимы:
//: воркер завершается с кодом 42 «wrong settings» и пул не поднимается.
const WORKER_LOG_LEVELS = new Set(['debug', 'warn', 'error', 'none']);

function workerLogLevel(raw) {
  const value = String(raw || '').toLowerCase();
  if (WORKER_LOG_LEVELS.has(value)) return value;
  // Наш логгер понимает 'info', воркер — нет. Ближайший аналог — 'warn'.
  logger.warn(`Уровень лога '${raw}' недопустим для mediasoup-worker, использую 'warn'`);
  return 'warn';
}

/** Сколько worker'ов поднимать: env или по числу ядер (но не больше 4). */
function desiredWorkerCount() {
  if (config.workers > 0) return config.workers;
  const cores = os.cpus()?.length || 1;
  return Math.max(1, Math.min(cores, 4));
}

export async function startWorkers() {
  const count = desiredWorkerCount();
  const { rtcMinPort, rtcMaxPort } = config;
  const logLevel = workerLogLevel(config.logLevel);

  for (let i = 0; i < count; i += 1) {
    const worker = await mediasoup.createWorker({
      logLevel,
      rtcMinPort,
      rtcMaxPort,
      // ВАЖНО: libuv внутри mediasoup-worker пытается использовать io_uring,
      // который в части окружений (контейнеры, старые/урезанные ядра) падает
      // с "io_uring_enter(getevents): Bad file descriptor" и worker аварийно
      // завершается. Штатный флаг mediasoup отключает io_uring и переходит на
      // обычный epoll. Без него worker не стартует вообще.
      disableLiburing: true,
    });

    // Падение worker'а — критично: без него комната мертва. Пишем и выходим,
    // чтобы Python-супервизор перезапустил sidecar целиком.
    worker.on('died', (error) => {
      logger.error(`mediasoup worker ${worker.pid} умер — завершаю sidecar`, {
        message: String(error),
      });
      setTimeout(() => process.exit(1), 100);
    });

    workers.push(worker);
    logger.info(`mediasoup worker запущен pid=${worker.pid}`);
  }

  logger.info(`Пул worker'ов: ${workers.length} (rtc ${rtcMinPort}-${rtcMaxPort})`);
  return workers;
}

/** Следующий worker по кругу (round-robin). */
export function pickWorker() {
  if (workers.length === 0) throw new Error('Нет запущенных mediasoup worker');
  const worker = workers[nextWorker % workers.length];
  nextWorker += 1;
  return worker;
}

export function workerStats() {
  return workers.map((w) => ({ pid: w.pid, closed: w.closed }));
}

export async function shutdownWorkers() {
  // ВАЖНО: worker.close() в mediasoup — СИНХРОННЫЙ (возвращает void),
  // поэтому .catch() у него нет: вызов падал с TypeError.
  for (const w of workers) {
    try {
      w.close();
    } catch {
      /* worker уже мёртв */
    }
  }
  workers = [];
  nextWorker = 0;
}
