#pragma once

#include "tether/bluetooth/ancs/notifications.hpp"

#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

namespace tether::bluetooth::ancs {

    // How long a notification waits for its app's display name before it goes out under the name derived from the
    // bundle id. The phone answers in well under a second when it answers at all.
    inline constexpr int APP_NAME_WAIT_SECONDS = 3;

    enum class NameAction {
        Emit, // show it now
        Hold, // the real name is on its way: wait for it
    };

    // Whether a resolved notification goes out now or waits for its app's display name. The derived name ("Battery" for
    // com.security.BatteryCam) is a guess, and a pop-up that shows it is wrong in a way the user can see, so a
    // notification waits while a lookup for its app is outstanding. With no id, a name already known, or no lookup in
    // flight there is nothing to wait for.
    inline NameAction name_action(bool app_id_empty, bool name_known, bool lookup_pending) {
        if (app_id_empty || name_known)
            return NameAction::Emit;
        return lookup_pending ? NameAction::Hold : NameAction::Emit;
    }

    // Notifications waiting for an app's display name, in arrival order. Nothing is ever dropped: a notification
    // leaves by being released (with the real name, or the derived one when none came in time) or cancelled because
    // the phone removed it first.
    class HeldNotifications {
    public:
        // Holds `notification` for its app. A uid already held is replaced where it stands, so an update keeps the
        // place of the notification it updates.
        void hold(Notification notification, int64_t now) {
            for (auto& entry : entries_) {
                if (entry.notification.uid == notification.uid) {
                    entry.notification = std::move(notification);
                    return;
                }
            }
            entries_.push_back({std::move(notification), now});
        }

        bool holding(const std::string& app_id) const {
            return std::any_of(entries_.begin(), entries_.end(), [&](const Entry& entry) {
                return entry.notification.app_id == app_id;
            });
        }

        // Everything held for `app_id`, oldest first, carrying `name`; the derived name when `name` is empty.
        std::vector<Notification> release(const std::string& app_id, const std::string& name) {
            std::vector<Notification> out;
            auto kept = entries_.begin();
            for (auto it = entries_.begin(); it != entries_.end(); ++it) {
                if (it->notification.app_id == app_id) {
                    out.push_back(std::move(it->notification));
                    out.back().app_name = name.empty() ? derive_app_name(app_id) : name;
                } else {
                    if (kept != it)
                        *kept = std::move(*it);
                    ++kept;
                }
            }
            entries_.erase(kept, entries_.end());
            return out;
        }

        // Releases, with the derived name, every app whose oldest held notification has waited long enough. An app's
        // notifications go together so that none overtakes an earlier one. `apps`, when given, receives those apps.
        std::vector<Notification> release_expired(int64_t now, std::vector<std::string>* apps = nullptr) {
            std::vector<std::string> due;
            for (const auto& entry : entries_) {
                if (now - entry.since >= APP_NAME_WAIT_SECONDS &&
                    std::find(due.begin(), due.end(), entry.notification.app_id) == due.end())
                    due.push_back(entry.notification.app_id);
            }
            std::vector<Notification> out;
            for (const auto& app_id : due) {
                auto released = release(app_id, {});
                out.insert(out.end(), std::make_move_iterator(released.begin()), std::make_move_iterator(released.end()));
            }
            if (apps)
                *apps = std::move(due);
            return out;
        }

        // Everything, with derived names: for a session that is ending, so nothing held is lost.
        std::vector<Notification> release_all() {
            std::vector<Notification> out;
            for (auto& entry : entries_) {
                out.push_back(std::move(entry.notification));
                out.back().app_name = derive_app_name(out.back().app_id);
            }
            entries_.clear();
            return out;
        }

        // The phone removed it before it was ever shown.
        bool cancel(uint32_t uid) {
            auto it = std::find_if(
                entries_.begin(), entries_.end(), [&](const Entry& entry) { return entry.notification.uid == uid; });
            if (it == entries_.end())
                return false;
            entries_.erase(it);
            return true;
        }

        size_t size() const { return entries_.size(); }

    private:
        struct Entry {
            Notification notification;
            int64_t since;
        };
        std::vector<Entry> entries_;
    };

} // namespace tether::bluetooth::ancs
