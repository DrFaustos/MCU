# Makefile для mcu_h323d — нативного H.323-хоста MCU (ADR-0002, Вариант B).
#
# Требует собранный PTLib + H323Plus. Если они в нестандартном PREFIX:
#   make PREFIX=/opt/h323plus
#
# Цели:
#   make        — собрать ./mcu_h323d
#   make clean  — удалить артефакты
#   make check  — проверить, что H323Plus виден (заголовки/библиотека)
#
# Источник — единственный main.cpp; ipc.hpp и json.hpp header-only и
# подключаются из него. Основной путь сборки — CMake (CMakeLists.txt),
# этот Makefile оставлен как лёгкая альтернатива без cmake.

PREFIX ?= /usr/local
CXX    ?= g++
CXXFLAGS ?= -O2 -g -Wall -Wextra -std=c++17

# PTLib и H323Plus ставят .pc-файлы по-разному; H323Plus (форк willamowius)
# .pc не ставит, поэтому пути задаём явно и добавляем найденные через pkg-config.
PTLIB_CFLAGS := $(shell pkg-config --cflags ptlib 2>/dev/null)
PTLIB_LIBS   := $(shell pkg-config --libs ptlib 2>/dev/null)

INCLUDES := -I. -I$(PREFIX)/include/openh323 $(PTLIB_CFLAGS)
LDFLAGS  := -L$(PREFIX)/lib -Wl,-rpath,$(PREFIX)/lib
LDLIBS   := -lh323_linux_x86_64_ $(PTLIB_LIBS) -lpthread

TARGET := mcu_h323d
SRCS   := main.cpp
OBJS   := $(SRCS:.cpp=.o)

.PHONY: all clean check

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) $(OBJS) -o $@ $(LDFLAGS) $(LDLIBS)

%.o: %.cpp ipc.hpp json.hpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

check:
	@echo "PREFIX=$(PREFIX)"
	@echo "ptlib cflags: $(PTLIB_CFLAGS)"
	@test -f $(PREFIX)/include/openh323/h323.h \
		&& echo "h323.h: OK" || (echo "h323.h: НЕ НАЙДЕН — соберите scripts/install_h323plus.sh"; exit 1)
	@ls $(PREFIX)/lib/libh323* >/dev/null 2>&1 \
		&& echo "libh323: OK" || (echo "libh323: НЕ НАЙДЕНА"; exit 1)

clean:
	rm -f $(TARGET) $(OBJS)
