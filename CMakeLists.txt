cmake_minimum_required(VERSION 3.21)
project(CodexBar LANGUAGES CXX)

find_package(Qt6 REQUIRED COMPONENTS Core Qml)
set(CMAKE_AUTOMOC ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(codexbarprocess MODULE
    native/commandrunner.cpp
    native/commandrunner.h
    native/plugin.cpp
)
target_link_libraries(codexbarprocess PRIVATE Qt6::Core Qt6::Qml)

# Refresh the complete package on every build, including removed QML/scripts.
# Keep the compiled plugin outside staging so refreshing cannot delete it.
set(package_dir "${CMAKE_BINARY_DIR}/package")
add_custom_target(package ALL
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${package_dir}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${package_dir}"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_SOURCE_DIR}/metadata.json"
        "${CMAKE_SOURCE_DIR}/LICENSE"
        "${CMAKE_SOURCE_DIR}/NOTICE"
        "${package_dir}/"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_SOURCE_DIR}/contents" "${package_dir}/contents"
    COMMAND ${CMAKE_COMMAND} -E rm -rf
        "${package_dir}/contents/scripts/__pycache__"
    COMMAND ${CMAKE_COMMAND} -E copy
        "$<TARGET_FILE:codexbarprocess>" "${package_dir}/contents/ui/process/"
    DEPENDS codexbarprocess
    VERBATIM
)

option(CODEXBAR_BUILD_TESTS "Build the native command-runner regression check" OFF)
if(CODEXBAR_BUILD_TESTS)
    enable_testing()
    add_executable(commandrunner_test
        native/commandrunner_test.cpp
        native/commandrunner.cpp
        native/commandrunner.h
    )
    target_link_libraries(commandrunner_test PRIVATE Qt6::Core)
    add_test(NAME commandrunner COMMAND commandrunner_test)
endif()
