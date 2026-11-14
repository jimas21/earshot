#include "tether/bluetooth/hfp_profile.hpp"
#include "tether/bluetooth/monitor.hpp"
#include "tether/bluetooth/objects.hpp"
#include "tether/log.hpp"

#include <algorithm>
#include <chrono>
#include <gio/gio.h>
#include <mutex>

namespace tether::bluetooth {

    namespace {

        constexpr const char* PROFILE_PATH = "/org/tether/hfp_hf";
        constexpr const char* UUID_HFP_HF = "0000111e-0000-1000-8000-00805f9b34fb";

        constexpr const char* PROFILE_XML = R"XML(
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg type="o" name="device" direction="in"/>
      <arg type="h" name="fd" direction="in"/>
      <arg type="a{sv}" name="fd_properties" direction="in"/>
    </method>
    <method name="RequestDisconnection">
      <arg type="o" name="device" direction="in"/>
    </method>
  </interface>
</node>)XML";

        std::mutex g_mutex;
        guint g_registration_id = 0;
        bool g_registered = false;

        void handle_method(GDBusConnection*,
                           const gchar*,
                           const gchar*,
                           const gchar*,
                           const gchar* method,
                           GVariant*,
                           GDBusMethodInvocation* invocation,
                           gpointer) {
            const std::string name = method ? method : "";
            if (name == "Release" || name == "RequestDisconnection") {
                g_dbus_method_invocation_return_value(invocation, nullptr);
                return;
            }
            // Nothing here speaks HFP; the phone is told so rather than left waiting on a dead socket.
            g_dbus_method_invocation_return_dbus_error(
                invocation, "org.bluez.Error.Rejected", "This record only advertises the device class");
        }

        const GDBusInterfaceVTable VTABLE = {handle_method, nullptr, nullptr, {nullptr}};

