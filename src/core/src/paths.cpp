#include "tether/paths.hpp"

#include <cerrno>
#include <cstdlib>
#include <fcntl.h>
#include <pwd.h>
#include <unistd.h>

namespace tether::paths {

    namespace {

        std::filesystem::path home_dir() {
            if (const char* home = getenv("HOME"); home && *home)
                return home;
            if (const passwd* pw = getpwuid(getuid()); pw && pw->pw_dir)
                return pw->pw_dir;
            return {};
        }

        bool dir_exists(const std::filesystem::path& path) {
            std::error_code ec;
            return std::filesystem::exists(path, ec);
        }

        std::filesystem::path xdg_dir(const char* variable, const char* fallback) {
            const std::filesystem::path home = home_dir();
            const std::filesystem::path legacy = home.empty() ? std::filesystem::path{} : home / fallback / "tether";

            const char* value = getenv(variable);
            // A relative XDG value is invalid and ignored.
            if (!value || !*value || !std::filesystem::path(value).is_absolute())
                return legacy;

            const std::filesystem::path dir = std::filesystem::path(value) / "tether";
            if (!legacy.empty() && !dir_exists(dir) && dir_exists(legacy))
                return legacy;
            return dir;
        }

    } // namespace

    std::filesystem::path config_dir() { return xdg_dir("XDG_CONFIG_HOME", ".config"); }

    std::filesystem::path data_dir() { return xdg_dir("XDG_DATA_HOME", ".local/share"); }

    std::filesystem::path state_dir() { return xdg_dir("XDG_STATE_HOME", ".local/state"); }

    bool write_private_file(const std::filesystem::path& path, const std::string& content) {
        std::error_code ec;
        std::filesystem::create_directories(path.parent_path(), ec);

        // A name nobody else can guess or pre-create; O_EXCL refuses to follow a planted file or link
        const std::string tmp = path.string() + ".tmp." + std::to_string(getpid());
        unlink(tmp.c_str());
        const int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0)
            return false;

        size_t written = 0;
        bool ok = true;
        while (ok && written < content.size()) {
            const ssize_t n = write(fd, content.data() + written, content.size() - written);
            if (n > 0)
                written += static_cast<size_t>(n);
            else if (n < 0 && errno == EINTR)
                continue;
            else
                ok = false;
        }
        ok = ok && fsync(fd) == 0;
        ok = (close(fd) == 0) && ok;
        if (!ok || rename(tmp.c_str(), path.c_str()) != 0) {
            unlink(tmp.c_str());
            return false;
        }

        // Make the rename itself durable
        const int dir = open(path.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (dir >= 0) {
            fsync(dir);
            close(dir);
        }
        return true;
    }

} // namespace tether::paths
