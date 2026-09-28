cmake_minimum_required(VERSION 3.20)
project(zen_cef_candidate LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "The Zen CEF candidate is Linux-only")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

find_package(CEF 151.3.24 EXACT REQUIRED)

add_executable(zen-cef-shell main.cc)
target_link_libraries(zen-cef-shell PRIVATE CEF::Wrapper CEF::Library)
target_compile_options(
  zen-cef-shell
  PRIVATE
    -O2
    -fPIE
    -fstack-protector-strong
    -fno-delete-null-pointer-checks
    -fno-strict-overflow
    -Wall
    -Wextra
    -Werror
    -Wformat=2
    -Wconversion
    -Wsign-conversion
)
target_compile_definitions(
  zen-cef-shell
  PRIVATE
    _FORTIFY_SOURCE=3
)
target_link_options(
  zen-cef-shell
  PRIVATE
    -pie
    -Wl,-s
    -Wl,-z,relro
    -Wl,-z,now
    -Wl,-z,noexecstack
    -Wl,-z,defs
    -Wl,--as-needed
    -Wl,--build-id=sha1
)
set_target_properties(
  zen-cef-shell
  PROPERTIES
    BUILD_RPATH "$ORIGIN"
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_RPATH "$ORIGIN"
)
