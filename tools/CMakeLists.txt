cmake_minimum_required(VERSION 3.20)
project(kikakuka_model_generators LANGUAGES CXX)

find_package(OpenCASCADE REQUIRED)

add_executable(
    generate-kicad-coupler-models
    generate-kicad-coupler-models.cpp
)
target_compile_features(generate-kicad-coupler-models PRIVATE cxx_std_17)
target_include_directories(
    generate-kicad-coupler-models PRIVATE ${OpenCASCADE_INCLUDE_DIR}
)
target_link_directories(
    generate-kicad-coupler-models PRIVATE ${OpenCASCADE_LIBRARY_DIR}
)
target_link_libraries(
    generate-kicad-coupler-models PRIVATE ${OpenCASCADE_LIBRARIES}
)
