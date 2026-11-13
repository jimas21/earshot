add_library(tether STATIC
    src/core.cpp
    src/i18n.cpp
    src/event_loop.cpp
    src/net.cpp
    src/base64.cpp
    src/paths.cpp
    src/otp.cpp
    src/file_transfer.cpp
    src/crypto.cpp
    src/secret_store.cpp
    src/client.cpp
    src/extension_host.cpp
    src/service.cpp
    src/discovery_group.cpp
    src/mpris.cpp
    src/audio.cpp
    src/bluetooth/objects.cpp
    src/bluetooth/monitor.cpp
    src/bluetooth/config.cpp
    src/bluetooth/agent.cpp
    src/bluetooth/advert.cpp
    src/bluetooth/lan_info.cpp
    src/bluetooth/pairing.cpp
    src/bluetooth/bearer_supervisor.cpp
    src/bluetooth/profile_supervisor.cpp
    src/bluetooth/vcard.cpp
    src/bluetooth/bmessage.cpp
    src/bluetooth/messages.cpp
    src/bluetooth/map_session.cpp
    src/bluetooth/contacts.cpp
    src/bluetooth/removed_threads.cpp
    src/bluetooth/journal.cpp
    src/bluetooth/pbap_session.cpp
    src/bluetooth/phone_audio.cpp
    src/bluetooth/obex_transfer.cpp
    src/bluetooth/ancs/parser.cpp
    src/bluetooth/ancs/notifications.cpp
    src/bluetooth/ancs/sequencer.cpp
    src/bluetooth/ancs/client.cpp
    src/bluetooth/groups.cpp
    src/bluetooth/telephony.cpp
    src/bluetooth/telephony_pipewire.cpp
    src/bluetooth/airpods.cpp
    src/bluetooth/connection.cpp
    src/bluetooth/diagnostics.cpp
    src/session_lock.cpp
)

if(TETHER_MDNS)
    target_sources(tether PRIVATE src/discovery.cpp)
else()
    target_sources(tether PRIVATE src/discovery_stub.cpp)
endif()

if(TETHER_WAYLAND)
    target_sources(tether PRIVATE
        src/wayland.cpp
        src/wayland_protocols/wayland.cpp
        src/wayland_protocols/wlr-data-control-unstable-v1.cpp
        src/wayland_protocols/ext-data-control-v1.cpp
        src/wayland_protocols/ext-idle-notify-v1.cpp
    )
else()
    target_sources(tether PRIVATE src/wayland_stub.cpp)
    target_compile_definitions(tether PUBLIC TETHER_NO_WAYLAND)
endif()

pkg_check_modules(GLIB REQUIRED IMPORTED_TARGET glib-2.0 gio-2.0)

target_include_directories(tether PUBLIC
    include
    ${CMAKE_BINARY_DIR}/generated
)

# Where the bluetooth.service drop-in is shipped, so --bt-setup can name it.
target_compile_definitions(tether PRIVATE
    TETHER_DATADIR="${CMAKE_INSTALL_FULL_DATADIR}/tether"
    TETHER_LOCALEDIR="${CMAKE_INSTALL_FULL_LOCALEDIR}"
    TETHER_GETTEXT_DOMAIN="tether"
)


target_link_libraries(tether PUBLIC
    nlohmann_json::nlohmann_json
    OpenSSL::SSL
    OpenSSL::Crypto
    PkgConfig::GLIB
    Intl::Intl
)
if(TETHER_WAYLAND)
    target_link_libraries(tether PUBLIC PkgConfig::WAYLAND)
endif()
if(TETHER_MDNS)
    target_link_libraries(tether PUBLIC PkgConfig::AVAHI)
endif()
if(TETHER_LIBSECRET)
    target_link_libraries(tether PUBLIC PkgConfig::SECRET)
    target_compile_definitions(tether PRIVATE TETHER_LIBSECRET)
endif()
