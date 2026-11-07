cmake_minimum_required(VERSION 3.22)

project(OotrRemakeNative LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(BFRASS_SOURCE_DIR "" CACHE PATH "Pinned BfrAss source tree")
set(BFRASS_ASSIMP_SOURCE_DIR "" CACHE PATH "Pinned Assimp source tree")

if(NOT EXISTS "${BFRASS_SOURCE_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "BfrAss sources not found at ${BFRASS_SOURCE_DIR}")
endif()
if(NOT EXISTS "${BFRASS_ASSIMP_SOURCE_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "Assimp sources not found at ${BFRASS_ASSIMP_SOURCE_DIR}")
endif()

set(BFRASS_BUILD_CLI ON CACHE BOOL "" FORCE)
add_subdirectory("${BFRASS_SOURCE_DIR}" bfrass)

add_library(ootr_native SHARED bridge.cpp)
target_link_libraries(ootr_native PRIVATE bfrass_core)
target_compile_definitions(ootr_native PRIVATE OOTR_NATIVE_EXPORTS)

set_target_properties(
    ootr_native
    PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}"
)
set_target_properties(
    bfrass-cli
    PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}"
)

if(MSVC)
    target_compile_options(ootr_native PRIVATE /W4 /permissive-)
else()
    target_compile_options(ootr_native PRIVATE -Wall -Wextra)
endif()
