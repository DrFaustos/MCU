# mcu_h323d — H.323-хост на H323Plus (ADR-0002, Вариант B).
#
# Сборка:
#   cmake -S tools/h323d -B tools/h323d/build -DCMAKE_BUILD_TYPE=Release
#   cmake --build tools/h323d/build -j
#
# H323Plus в форке willamowius НЕ ставит .pc-файл, а кладёт заголовки в
# <prefix>/include/openh323. Поэтому ищем и include, и библиотеку вручную,
# с явными PATHS (иначе CMake не заглядывает в /usr/local/include/openh323).
cmake_minimum_required(VERSION 3.16)
project(mcu_h323d LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Корень установки H323Plus/PTLib. Можно переопределить: -DH323PLUS_ROOT=/opt/h323plus
set(H323PLUS_ROOT "/usr/local" CACHE PATH "Корень установки H323Plus/PTLib")

# --- include: заголовки H323Plus и PTLib -------------------------------------
find_path(H323PLUS_INCLUDE_DIR
    NAMES h323.h h323ep.h
    PATHS ${H323PLUS_ROOT}
    PATH_SUFFIXES include/openh323 include
    NO_DEFAULT_PATH)

find_path(PTLIB_INCLUDE_DIR
    NAMES ptlib.h
    PATHS ${H323PLUS_ROOT}
    PATH_SUFFIXES include/ptlib include/openh323 include
    NO_DEFAULT_PATH)

# --- библиотеки --------------------------------------------------------------
find_library(H323PLUS_LIBRARY
    NAMES h323_linux_x86_64_ openh323 h323plus h323
    PATHS ${H323PLUS_ROOT}
    PATH_SUFFIXES lib lib64
    NO_DEFAULT_PATH)

find_library(PTLIB_LIBRARY
    NAMES pt
    PATHS ${H323PLUS_ROOT}
    PATH_SUFFIXES lib lib64
    NO_DEFAULT_PATH)

# Fallback: системные пути (если H323Plus поставлен в /usr).
if(NOT H323PLUS_INCLUDE_DIR)
    find_path(H323PLUS_INCLUDE_DIR NAMES h323.h PATH_SUFFIXES openh323)
endif()
if(NOT H323PLUS_LIBRARY)
    find_library(H323PLUS_LIBRARY NAMES h323_linux_x86_64_ openh323 h323plus h323)
endif()
if(NOT PTLIB_LIBRARY)
    find_library(PTLIB_LIBRARY NAMES pt)
endif()

if(NOT H323PLUS_INCLUDE_DIR OR NOT H323PLUS_LIBRARY OR NOT PTLIB_LIBRARY)
    message(FATAL_ERROR
        "Не найдены PTLib/H323Plus. Сначала соберите стек: ./scripts/install_h323plus.sh\n"
        "  H323PLUS_ROOT        = ${H323PLUS_ROOT}\n"
        "  H323PLUS_INCLUDE_DIR = ${H323PLUS_INCLUDE_DIR}\n"
        "  H323PLUS_LIBRARY     = ${H323PLUS_LIBRARY}\n"
        "  PTLIB_LIBRARY        = ${PTLIB_LIBRARY}")
endif()

message(STATUS "H323Plus include: ${H323PLUS_INCLUDE_DIR}")
message(STATUS "H323Plus lib:     ${H323PLUS_LIBRARY}")
message(STATUS "PTLib include:    ${PTLIB_INCLUDE_DIR}")
message(STATUS "PTLib lib:        ${PTLIB_LIBRARY}")

find_package(Threads REQUIRED)

# main.cpp — единая точка входа (H323EndPoint + IPC).
# ipc.hpp / json.hpp — header-only реализации, подключаются из main.cpp.
add_executable(mcu_h323d
    main.cpp
)

target_include_directories(mcu_h323d PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${H323PLUS_INCLUDE_DIR}
)

if(PTLIB_INCLUDE_DIR)
    target_include_directories(mcu_h323d PRIVATE ${PTLIB_INCLUDE_DIR})
endif()

target_link_libraries(mcu_h323d PRIVATE
    ${H323PLUS_LIBRARY}
    ${PTLIB_LIBRARY}
    Threads::Threads
)

install(TARGETS mcu_h323d RUNTIME DESTINATION bin)
