#pragma once

#include <filesystem>
#include <string>

namespace tether::paths {

    // XDG base directories for tether's own files. Returns an empty path
    // when there is no home directory to fall back on. Nothing is created.
    //
    // A directory named by an XDG variable is used only when the value is
    // absolute, per the spec. When the XDG location does not exist yet but the
    // legacy $HOME one does, the legacy path wins so an existing install
    // doesn't break.

    // $XDG_CONFIG_HOME/tether, else ~/.config/tether.
    std::filesystem::path config_dir();

    // $XDG_DATA_HOME/tether, else ~/.local/share/tether.
    std::filesystem::path data_dir();

    // $XDG_STATE_HOME/tether, else ~/.local/state/tether.
    std::filesystem::path state_dir();

    // Replaces `path` with `content`, readable by the owner alone from the first byte: the temporary file is
    // created O_EXCL with mode 0600, written, fsynced and renamed over the target, and the directory is fsynced
    // so the rename survives a crash. Never leaves a truncated or world-readable file. False on any failure.
    bool write_private_file(const std::filesystem::path& path, const std::string& content);

} // namespace tether::paths
