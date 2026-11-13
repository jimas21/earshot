#include "tether/bluetooth/obex_transfer.hpp"
#include "tether/net.hpp"

#include <chrono>
#include <map>
#include <thread>

namespace tether::bluetooth {

    namespace {

        constexpr const char* OBEX_NAME = "org.bluez.obex";
        constexpr const char* IFACE_TRANSFER = "org.bluez.obex.Transfer1";
        constexpr const char* IFACE_PROPS = "org.freedesktop.DBus.Properties";
        constexpr int POLL_INTERVAL_MS = 50;

        TransferState poll_once(GDBusConnection* bus, const std::string& path) {
            GError* error = nullptr;
            GVariant* reply = g_dbus_connection_call_sync(bus,
                                                          OBEX_NAME,
                                                          path.c_str(),
                                                          IFACE_PROPS,
                                                          "Get",
                                                          g_variant_new("(ss)", IFACE_TRANSFER, "Status"),
                                                          G_VARIANT_TYPE("(v)"),
                                                          G_DBUS_CALL_FLAGS_NONE,
                                                          5000,
                                                          nullptr,
                                                          &error);
            if (!reply) {
                g_clear_error(&error);
                return TransferState::Gone;
            }

            GVariant* boxed = nullptr;
            g_variant_get(reply, "(v)", &boxed);
            std::string status;
            if (boxed && g_variant_is_of_type(boxed, G_VARIANT_TYPE_STRING))
                status = g_variant_get_string(boxed, nullptr);
            if (boxed)
                g_variant_unref(boxed);
            g_variant_unref(reply);

            if (status == "complete")
                return TransferState::Complete;
            if (status == "error")
                return TransferState::Error;
            return TransferState::Active;
        }

    } // namespace

    struct TransferWatch::Impl {
        GDBusConnection* bus = nullptr;
        GMainContext* context = nullptr;
        guint subscription = 0;
        std::map<std::string, std::string> statuses;
    };

    namespace {
        void on_properties_changed(GDBusConnection*,
                                   const gchar*,
                                   const gchar* object_path,
                                   const gchar*,
                                   const gchar*,
                                   GVariant* parameters,
                                   gpointer user_data) {
            auto* impl = static_cast<TransferWatch::Impl*>(user_data);
            const gchar* iface = nullptr;
            GVariant* changed = nullptr;
            g_variant_get(parameters, "(&s@a{sv}as)", &iface, &changed, nullptr);
            if (changed) {
                GVariant* status = g_variant_lookup_value(changed, "Status", G_VARIANT_TYPE_STRING);
                if (status && object_path)
                    impl->statuses[object_path] = g_variant_get_string(status, nullptr);
                if (status)
                    g_variant_unref(status);
                g_variant_unref(changed);
            }
        }
    } // namespace

    TransferWatch::TransferWatch(GDBusConnection* bus) : impl_(new Impl) {
        impl_->bus = bus;
        impl_->context = g_main_context_new();
        if (!bus)
            return;
        g_main_context_push_thread_default(impl_->context);
        impl_->subscription = g_dbus_connection_signal_subscribe(bus,
                                                                 OBEX_NAME,
                                                                 IFACE_PROPS,
                                                                 "PropertiesChanged",
                                                                 nullptr,
                                                                 IFACE_TRANSFER,
                                                                 G_DBUS_SIGNAL_FLAGS_NONE,
                                                                 on_properties_changed,
                                                                 impl_,
                                                                 nullptr);
        g_main_context_pop_thread_default(impl_->context);
    }

    TransferWatch::~TransferWatch() {
        if (impl_->bus && impl_->subscription)
            g_dbus_connection_signal_unsubscribe(impl_->bus, impl_->subscription);
        // Anything still queued for the context is dropped with it
        while (g_main_context_iteration(impl_->context, FALSE)) {
        }
        g_main_context_unref(impl_->context);
        delete impl_;
    }

    void TransferWatch::pump() {
        while (g_main_context_iteration(impl_->context, FALSE)) {
        }
    }

    std::string TransferWatch::status(const std::string& path) const {
        auto it = impl_->statuses.find(path);
        return it == impl_->statuses.end() ? std::string{} : it->second;
    }

    TransferState wait_for_transfer(GDBusConnection* bus,
                                    const std::string& path,
                                    int timeout_seconds,
                                    TransferWatch* watch) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_seconds);
        TransferState state = TransferState::Active;
        auto from_watch = [&](TransferState& out) {
            if (!watch)
                return false;
            watch->pump();
            const std::string status = watch->status(path);
            if (status == "complete") {
                out = TransferState::Complete;
                return true;
            }
            if (status == "error") {
                out = TransferState::Error;
                return true;
            }
            return false;
        };

        while (std::chrono::steady_clock::now() < deadline) {
            if (from_watch(state))
                return state;
            state = poll_once(bus, path);
            if (state == TransferState::Complete || state == TransferState::Error)
                return state;
            // obexd drops the transfer object as soon as it settles, on success and
            // on failure alike, so polling loses the final status more often than it
            // catches it. Without a watch the disappearance is read as success, so a
            // download does not strand a message the phone did deliver. A watch says
            // how it ended; if even that is silent the outcome is genuinely unknown,
            // and a send must not be reported as made.
            if (state == TransferState::Gone) {
                if (!watch)
                    return TransferState::Complete;
                // the signal can trail the removal of the object by a moment
                for (int i = 0; i < 10; ++i) {
                    if (from_watch(state))
                        return state;
                    std::this_thread::sleep_for(std::chrono::milliseconds(POLL_INTERVAL_MS));
                }
                return TransferState::Unknown;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(POLL_INTERVAL_MS));
        }
        return state;
    }

    std::filesystem::path obex_staging_path(const std::string& name) {
        try {
            return std::filesystem::path(get_runtime_dir()) / name;
        } catch (const std::exception&) {
            return {};
        }
    }

} // namespace tether::bluetooth
