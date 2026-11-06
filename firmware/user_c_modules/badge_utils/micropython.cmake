add_library(usermod_badge_utils INTERFACE)

target_compile_options(usermod_badge_utils INTERFACE -Wno-array-bounds -Wno-stringop-overflow)

target_sources(usermod_badge_utils INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/epaper.c
)

target_include_directories(usermod_badge_utils INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/../../lv_micropython/user_modules/lv_binding_micropython/lvgl/src/libs/lodepng/
    ${CMAKE_CURRENT_LIST_DIR}/../../lv_micropython/user_modules/lv_binding_micropython/lvgl/
)

target_link_libraries(usermod INTERFACE usermod_badge_utils)