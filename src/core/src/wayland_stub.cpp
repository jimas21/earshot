// Wayland for a build without wayland-client (TETHER_WAYLAND=OFF): no clipboard, no idle check.
#include "tether/wayland.hpp"

namespace tether {

    WaylandContext* g_wayland = nullptr;

    std::vector<std::string> wayland_sockets(const std::filesystem::path&) {
        return {};
    }

    bool wayland_idle() {
        return false;
    }

} // namespace tether
