#include "tether/discovery.hpp"

#include <algorithm>
#include <map>

namespace tether {

    // ─── Grouping helper ────────────────────────────────────────────

    std::vector<DiscoveredDevice> group_discovered_hosts(const std::vector<DiscoveredHost>& hosts,
                                                         const std::string& exclude_fingerprint) {
        // Use fingerprint as the grouping key.  Maintain insertion order.
        std::vector<DiscoveredDevice> devices;
        std::map<std::string, size_t> fp_index; // fingerprint -> index into devices

        for (const auto& h : hosts) {
            if (!exclude_fingerprint.empty() && h.fingerprint == exclude_fingerprint)
                continue;

            std::string key = h.fingerprint.empty() ? ("__nokey__" + h.name) : h.fingerprint;

            auto it = fp_index.find(key);
            if (it == fp_index.end()) {
                fp_index[key] = devices.size();
                DiscoveredDevice dev;
                dev.name = h.name;
                dev.fingerprint = h.fingerprint;
                dev.addresses.push_back({h.address, h.port});
                devices.push_back(std::move(dev));
            } else {
                devices[it->second].addresses.push_back({h.address, h.port});
            }
        }

        // Sort addresses within each device: IPv4 first (no ':'), then IPv6
        for (auto& dev : devices) {
            std::stable_sort(
                dev.addresses.begin(), dev.addresses.end(), [](const DiscoveredAddress& a, const DiscoveredAddress& b) {
                    bool a_v4 = a.address.find(':') == std::string::npos;
                    bool b_v4 = b.address.find(':') == std::string::npos;
                    return a_v4 > b_v4; // true (v4) sorts before false (v6)
                });
        }

        return devices;
    }

} // namespace tether
