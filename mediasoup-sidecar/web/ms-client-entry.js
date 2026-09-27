// Точка входа бандла mediasoup-client для web-панели MCU.
//
// esbuild собирает это в один файл (без CDN и без Node в рантайме),
// который подключается тегом <script> и кладёт конструктор в window.mediasoupClient.
//
// Так страница остаётся самодостаточной (один файл, offline-контур),
// а версия клиента фиксируется в package.json сайдкара.
import * as mediasoupClient from 'mediasoup-client';

window.mediasoupClient = mediasoupClient;
