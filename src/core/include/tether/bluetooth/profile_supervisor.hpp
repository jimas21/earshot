#pragma once

#include <cstdint>
#include <string>

namespace tether::bluetooth {

    // Poll fast until the first session opens, then slowly.
    inline constexpr int PROFILE_INITIAL_POLL_SECONDS = 5;
    inline constexpr int PROFILE_STEADY_POLL_SECONDS = 15;
    // With obexd absent from the session bus, every attempt fails the same way for as long as nobody installs or
    // starts it. Looking this rarely costs nothing; the bus itself says when org.bluez.obex appears.
    inline constexpr int PROFILE_NO_DAEMON_POLL_SECONDS = 600;
    inline constexpr int PROFILE_BUS_WATCH_SECONDS = 5;

    // Why an OBEX session could not be opened. Forbidden and Busy look alike from a distance, and reporting one as the
    // other sends the user off re-pairing for nothing.
    enum class ObexError {
        None,
        // OBEX Forbidden / 0x43: the iPhone's permission toggle is off. Not a pairing failure
        Forbidden,
        // Transport-level "Connection refused (111)": another computer probably
        // owns the iPhone's single MAP session. Keep polling; do not re-pair.
        Busy,
        // obexd's live SDP query found no MAS/PSE record. NOT a permissions failure. Recovers on its own once the phone
        // serves them again.
        NoRecord,
        // The phone is not reachable right now.
        Unavailable,
        // org.bluez.obex is not on the session bus and cannot be activated,
        // meaning obexd is not installed. No phone-side setting affects this.
        NoDaemon,
        Other,
    };

    const char* to_string(ObexError error);

    // Maps an obexd/D-Bus error message onto the cases above. Pure so the
    // classification can be pinned down by tests against real message text.
    ObexError classify_obex_error(const std::string& message);

    // Advice for the user, phrased so it can be shown verbatim. `ertm_disabled`
    // names the local cause of a NoRecord that would otherwise blame the phone.
    std::string obex_error_advice(ObexError error, const std::string& profile, bool ertm_disabled = false);

    // Seconds until the next attempt to open the profiles that are not open. `all_pending_no_daemon` is true when
    // every one of them failed because obexd is not there, which no amount of retrying changes. Pure so the cadence
    // can be pinned down by tests.
    inline int profile_retry_seconds(bool opened_once, bool all_pending_no_daemon) {
        if (all_pending_no_daemon)
            return PROFILE_NO_DAEMON_POLL_SECONDS;
        return opened_once ? PROFILE_STEADY_POLL_SECONDS : PROFILE_INITIAL_POLL_SECONDS;
    }

    // Session operations on obexd.
    class ProfileOps {
    public:
        virtual ~ProfileOps() = default;

        // Whether org.bluez.obex has an owner on the session bus right now. Lets a missing daemon be noticed the
        // moment it starts, without retrying CreateSession. Implementations that cannot tell leave this false.
        virtual bool obex_service_present() { return false; }

        // Returns the new session's object path, or an empty string with err set.
        virtual std::string create_session(const std::string& target, std::string& err) = 0;
        virtual void remove_session(const std::string& path) = 0;
    };

    struct ProfileStatus {
        bool map_open = false;
        bool pbap_open = false;
        ObexError map_error = ObexError::None;
        ObexError pbap_error = ObexError::None;
        // Released on purpose, so another computer can open it.
        bool map_held = false;
        bool pbap_held = false;
        std::string reason;

        bool operator==(const ProfileStatus&) const = default;
    };

    // Keeps exactly one MAP and one PBAP session alive for the daemon's lifetime.
    class ProfileSupervisor {
    public:
        explicit ProfileSupervisor(ProfileOps& ops);

        // Releases both obexd sessions. The iPhone serves one MAP session at a
        // time, so a session abandoned here blocks the next one.
        ~ProfileSupervisor();

        // Drive periodically with a monotonic clock. `device_ready` is a paired
        // device, not a link: obexd opens its own transport.
        bool tick(int64_t now, bool device_ready);

        // Drops both sessions after a disconnect so the next tick reopens them.
        void reset();

        // Drops only the MAP session, for a caller that has found it dead while
        // the link itself is still up. The next tick reopens it.
        void drop_map();

        // A held profile has its session removed and is not reopened until the
        // hold is lifted. `by_inactivity` only changes the reason shown.
        void set_held(bool map, bool pbap, bool by_inactivity);

        const ProfileStatus& status() const { return status_; }
        const std::string& map_session() const { return map_path_; }
        const std::string& pbap_session() const { return pbap_path_; }

    private:
        bool open_profile(
            const std::string& target, std::string& path, ObexError& error, bool& retried, bool& no_daemon_logged);

        ProfileOps& ops_;
        ProfileStatus status_;
        std::string map_path_;
        std::string pbap_path_;
        bool map_retried_ = false;
        bool pbap_retried_ = false;
        // A missing obexd is logged once per daemon run, not once per attempt.
        bool map_no_daemon_logged_ = false;
        bool pbap_no_daemon_logged_ = false;
        // Every profile still to open failed for want of obexd: retries are rare and the bus is watched instead.
        bool waiting_for_obexd_ = false;
        int64_t next_bus_check_ = 0;
        int64_t next_attempt_ = 0;
        bool opened_once_ = false;
        bool held_by_inactivity_ = false;
    };

} // namespace tether::bluetooth
