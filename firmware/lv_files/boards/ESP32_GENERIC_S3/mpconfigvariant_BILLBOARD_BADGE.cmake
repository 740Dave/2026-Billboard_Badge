set(SDKCONFIG_DEFAULTS
    ${SDKCONFIG_DEFAULTS}
    boards/sdkconfig.240mhz
    boards/sdkconfig.spiram_oct
)

list(APPEND MICROPY_DEF_BOARD
    MICROPY_HW_BOARD_NAME="Billboard Badge Generic ESP32S3 module with Octal-SPIRAM"
)

# list(APPEND USER_C_MODULES ${CMAKE_CURRENT_LIST_DIR}/../../../../../user_c_modules/badge_utils/micropython.cmake)