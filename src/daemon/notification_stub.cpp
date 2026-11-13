// Desktop notifications for a build without libnotify (TETHER_LIBNOTIFY=OFF).
// init() reports false, so the daemon logs once that popups are unavailable and
// carries on; everything that would have been a popup is still sent to clients.
#include "notification.hpp"

namespace tether {

    struct DesktopNotifier::Impl {};

    DesktopNotifier::DesktopNotifier() = default;
    DesktopNotifier::~DesktopNotifier() = default;

    bool DesktopNotifier::init() {
        return false;
    }

    void DesktopNotifier::set_copy_handler(std::function<void(const std::string&, const std::string&)>) {}
    void DesktopNotifier::set_reply_handler(std::function<void(const std::string&, const std::string&)>) {}
    void DesktopNotifier::notify_file_arrived(const std::filesystem::path&) {}
    void DesktopNotifier::notify(const NotificationSpec&) {}
    void DesktopNotifier::withdraw(uint32_t) {}

} // namespace tether
