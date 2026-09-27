/**
 * Конфигурация mediasoup-sidecar.
 *
 * Всё берётся из переменных окружения (их выставляет Python-приложение
 * при запуске дочернего процесса) со значениями по умолчанию.
 *
 * Важно: rtcMinPort/rtcMaxPort — диапазон UDP-портов медиа. Должен совпадать
 * с пробросом портов в контейнере/фаерволе, иначе ICE не соберётся.
 */

function envInt(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) ? value : fallback;
}

function envStr(name, fallback) {
  const raw = process.env[name];
  return raw === undefined || raw === '' ? fallback : raw;
}

function envList(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  return raw.split(',').map((s) => s.trim()).filter(Boolean);
}

export const config = {
  // HTTP control API (им пользуется Python).
  httpHost: envStr('MCU_MEDIASOUP_HOST', '127.0.0.1'),
  httpPort: envInt('MCU_MEDIASOUP_PORT', 4443),
  // Общий токен: и Python, и sidecar знают его из env. Пусто = без токена.
  authToken: envStr('MCU_MEDIASOUP_TOKEN', ''),

  // Сколько C++ worker'ов поднять. 0 = по числу ядер.
  workers: envInt('MCU_MEDIASOUP_WORKERS', 0),

  // Медиа-порты WebRTC (UDP). Диапазон должен быть открыт.
  rtcMinPort: envInt('MCU_MEDIASOUP_RTC_MIN', 40000),
  rtcMaxPort: envInt('MCU_MEDIASOUP_RTC_MAX', 40100),

  // IP, который mediasoup объявляет в ICE-кандидатах.
  // 127.0.0.1 — только локально; для LAN/интернета задайте внешний IP.
  listenIp: envStr('MCU_MEDIASOUP_LISTEN_IP', '0.0.0.0'),
  announcedIp: envStr('MCU_MEDIASOUP_ANNOUNCED_IP', ''),

  // Включать ли лог в stderr (Python перенаправляет в mcu-client.log).
  logLevel: envStr('MEDIASOUP_LOG_LEVEL', 'warn'),
  logTags: envList('MEDIASOUP_LOG_TAGS', []),

  // Лимит комнат на worker (защита от переполнения).
  maxRoomsPerWorker: envInt('MCU_MEDIASOUP_MAX_ROOMS', 200),
};
