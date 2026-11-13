add_executable(tetherd
    main.cpp
)

pkg_check_modules(GIO_UNIX REQUIRED IMPORTED_TARGET gio-unix-2.0)

target_link_libraries(tetherd PRIVATE tether PkgConfig::GIO_UNIX)

if(TETHER_LIBNOTIFY)
    pkg_check_modules(LIBNOTIFY REQUIRED IMPORTED_TARGET libnotify)
    target_sources(tetherd PRIVATE notification.cpp)
    target_link_libraries(tetherd PRIVATE PkgConfig::LIBNOTIFY)
else()
    target_sources(tetherd PRIVATE notification_stub.cpp)
endif()

if(TETHER_CAMERA)
    pkg_check_modules(CAMERA REQUIRED IMPORTED_TARGET libavcodec libavutil libpipewire-0.3)
    target_sources(tetherd PRIVATE camera.cpp)
    target_compile_definitions(tetherd PRIVATE TETHER_CAMERA)
    target_link_libraries(tetherd PRIVATE PkgConfig::CAMERA)
endif()

# Output into root build dir instead of nested build/src/daemon
set_target_properties(tetherd PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")

install(TARGETS tetherd
        RUNTIME DESTINATION bin)
