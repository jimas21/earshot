// Discovery for a build without Avahi (TETHER_MDNS=OFF): nothing is advertised and
// nothing is found. Wi-Fi pairing is unavailable; Bluetooth is not affected.
#include "tether/discovery.hpp"

namespace tether {

    struct Discovery::Impl {};

    Discovery::Discovery() : impl_(std::make_unique<Impl>()) {}
    Discovery::~Discovery() = default;
    Discovery::Discovery(Discovery&&) noexcept = default;
    Discovery& Discovery::operator=(Discovery&&) noexcept = default;

    void Discovery::set_state_callback(StateCallback) {}

    bool Discovery::publish(const std::string&, uint16_t, const std::string&) {
        return false;
    }

    void Discovery::unpublish() {}

    std::vector<DiscoveredHost> Discovery::discover(int) {
        return {};
    }

    void Discovery::start_continuous_browse(std::function<void(const std::vector<DiscoveredDevice>&)>) {}

    void Discovery::refresh() {}

    void Discovery::stop_continuous_browse() {}

} // namespace tether
