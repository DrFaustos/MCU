// Минимальный JSON для протокола IPC mcu_h323d.
//
// Зачем свой: тащить nlohmann/json в хост не хочется (сборка одной
// команды, никаких submodules). Протокол простой — плоские объекты со
// строковыми/целочисленными значениями, поэтому пишем только то, что нужно:
//   * сериализация объекта {"k":"v","n":123}
//   * разбор строки в map<string,string> + отдельно целые по запросу
//
// Экранирование строк делаем полное (RFC 8259), чтобы alias/IP с кавычками
// или не-ASCII не ломали строку.
#pragma once

#include <cctype>
#include <cstdint>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace mcu_json {

// --- экранирование строки в JSON ---------------------------------------------
inline std::string escape(const std::string &s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

// --- сборка объекта из пар ---------------------------------------------------
// Все значения трактуются как строки, кроме пар, добавленных через add_int.
class Builder {
 public:
  Builder &add(const std::string &key, const std::string &value) {
    parts_.push_back("\"" + escape(key) + "\":\"" + escape(value) + "\"");
    return *this;
  }
  Builder &add_int(const std::string &key, long long value) {
    parts_.push_back("\"" + escape(key) + "\":" + std::to_string(value));
    return *this;
  }
  std::string str() const {
    std::string out = "{";
    for (size_t i = 0; i < parts_.size(); ++i) {
      if (i) out += ",";
      out += parts_[i];
    }
    out += "}";
    return out;
  }

 private:
  std::vector<std::string> parts_;
};

// --- разбор плоского объекта в map<string,string> ----------------------------
// Возвращает false при синтаксической ошибке. Значения-числа и true/false
// остаются в виде исходного текста ("123", "true") — вызывающий код
// парсит их сам при необходимости.
inline bool parse_flat(const std::string &text, std::map<std::string, std::string> &out) {
  out.clear();
  size_t i = 0;
  const size_t n = text.size();

  auto skip_ws = [&]() {
    while (i < n && std::isspace(static_cast<unsigned char>(text[i]))) ++i;
  };

  skip_ws();
  if (i >= n || text[i] != '{') return false;
  ++i;
  skip_ws();
  if (i < n && text[i] == '}') return true;  // пустой объект

  while (i < n) {
    skip_ws();
    if (i >= n || text[i] != '"') return false;
    ++i;

    // ключ
    std::string key;
    while (i < n && text[i] != '"') {
      if (text[i] == '\\' && i + 1 < n) {
        ++i;
        switch (text[i]) {
          case 'n': key += '\n'; break;
          case 't': key += '\t'; break;
          case 'r': key += '\r'; break;
          default: key += text[i]; break;
        }
      } else {
        key += text[i];
      }
      ++i;
    }
    if (i >= n) return false;
    ++i;  // закрывающая кавычка ключа

    skip_ws();
    if (i >= n || text[i] != ':') return false;
    ++i;
    skip_ws();
    if (i >= n) return false;

    std::string value;
    if (text[i] == '"') {
      ++i;
      while (i < n && text[i] != '"') {
        if (text[i] == '\\' && i + 1 < n) {
          ++i;
          switch (text[i]) {
            case 'n': value += '\n'; break;
            case 't': value += '\t'; break;
            case 'r': value += '\r'; break;
            case '"': value += '"'; break;
            case '\\': value += '\\'; break;
            default: value += text[i]; break;
          }
        } else {
          value += text[i];
        }
        ++i;
      }
      if (i >= n) return false;
      ++i;  // закрывающая кавычка значения
    } else {
      // число / true / false / null — до запятой или }
      while (i < n && text[i] != ',' && text[i] != '}') {
        value += text[i];
        ++i;
      }
      // тримим хвостовые пробелы
      while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) {
        value.pop_back();
      }
    }
    out[key] = value;

    skip_ws();
    if (i < n && text[i] == ',') {
      ++i;
      continue;
    }
    if (i < n && text[i] == '}') return true;
    return false;
  }
  return false;
}

}  // namespace mcu_json
