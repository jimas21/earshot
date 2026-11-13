#pragma once

#include <filesystem>
#include <gio/gio.h>
#include <string>

namespace tether::bluetooth {

    // obexd removes a transfer object as soon as it finishes, so an object that
    // has disappeared is a normal terminal state rather than a failure.
    // Unknown is only returned when a TransferWatch was given: the object vanished and no signal said how it ended.
    enum class TransferState { Active, Complete, Error, Gone, Unknown };

    // Catches a transfer's final Status from PropertiesChanged, which polling usually misses because obexd drops
    // the object the moment it settles. Create it before the call that starts the transfer, on the thread that will
    // wait: signals are delivered to a context private to this object, which wait_for_transfer pumps.
    class TransferWatch {
    public:
        explicit TransferWatch(GDBusConnection* bus);
        ~TransferWatch();
        TransferWatch(const TransferWatch&) = delete;
        TransferWatch& operator=(const TransferWatch&) = delete;

        // Delivers whatever signals have arrived.
        void pump();
        // The last Status seen for a transfer path, or empty.
        std::string status(const std::string& path) const;

        struct Impl;

    private:
        Impl* impl_;
    };

    // Polls a transfer to completion. Returns Active only when the timeout ran
    // out with the transfer still in progress. With a watch, a transfer that
    // vanishes without a recorded final status is Unknown rather than Complete.
    TransferState wait_for_transfer(GDBusConnection* bus,
                                    const std::string& path,
                                    int timeout_seconds,
                                    TransferWatch* watch = nullptr);

    // Where to stage a file obexd reads or writes.
    std::filesystem::path obex_staging_path(const std::string& name);

} // namespace tether::bluetooth