        bool adapter_offers_handsfree(BluezMonitor& monitor) {
            for (const auto& adapter : monitor.snapshot().adapters) {
                for (const auto& uuid : adapter.uuids) {
                    std::string lower = uuid;
                    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) {
                        return static_cast<char>(std::tolower(c));
                    });
                    if (lower == UUID_HFP_HF)
                        return true;
                }
            }
            return false;
        }

        // Whether PipeWire's telephony service is running in this user's session.
        bool pipewire_telephony_present() {
            GError* error = nullptr;
            GDBusConnection* bus = g_bus_get_sync(G_BUS_TYPE_SESSION, nullptr, &error);
            if (!bus) {
                g_clear_error(&error);
                return false;
            }
            GVariant* reply = g_dbus_connection_call_sync(bus,
                                                          "org.freedesktop.DBus",
                                                          "/org/freedesktop/DBus",
                                                          "org.freedesktop.DBus",
                                                          "NameHasOwner",
                                                          g_variant_new("(s)", "org.pipewire.Telephony"),
                                                          G_VARIANT_TYPE("(b)"),
                                                          G_DBUS_CALL_FLAGS_NONE,
                                                          1000,
                                                          nullptr,
                                                          &error);
            bool present = false;
            if (reply) {
                gboolean owned = FALSE;
                g_variant_get(reply, "(b)", &owned);
                present = owned;
                g_variant_unref(reply);
            }
            g_clear_error(&error);
            g_object_unref(bus);
            return present;
        }

        // Caller holds g_mutex.
        void release_locked(BluezMonitor& monitor) {
            GDBusConnection* conn = monitor.connection();
            if (!conn || !g_registered)
                return;
            GError* error = nullptr;
            GVariant* reply = g_dbus_connection_call_sync(conn,
                                                          "org.bluez",
                                                          "/org/bluez",
                                                          "org.bluez.ProfileManager1",
                                                          "UnregisterProfile",
                                                          g_variant_new("(o)", PROFILE_PATH),
                                                          nullptr,
                                                          G_DBUS_CALL_FLAGS_NONE,
                                                          5000,
                                                          nullptr,
                                                          &error);
            if (reply)
                g_variant_unref(reply);
            g_clear_error(&error);
            monitor.invoke_sync([&] {
                if (g_registration_id)
                    g_dbus_connection_unregister_object(conn, g_registration_id);
                g_registration_id = 0;
            });
            g_registered = false;
        }

    } // namespace

    HandsfreeStub decide_handsfree_stub(const HandsfreeSources& sources) {
        const bool someone_else =
            sources.pipewire_telephony || (!sources.ours_registered && sources.adapter_lists_hfp);
        if (sources.ours_registered)
            return someone_else ? HandsfreeStub::Withdraw : HandsfreeStub::Keep;
        return someone_else ? HandsfreeStub::Leave : HandsfreeStub::Register;
    }

    bool ensure_handsfree_record(BluezMonitor& monitor, std::string& how) {
        GDBusConnection* conn = monitor.connection();
        if (!conn) {
            how = "no system bus";
            return false;
        }
        std::lock_guard<std::mutex> lock(g_mutex);

        // With calls off there is nothing for a Hands-Free record to do but invite the phone to route its audio here.
        if (const char* calls = std::getenv("TETHER_CALLS"); calls && std::string(calls) == "0") {
            release_locked(monitor);
            how = "calls are off";
            return false;
        }

        HandsfreeSources sources;
        sources.ours_registered = g_registered;
        sources.adapter_lists_hfp = adapter_offers_handsfree(monitor);
        sources.pipewire_telephony = pipewire_telephony_present();
        const bool pipewire = sources.pipewire_telephony;

        switch (decide_handsfree_stub(sources)) {
        case HandsfreeStub::Keep:
            how = "registered";
            return true;
        case HandsfreeStub::Withdraw:
            release_locked(monitor);
            debug::log(INFO, "bluetooth: withdrew the placeholder Hands-Free record; PipeWire telephony provides it");
            how = pipewire ? "pipewire" : "present";
            return true;
        case HandsfreeStub::Leave:
            how = pipewire ? "pipewire" : "present";
            return true;
        case HandsfreeStub::Register:
            break;
        }

        GError* error = nullptr;
        GDBusNodeInfo* info = g_dbus_node_info_new_for_xml(PROFILE_XML, &error);
        if (!info) {
            how = error ? error->message : "bad introspection";
            g_clear_error(&error);
            return false;
        }
        bool exported = false;
        monitor.invoke_sync([&] {
            g_registration_id = g_dbus_connection_register_object(
                conn, PROFILE_PATH, info->interfaces[0], &VTABLE, nullptr, nullptr, nullptr);
            exported = g_registration_id != 0;
        });
        g_dbus_node_info_unref(info);
        if (!exported) {
            how = "could not export the profile object";
            return false;
        }

        GVariantBuilder options;
        g_variant_builder_init(&options, G_VARIANT_TYPE("a{sv}"));
        g_variant_builder_add(&options, "{sv}", "Name", g_variant_new_string("Hands-Free"));
        g_variant_builder_add(&options, "{sv}", "Role", g_variant_new_string("client"));
        g_variant_builder_add(&options, "{sv}", "RequireAuthentication", g_variant_new_boolean(FALSE));
        g_variant_builder_add(&options, "{sv}", "RequireAuthorization", g_variant_new_boolean(FALSE));
        g_variant_builder_add(&options, "{sv}", "AutoConnect", g_variant_new_boolean(FALSE));

        GVariant* reply = g_dbus_connection_call_sync(conn,
                                                      "org.bluez",
                                                      "/org/bluez",
                                                      "org.bluez.ProfileManager1",
                                                      "RegisterProfile",
                                                      g_variant_new("(osa{sv})", PROFILE_PATH, UUID_HFP_HF, &options),
                                                      nullptr,
                                                      G_DBUS_CALL_FLAGS_NONE,
                                                      5000,
                                                      nullptr,
                                                      &error);
        if (!reply) {
            how = error ? error->message : "RegisterProfile failed";
            g_clear_error(&error);
            monitor.invoke_sync([&] {
                g_dbus_connection_unregister_object(conn, g_registration_id);
                g_registration_id = 0;
            });
            debug::log(WARN, "bluetooth: could not register a Hands-Free record: {}", how);
            return false;
        }
        g_variant_unref(reply);
        g_registered = true;
        how = "registered";
        debug::log(INFO, "bluetooth: registered a Hands-Free record so BlueZ sets the Audio service class");
        return true;
    }

    void release_handsfree_record(BluezMonitor& monitor) {
        std::lock_guard<std::mutex> lock(g_mutex);
        release_locked(monitor);
    }

    void reconcile_handsfree_record(BluezMonitor& monitor) {
        if (!no_root_mode())
            return;
        static std::mutex throttle_mutex;
        static std::chrono::steady_clock::time_point next_check{};
        {
            std::lock_guard<std::mutex> lock(throttle_mutex);
            const auto now = std::chrono::steady_clock::now();
            if (now < next_check)
                return;
            next_check = now + std::chrono::seconds(10);
        }
        std::string how;
        ensure_handsfree_record(monitor, how);
    }

} // namespace tether::bluetooth
