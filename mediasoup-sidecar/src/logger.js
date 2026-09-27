/**
 * Минимальный логгер sidecar.
 *
 * Пишем в stdout/stderr строками с префиксом [mediasoup]: Python-приложение
 * запускает sidecar как дочерний процесс и перенаправляет его вывод в свой
 * лог-файл, поэтому формат должен быть простым и однострочным.
 */

const LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function currentLevel() {
  const raw = (process.env.MEDIASOUP_LOG_LEVEL || 'warn').toLowerCase();
  return LEVELS[raw] ?? LEVELS.warn;
}

export function log(level, message, extra) {
  if ((LEVELS[level] ?? 99) > currentLevel()) return;
  const line = extra === undefined
    ? `[mediasoup] ${level.toUpperCase()} ${message}`
    : `[mediasoup] ${level.toUpperCase()} ${message} ${JSON.stringify(extra)}`;
  // error/warn — в stderr, остальное — в stdout (как принято в CLI).
  if (level === 'error' || level === 'warn') {
    process.stderr.write(line + '\n');
  } else {
    process.stdout.write(line + '\n');
  }
}

export const logger = {
  error: (m, e) => log('error', m, e),
  warn: (m, e) => log('warn', m, e),
  info: (m, e) => log('info', m, e),
  debug: (m, e) => log('debug', m, e),
};
