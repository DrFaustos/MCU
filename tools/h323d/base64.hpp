// Base64 для PCM поверх текстового IPC (ipc.hpp носит newline-delimited
// JSON, бинарные кадры туда иначе как в base64 не положить).
//
// Только то, что нужно мосту: encode(uint8_t*, len) и decode(string).
// Стандартное основание (+, /), с '='-паддингом на выходе; на входе '='
// обрывает, прочие посторонние символы пропускаются (JSON-escape уже снят
// парсером, переносов строк тут быть не должно — но страховка дешёвая).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace mcu_b64 {

inline std::string encode(const uint8_t *data, size_t len) {
  static const char *T =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  out.reserve((len + 2) / 3 * 4);
  size_t i = 0;
  while (i + 2 < len) {
    unsigned v = (unsigned(data[i]) << 16) | (unsigned(data[i + 1]) << 8) |
                 unsigned(data[i + 2]);
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += T[(v >> 6) & 63];
    out += T[v & 63];
    i += 3;
  }
  if (i + 1 == len) {
    unsigned v = unsigned(data[i]) << 16;
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += "==";
  } else if (i + 2 == len) {
    unsigned v = (unsigned(data[i]) << 16) | (unsigned(data[i + 1]) << 8);
    out += T[(v >> 18) & 63];
    out += T[(v >> 12) & 63];
    out += T[(v >> 6) & 63];
    out += '=';
  }
  return out;
}

inline std::vector<uint8_t> decode(const std::string &s) {
  auto val = [](char c) -> int {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
  };
  std::vector<uint8_t> out;
  out.reserve(s.size() / 4 * 3 + 3);
  int acc = 0, bits = 0;
  for (char c : s) {
    if (c == '=') break;
    int v = val(c);
    if (v < 0) continue;
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<uint8_t>((acc >> bits) & 0xff));
    }
  }
  return out;
}

}  // namespace mcu_b64
