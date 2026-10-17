cmake_minimum_required(VERSION 3.22)

project(tsov_host VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# Pin JUCE. 8.0.9 is the last 8.x line used by this project; see ADR-0026.
FetchContent_Declare(JUCE
    GIT_REPOSITORY https://github.com/juce-framework/JUCE.git
    GIT_TAG 8.0.9
    GIT_SHALLOW TRUE)

FetchContent_MakeAvailable(JUCE)

juce_add_console_app(tsov_host
    PRODUCT_NAME "tsov-host"
    VERSION "0.1.0")

target_sources(tsov_host PRIVATE Source/Main.cpp)

target_compile_definitions(tsov_host PRIVATE
    JUCE_WEB_BROWSER=0
    JUCE_USE_CURL=0
    JUCE_DISPLAY_SPLASH_SCREEN=0
    JUCE_PLUGINHOST_VST3=1
    JUCE_PLUGINHOST_VST=0
    JUCE_APPLICATION_NAME_STRING="tsov-host"
    JUCE_APPLICATION_VERSION_STRING="0.1.0")

target_link_libraries(tsov_host PRIVATE
    juce::juce_audio_utils
    juce::juce_audio_processors
    juce::juce_recommended_config_flags
    juce::juce_recommended_lto_flags)

set_target_properties(tsov_host PROPERTIES
    OUTPUT_NAME "tsov-host"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
