#include "tether/bluetooth/config.hpp"
#include "tether/log.hpp"
#include "tether/paths.hpp"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <unistd.h>

namespace tether::bluetooth {

    namespace {
        // 2 since 0.2.24 how to tell "never had a retention setting" from "lost one".
        constexpr int CONFIG_VERSION = 2;
    } // namespace

    const char* to_string(AuthStrategy strategy) {
        return strategy == AuthStrategy::ExplicitPair ? "explicit-pair" : "connect-first";
    }

    AuthStrategy auth_strategy_from_string(const std::string& value) {
        return value == "explicit-pair" ? AuthStrategy::ExplicitPair : AuthStrategy::ConnectFirst;
    }

    const char* to_string(PauseMode mode) {
        switch (mode) {
        case PauseMode::Never:
            return "never";
        case PauseMode::OneRemoved:
            return "one-removed";
        case PauseMode::BothRemoved:
            return "both-removed";
        }
        return "never";
    }

    PauseMode pause_mode_from_string(const std::string& value) {
        if (value == "one-removed")
            return PauseMode::OneRemoved;
        if (value == "both-removed")
            return PauseMode::BothRemoved;
        return PauseMode::Never;
    }

    const char* to_string(PhoneAudio choice) {
        switch (choice) {
        case PhoneAudio::Auto:
            return "auto";
        case PhoneAudio::Here:
            return "here";
        case PhoneAudio::Phone:
            return "phone";
        }
        return "auto";
    }

    PhoneAudio phone_audio_from_string(const std::string& value) {
        if (value == "here")
            return PhoneAudio::Here;
        if (value == "phone")
            return PhoneAudio::Phone;
        return PhoneAudio::Auto;
    }

    namespace {
        bool env_on(const char* name) {
            const char* value = std::getenv(name);
            return value && *value && std::string(value) != "0";
        }
    } // namespace

    // What a config with nothing saved means. TETHER_NO_ROOT turns call control on by default (it reads
    // PipeWire's telephony when BlueZ has none, which is all a user-level install can reach); TETHER_CALLS=0
    // turns it back off. TETHER_RETENTION picks how the message journal is kept until the user chooses.
    Config default_config() {
        Config config;
        if (env_on("TETHER_NO_ROOT")) {
            const char* calls = std::getenv("TETHER_CALLS");
            config.calls_enabled = !(calls && std::string(calls) == "0");
        }
        if (const char* retention = std::getenv("TETHER_RETENTION"); retention && *retention)
            config.retention = retention_from_string(retention);
        return config;
    }

    std::string config_path() {
        const std::filesystem::path dir = paths::config_dir();
        if (dir.empty())
            return {};
        return (dir / "bluetooth.json").string();
    }

    std::string serialize_config(const Config& config) {
        nlohmann::json j;
        j["config_version"] = CONFIG_VERSION;
        j["device_address"] = config.device_address;
        j["auth_strategy"] = to_string(config.auth_strategy);
        j["ancs_enabled"] = config.ancs_enabled;
        j["ancs_content_enabled"] = config.ancs_content_enabled;
        j["group_messages_enabled"] = config.group_messages_enabled;
        j["calls_enabled"] = config.calls_enabled;
        j["enabled"] = config.enabled;
        j["adapter"] = config.adapter;
        j["retention"] = to_string(config.retention);
        j["desktop_popups_enabled"] = config.desktop_popups_enabled;
        j["popup_previews_enabled"] = config.popup_previews_enabled;
        j["clipboard_auto_share"] = config.clipboard_auto_share;
        j["hide_unknown_senders"] = config.hide_unknown_senders;
        j["airpods_enabled"] = config.airpods_enabled;
        j["airpods_pause"] = to_string(config.airpods_pause);
        j["airpods_handoff"] = config.airpods_handoff;
        j["lock_on_away"] = config.lock_on_away;
        j["lock_away_seconds"] = config.lock_away_seconds;
        j["lock_command"] = config.lock_command;
        j["yield_when_inactive"] = config.yield_when_inactive;
        j["yield_idle_seconds"] = config.yield_idle_seconds;
        j["contacts_by_last_name"] = config.contacts_by_last_name;
        j["phone_audio"] = to_string(config.phone_audio);
        return j.dump(2);
    }

    Config deserialize_config(const std::string& text) {
        Config config = default_config();
        try {
            auto j = nlohmann::json::parse(text);
            if (!j.is_object())
                return config;
            config.device_address = j.value("device_address", "");
            config.auth_strategy = auth_strategy_from_string(j.value("auth_strategy", ""));
            config.ancs_enabled = j.value("ancs_enabled", true);
            const int version = j.value("config_version", 0);
            config.ancs_content_enabled = version < 1 ? true : j.value("ancs_content_enabled", true);
            config.group_messages_enabled = j.value("group_messages_enabled", false);
            config.calls_enabled = j.value("calls_enabled", config.calls_enabled);
            config.enabled = j.value("enabled", true);
            config.adapter = j.value("adapter", "");
            config.retention = j.contains("retention") ? retention_from_string(j.value("retention", "encrypted")) : config.retention;
            config.desktop_popups_enabled = j.value("desktop_popups_enabled", true);
            config.popup_previews_enabled = j.value("popup_previews_enabled", true);
            config.clipboard_auto_share = j.value("clipboard_auto_share", true);
            config.hide_unknown_senders = j.value("hide_unknown_senders", false);
            config.airpods_enabled = j.value("airpods_enabled", false);
            config.airpods_pause = pause_mode_from_string(j.value("airpods_pause", ""));
            config.airpods_handoff = j.value("airpods_handoff", false);
            config.lock_on_away = j.value("lock_on_away", false);
            config.yield_when_inactive = j.value("yield_when_inactive", false);
            // A short timeout would hand the phone away during a pause in typing.
            config.yield_idle_seconds =
                std::max(MIN_YIELD_IDLE_SECONDS, j.value("yield_idle_seconds", YIELD_IDLE_SECONDS));
            // A zero or negative grace would lock on the first flap.
            config.lock_away_seconds = std::max(1, j.value("lock_away_seconds", AWAY_LOCK_GRACE_SECONDS));
            config.lock_command = j.value("lock_command", "");
            config.contacts_by_last_name = j.value("contacts_by_last_name", false);
            config.phone_audio = phone_audio_from_string(j.value("phone_audio", ""));
        } catch (const std::exception&) {
            // A corrupt file must not stop the daemon; defaults are safe.
        }
        return config;
    }

    std::string supervised_address(const Config& config) {
        return config.enabled ? config.device_address : std::string{};
    }

    Config load_config() {
        std::string path = config_path();
        if (path.empty())
            return default_config();
        std::ifstream in(path);
        if (!in.is_open())
            return default_config();
        std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        return deserialize_config(text);
    }

    bool save_config(const Config& config) {
        std::string path = config_path();
        if (path.empty())
            return false;

        // Owner-only from creation, fsynced, then renamed over the old file: a crash cannot leave a truncated config
        if (!paths::write_private_file(path, serialize_config(config))) {
            debug::log(ERR, "bluetooth: cannot write {}", path);
            return false;
        }
        return true;
    }

} // namespace tether::bluetooth
