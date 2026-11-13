#include "tether/bluetooth/removed_threads.hpp"
#include "tether/bluetooth/bmessage.hpp"
#include "tether/log.hpp"
#include "tether/paths.hpp"

#include <algorithm>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <unistd.h>

namespace tether::bluetooth {

    namespace {

        // Write-then-rename at mode 0600, fsynced before the rename.
        bool write_private(const std::string& path, const std::string& body) {
            std::error_code ec;
            std::filesystem::create_directories(std::filesystem::path(path).parent_path(), ec);

            const std::string tmp = path + ".tmp";
            {
                std::ofstream out(tmp, std::ios::trunc);
                if (!out.is_open()) {
                    debug::log(ERR, "bluetooth: cannot write {}", tmp);
                    return false;
                }
                out << body;
                if (!out) {
                    debug::log(ERR, "bluetooth: failed writing {}", tmp);
                    return false;
                }
            }
            std::filesystem::permissions(tmp,
                                         std::filesystem::perms::owner_read | std::filesystem::perms::owner_write,
                                         std::filesystem::perm_options::replace,
                                         ec);
            if (const int fd = ::open(tmp.c_str(), O_RDONLY); fd >= 0) {
                ::fsync(fd);
                ::close(fd);
            }
            std::filesystem::rename(tmp, path, ec);
            if (ec) {
                debug::log(ERR, "bluetooth: cannot replace {}: {}", path, ec.message());
                std::filesystem::remove(tmp, ec);
                return false;
            }
            return true;
        }

        bool read_all(const std::string& path, std::string& out) {
            std::ifstream in(path);
            if (!in.is_open())
                return false;
            out.assign((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
            return true;
        }

    } // namespace

    void RemovedThreads::remove(const std::string& thread_key, int64_t cutoff) {
        if (thread_key.empty())
            return;
        auto [at, fresh] = cutoffs_.emplace(thread_bucket(thread_key), cutoff);
        if (!fresh)
            at->second = std::max(at->second, cutoff);
    }

    bool RemovedThreads::hides(const Message& message) const {
        if (cutoffs_.empty() || message.thread_key.empty())
            return false;
        auto at = cutoffs_.find(thread_bucket(message.thread_key));
        return at != cutoffs_.end() && message.timestamp <= at->second;
    }

    std::string removed_threads_path(Retention mode) {
        const std::filesystem::path dir = paths::data_dir();
        if (dir.empty() || mode == Retention::None)
            return {};
        return (dir / (mode == Retention::Encrypted ? "removed_threads.json.enc" : "removed_threads.json")).string();
    }

    std::string serialize_removed_threads(const RemovedThreads& removed) {
        nlohmann::json j;
        j["removed"] = removed.cutoffs();
        return secret::seal(j.dump(-1, ' ', false, nlohmann::json::error_handler_t::replace));
    }

    RemovedThreads deserialize_removed_threads(const std::string& text) {
        RemovedThreads removed;
        std::string plain;
        if (!secret::unseal(text, plain))
            return removed;
        try {
            auto j = nlohmann::json::parse(plain);
            if (!j.is_object() || !j.contains("removed") || !j["removed"].is_object())
                return removed;
            for (const auto& [key, cutoff] : j["removed"].items()) {
                if (cutoff.is_number_integer())
                    removed.remove(key, cutoff.get<int64_t>());
            }
        } catch (const std::exception&) {
            return RemovedThreads();
        }
        return removed;
    }

    bool save_removed_threads(const RemovedThreads& removed) {
        const std::string path = removed_threads_path(secret::retention());
        if (path.empty())
            return false;
        const std::string body = serialize_removed_threads(removed);
        return !body.empty() && write_private(path, body);
    }

    RemovedThreads load_removed_threads() {
        const Retention mode = secret::retention();
        if (mode == Retention::None || !secret::have_key())
            return {};

        // No-op unless a file written under the other mode is still there.
        migrate_removed_threads(mode == Retention::Encrypted ? Retention::Plaintext : Retention::Encrypted, mode);

        std::string text;
        if (!read_all(removed_threads_path(mode), text))
            return {};
        return deserialize_removed_threads(text);
    }

    bool migrate_removed_threads(Retention from, Retention to) {
        const std::string source = removed_threads_path(from);
        const std::string destination = removed_threads_path(to);
        if (source.empty() || destination.empty() || source == destination)
            return false;
        if (!std::filesystem::exists(source))
            return false;

        // A destination already there means an earlier migration was interrupted
        // between the rename and the unlink.
        std::error_code ec;
        if (std::filesystem::exists(destination)) {
            std::filesystem::remove(source, ec);
            return !ec;
        }

        std::string text;
        std::string plain;
        if (!read_all(source, text) || !secret::unseal(text, plain, from)) {
            debug::log(WARN, "bluetooth: cannot read {} to migrate it; leaving it in place", source);
            return false;
        }
        const std::string sealed = secret::seal(plain, to);
        if (sealed.empty() || !write_private(destination, sealed))
            return false;
        std::filesystem::remove(source, ec);
        return true;
    }

    RemovedThreads& removed_threads() {
        static RemovedThreads removed;
        return removed;
    }

} // namespace tether::bluetooth
