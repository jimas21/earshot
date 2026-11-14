#pragma once

#include "tether/bluetooth/config.hpp"

#include <atomic>
#include <functional>
#include <nlohmann/json.hpp>
#include <string>

namespace tether::bluetooth {

    class BluezMonitor;

    struct PairResult {
        bool success = false;
        // Machine-readable outcome: "paired", "already_paired", "not_found",
        // "rejected", "timeout", "busy", "error".
        std::string status;
        std::string message;
        std::string device_path;
        std::string device_address;
        // True when the resulting bond covers LE as well as BR/EDR.
        bool dual_bond = false;
        // Which transaction actually produced the bond, which is not always the
        // one that was asked for: connect-first falls back to an explicit pair.
        AuthStrategy auth_strategy_used = AuthStrategy::ConnectFirst;
        // The phone started this pairing (listen_for_pair), so no auth strategy of ours produced it.
        bool listened = false;
    };

    // Whether a failed connect-first transaction should be retried as an explicit
    // Device1.Pair(). Connect-first induces authentication only as a side effect of
    // a profile connect, so a phone that refuses the profile never starts pairing
    // at all. Device1.Pair() involves no profile.
    bool should_fall_back(AuthStrategy tried, bool paired, bool confirmation_failed);

    // Whether a finished tether-dialog answered one way or another.
    bool dialog_answered(int wait_status);

    // Whether a D-Bus error names an authentication BlueZ already failed. No bond can appear afterwards.
    bool is_authentication_failure(const std::string& err);

    // Reports progress steps so the CLI and GTK app can show what is happening
    // during a transaction that legitimately takes tens of seconds.
    using ProgressFn = std::function<void(const std::string& step, const std::string& detail)>;

    // Asks the user to confirm a six-digit numeric comparison code. Returns true
    // to accept. Runs on the monitor's GLib thread.
    using ConfirmFn = std::function<bool(const std::string& code)>;

    // Runs the full pairing transaction against `address`, blocking until it
    // resolves. Intended to be called from a worker thread: GDBusConnection is
    // thread-safe for calls, while the agent's callbacks land on the monitor's
    // GLib thread.
    // `calls_enabled` widens the agent's service whitelist to hands-free.
    PairResult pair_device(BluezMonitor& monitor,
                           const std::string& address,
                           AuthStrategy strategy,
                           const ProgressFn& progress,
                           const ConfirmFn& confirm,
                           bool calls_enabled = false);

    // Phone-initiated pairing, which needs no experimental BlueZ and no root: the adapter is made discoverable and
    // pairable for `seconds`, the ANCS solicitation is on air until a phone connects (it goes off for the pairing
    // and returns once the bond is made), and the agent serves whichever phone asks first.
    // The confirmation reaches `confirm` (the client that started the listen). Returns when the phone has bonded,
    // nobody came, the user declined, or `cancel` was set. Progress steps: listening, connecting (detail = the
    // phone's name), confirm, confirmed, paired, settling, soliciting. Status "timeout" means nobody connected.
    // `dual_bond` is true when the bond carries ANCS.
    PairResult listen_for_pair(BluezMonitor& monitor,
                               int seconds,
                               const ProgressFn& progress,
                               const ConfirmFn& confirm,
                               const std::atomic<bool>& cancel,
                               bool calls_enabled = false);

    // What a listen should do with its ANCS solicitation advert on each poll. The advert must not be on air
    // while the phone is pairing over Classic: an iPhone that sees it can bond LE-only, with no Classic half,
    // and then MAP and PBAP never work. So it is up while nobody has shown up (re-armed if BlueZ's own
    // timeout took it down before the listen window ended) and down from the moment a phone connects.
    enum class ListenAdvert {
        Start, // put it on air (again)
        Stop,  // take it off air
        Hold,  // leave it as it is
    };
    ListenAdvert listen_advert_action(bool phone_announced, bool advert_on_air);

    // What to do about the ANCS solicitation advert when something asks for it. The controller may offer a single
    // advertising instance, so a second registration while the supervision's own is up fails with "Failed to register
    // advertisement": the same advert is kept instead of being torn down and re-registered, which BlueZ may not have
    // released yet. A caller that needs a fresh one (the manual solicit, a re-arm after BlueZ dropped it) replaces it.
    enum class AdvertStart {
        Reuse,   // the advert on air is the one wanted: leave it
        Replace, // take down whatever is there and register again
    };
    AdvertStart advert_start_action(bool reuse_if_on_air, bool on_air_on_this_adapter);

    // Whether a listen should take this device's bond as the new one it is waiting for. `target_unpaired_seen`
    // says the target was observed unpaired at some point: a bond that was already there is not news. A
    // bond on any device other than the one the agent is serving never counts.
    bool listen_bond_matches(bool paired,
                             bool was_paired_before,
                             bool is_target,
                             bool target_unpaired_seen,
                             bool confirmed);

    // Removes the bond so a clean pairing can be retried.
    PairResult unpair_device(BluezMonitor& monitor, const std::string& address);

    // Runs a BlueZ discovery for `seconds`, calling `on_tick` about once a second
    // so a caller can publish the growing device list.
    bool scan_devices(BluezMonitor& monitor, int seconds, const std::function<void()>& on_tick, std::string& err);

    // Device1.Connect / Device1.Disconnect on a bonded device, by address.
    bool connect_device(BluezMonitor& monitor, const std::string& address, std::string& err);
    bool disconnect_device(BluezMonitor& monitor, const std::string& address, std::string& err);

    // Puts the ANCS solicitation advertisement back on air without re-pairing.
    // This is what makes iOS reveal its "Show Message Notifications" and
    // "Sync Contacts" toggles, and it expires a few minutes after pairing, so
    // there has to be a way back to it short of removing the bond.
    bool solicit_ancs(BluezMonitor& monitor, std::string& err);

    // Whether that advertisement is on air right now
    bool ancs_solicitation_active();

    // Takes it off air, freeing the LE advertising instance
    void stop_ancs_solicitation(BluezMonitor& monitor);

    // Holds the solicitation on air for exactly as long as `want`
    void supervise_ancs_solicitation(BluezMonitor& monitor, bool want);

    nlohmann::json to_json(const PairResult& result);

    bool confirm_with_dialog(const std::string& device_name, const std::string& code, bool& unavailable);

} // namespace tether::bluetooth
