#include "tether/bluetooth/map_session.hpp"
#include "tether/bluetooth/obex_transfer.hpp"
#include "tether/log.hpp"

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>
#include <unistd.h>

namespace tether::bluetooth {

    namespace {

        constexpr const char* OBEX_NAME = "org.bluez.obex";
        constexpr const char* IFACE_MAP = "org.bluez.obex.MessageAccess1";
        constexpr const char* IFACE_MESSAGE = "org.bluez.obex.Message1";
        constexpr const char* IFACE_PROPS = "org.freedesktop.DBus.Properties";

        // Listing a large mailbox is a real OBEX transfer; give it room.
        constexpr int MAP_CALL_TIMEOUT_MS = 60000;
        constexpr int MAP_PUSH_CALL_TIMEOUT_MS = 15000;
        constexpr int MAP_PUSH_TIMEOUT_SECONDS = 30;
        constexpr int MAP_GET_TIMEOUT_SECONDS = 15;
        // obexd names the file before the transfer has written it, so it can
        // still be a moment behind when the transfer settles.
        constexpr int MAP_FILE_GRACE_SECONDS = 2;
        constexpr int MAP_FILE_POLL_MS = 50;

        std::string variant_string(GVariant* value) {
            if (!value)
                return {};
            if (g_variant_is_of_type(value, G_VARIANT_TYPE_STRING))
                return g_variant_get_string(value, nullptr);
            return {};
        }

        bool variant_bool(GVariant* value) {
            return value && g_variant_is_of_type(value, G_VARIANT_TYPE_BOOLEAN) && g_variant_get_boolean(value);
        }

        bool take_error(GError*& error, std::string& err_out) {
            err_out = error ? error->message : "unknown error";
            g_clear_error(&error);
            return false;
        }

    } // namespace

    struct MapSessionState {
        GDBusConnection* bus = nullptr;
        std::string path;
    };

    MapSession::MapSession(GDBusConnection* session_bus, std::string session_path)
        : state_(std::make_unique<MapSessionState>()) {
        // The session bus is owned by the profile ops, which are replaced when the
        // selected device changes. Hold a reference so an in-flight call cannot
        // outlive the connection it is using.
        state_->bus = session_bus ? G_DBUS_CONNECTION(g_object_ref(session_bus)) : nullptr;
        state_->path = std::move(session_path);
    }

    MapSession::~MapSession() { g_clear_object(&state_->bus); }

    const std::string& MapSession::path() const { return state_->path; }

    bool MapSession::set_folder(const std::string& folder, std::string& err) {
        GError* error = nullptr;
        // MessageAccess1.SetFolder, deliberately not PBAP's Select.
        GVariant* reply = g_dbus_connection_call_sync(state_->bus,
                                                      OBEX_NAME,
                                                      state_->path.c_str(),
                                                      IFACE_MAP,
                                                      "SetFolder",
                                                      g_variant_new("(s)", folder.c_str()),
                                                      nullptr,
                                                      G_DBUS_CALL_FLAGS_NONE,
                                                      MAP_CALL_TIMEOUT_MS,
                                                      nullptr,
                                                      &error);
        if (!reply)
            return take_error(error, err);
        g_variant_unref(reply);
        return true;
    }

    std::vector<MapListing> MapSession::list_messages(const std::string& folder, int max, std::string& err) {
        std::vector<MapListing> listings;

        GVariantBuilder filter;
        g_variant_builder_init(&filter, G_VARIANT_TYPE("a{sv}"));
        g_variant_builder_add(&filter, "{sv}", "MaxCount", g_variant_new_uint16(static_cast<guint16>(max)));

        GError* error = nullptr;
        GVariant* reply = g_dbus_connection_call_sync(state_->bus,
                                                      OBEX_NAME,
                                                      state_->path.c_str(),
                                                      IFACE_MAP,
                                                      "ListMessages",
                                                      g_variant_new("(sa{sv})", folder.c_str(), &filter),
                                                      // obexd returns a dict of object path -> properties,
                                                      // not an array of (path, properties) structs.
                                                      G_VARIANT_TYPE("(a{oa{sv}})"),
                                                      G_DBUS_CALL_FLAGS_NONE,
                                                      MAP_CALL_TIMEOUT_MS,
                                                      nullptr,
                                                      &error);
        if (!reply) {
            take_error(error, err);
            return listings;
        }

        GVariant* array = g_variant_get_child_value(reply, 0);
        GVariantIter iter;
        const gchar* object_path = nullptr;
        GVariant* props = nullptr;
        g_variant_iter_init(&iter, array);
        while (g_variant_iter_loop(&iter, "{&o@a{sv}}", &object_path, &props)) {
            MapListing listing;
            listing.handle = object_path ? object_path : "";

            GVariantIter prop_iter;
            const gchar* key = nullptr;
            GVariant* value = nullptr;
            g_variant_iter_init(&prop_iter, props);
            while (g_variant_iter_loop(&prop_iter, "{&sv}", &key, &value)) {
                const std::string name = key ? key : "";
                // The message text lives in Subject for a listing; a full
                // bMessage download is only needed when this is truncated.
                if (name == "Subject")
                    listing.subject = variant_string(value);
                else if (name == "Sender")
                    listing.sender_name = variant_string(value);
                else if (name == "SenderAddress")
                    listing.sender_address = variant_string(value);
                else if (name == "Recipient")
                    listing.recipient_name = variant_string(value);
                else if (name == "RecipientAddress")
                    listing.recipient_address = variant_string(value);
                else if (name == "Timestamp")
                    listing.timestamp = variant_string(value);
                else if (name == "Type")
                    listing.type = variant_string(value);
                else if (name == "Folder")
                    listing.folder = variant_string(value);
                else if (name == "Read")
                    listing.read = variant_bool(value);
                else if (name == "Sent")
                    listing.sent = variant_bool(value);
            }

            if (listing.folder.empty())
                listing.folder = folder;
            listings.push_back(std::move(listing));
        }

        g_variant_unref(array);
        g_variant_unref(reply);
        return listings;
    }

    bool MapSession::fetch_bmessage(const std::string& message_path, BMessage& out, std::string& err) {
        // Message1.Get writes the bMessage to a file and returns the transfer
        // object plus its properties.
        static std::atomic<uint64_t> serial{0};
        const std::filesystem::path target =
            obex_staging_path("tether-msg-" + std::to_string(getpid()) + "-" + std::to_string(serial++) + ".bmsg");
        if (target.empty()) {
            err = "XDG_RUNTIME_DIR is not set, and a message cannot be staged anywhere else.";
            return false;
        }
        std::error_code target_ec;
        std::filesystem::remove(target, target_ec);

        GError* error = nullptr;
        GVariant* reply = g_dbus_connection_call_sync(state_->bus,
                                                      OBEX_NAME,
                                                      message_path.c_str(),
                                                      IFACE_MESSAGE,
                                                      "Get",
                                                      g_variant_new("(sb)", target.c_str(), TRUE),
                                                      G_VARIANT_TYPE("(oa{sv})"),
                                                      G_DBUS_CALL_FLAGS_NONE,
                                                      MAP_CALL_TIMEOUT_MS,
                                                      nullptr,
                                                      &error);
        if (!reply)
            return take_error(error, err);

        const gchar* transfer_path = nullptr;
        GVariant* props = nullptr;
        g_variant_get(reply, "(&o@a{sv})", &transfer_path, &props);
        const std::string transfer = transfer_path ? transfer_path : "";
        GVariant* filename = props ? g_variant_lookup_value(props, "Filename", G_VARIANT_TYPE_STRING) : nullptr;
        std::string path = filename ? g_variant_get_string(filename, nullptr) : "";
        if (path.empty())
            path = target.string();
        if (filename)
            g_variant_unref(filename);
        if (props)
            g_variant_unref(props);
        g_variant_unref(reply);

        if (transfer.empty()) {
            err = "obexd returned no transfer object";
            return false;
        }

        std::error_code ec;
        const TransferState state = wait_for_transfer(state_->bus, transfer, MAP_GET_TIMEOUT_SECONDS);
        if (state == TransferState::Error) {
            err = "message transfer failed";
            std::filesystem::remove(path, ec);
            return false;
        }
        if (state == TransferState::Active) {
            err = "message transfer timed out";
            std::filesystem::remove(path, ec);
            return false;
        }

        const auto grace = std::chrono::steady_clock::now() + std::chrono::seconds(MAP_FILE_GRACE_SECONDS);
        while (!std::filesystem::exists(path, ec) && std::chrono::steady_clock::now() < grace)
            std::this_thread::sleep_for(std::chrono::milliseconds(MAP_FILE_POLL_MS));

        std::ifstream in(path, std::ios::binary);
        if (!in.is_open()) {
            err = "cannot read " + path;
            return false;
        }
        std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        in.close();
        std::filesystem::remove(path, ec);

        out = parse_bmessage(text);
        if (!out.valid) {
            err = "unparseable bMessage";
            return false;
        }
        return true;
    }

    bool MapSession::set_read(const std::string& message_path, bool read, std::string& err) {
        GError* error = nullptr;
        GVariant* reply =
            g_dbus_connection_call_sync(state_->bus,
                                        OBEX_NAME,
                                        message_path.c_str(),
                                        IFACE_PROPS,
                                        "Set",
                                        g_variant_new("(ssv)", IFACE_MESSAGE, "Read", g_variant_new_boolean(read)),
                                        nullptr,
                                        G_DBUS_CALL_FLAGS_NONE,
                                        MAP_CALL_TIMEOUT_MS,
                                        nullptr,
                                        &error);
        if (!reply)
            return take_error(error, err);
        g_variant_unref(reply);
        return true;
    }

    bool MapSession::push_message(const std::string& folder, const std::string& bmessage, std::string& err) {
        if (bmessage.empty()) {
            err = "Refusing to send an empty message.";
            return false;
        }

        static std::atomic<uint64_t> serial{0};
        std::filesystem::path source =
            obex_staging_path("tether-send-" + std::to_string(getpid()) + "-" + std::to_string(serial++) + ".bmsg");
        if (source.empty()) {
            err = "XDG_RUNTIME_DIR is not set, and a message cannot be staged anywhere else.";
            return false;
        }

        std::error_code ec;
        {
            std::ofstream out(source, std::ios::binary | std::ios::trunc);
            if (!out.is_open()) {
                err = "Cannot stage the message for sending.";
                return false;
            }
            out << bmessage;
            if (!out) {
                err = "Failed writing the staged message.";
                std::filesystem::remove(source, ec);
                return false;
            }
        }
        // The recipient the phone acts on lives in these bytes and nowhere else,
        // so a send that arrives addressed wrongly can be traced to a side. Only
        // the address lines are logged; the body is the user's message.
        for (size_t at = 0; at < bmessage.size();) {
            const size_t end = bmessage.find('\r', at);
            const std::string line = bmessage.substr(at, end == std::string::npos ? end : end - at);
            const bool addressed = line.size() > 4 && (line.rfind("TEL:", 0) == 0 || line.rfind("EMAIL:", 0) == 0);
            if (addressed && line.find(':') + 1 < line.size())
                debug::log(DEBUG, "bluetooth: sending to {} ({})", line, folder);
            if (end == std::string::npos)
                break;
            at = end + 2;
        }

        std::filesystem::permissions(source,
                                     std::filesystem::perms::owner_read | std::filesystem::perms::owner_write,
                                     std::filesystem::perm_options::replace,
                                     ec);

        GVariantBuilder args;
        g_variant_builder_init(&args, G_VARIANT_TYPE("a{sv}"));
        // The body is UTF-8; letting obexd transcode to the native charset would
        // mangle anything outside the phone's default encoding.
        g_variant_builder_add(&args, "{sv}", "Charset", g_variant_new_string("utf8"));

        // Made before the push so the final status of the transfer cannot be missed
        TransferWatch watch(state_->bus);

        GError* error = nullptr;
        GVariant* reply = g_dbus_connection_call_sync(state_->bus,
                                                      OBEX_NAME,
                                                      state_->path.c_str(),
                                                      IFACE_MAP,
                                                      "PushMessage",
                                                      g_variant_new("(ssa{sv})", source.c_str(), folder.c_str(), &args),
                                                      G_VARIANT_TYPE("(oa{sv})"),
                                                      G_DBUS_CALL_FLAGS_NONE,
                                                      MAP_PUSH_CALL_TIMEOUT_MS,
                                                      nullptr,
                                                      &error);
        if (!reply) {
            take_error(error, err);
            std::filesystem::remove(source, ec);
            return false;
        }

        const gchar* transfer_path = nullptr;
        GVariant* props = nullptr;
        g_variant_get(reply, "(&o@a{sv})", &transfer_path, &props);
        const std::string transfer = transfer_path ? transfer_path : "";
        if (props)
            g_variant_unref(props);
        g_variant_unref(reply);

        if (transfer.empty()) {
            err = "obexd returned no transfer object.";
            std::filesystem::remove(source, ec);
            return false;
        }

        const TransferState state = wait_for_transfer(state_->bus, transfer, MAP_PUSH_TIMEOUT_SECONDS, &watch);
        std::filesystem::remove(source, ec);

        if (state == TransferState::Error) {
            err = "The phone rejected the message.";
            return false;
        }
        if (state == TransferState::Active) {
            // The transfer is still running, so the message may yet be sent.
            err = "Timed out waiting for the phone to accept the message; it may still be sent.";
            return false;
        }
        if (state == TransferState::Unknown) {
            err = "The phone did not say whether the message was sent. Check the conversation before sending it again.";
            return false;
        }
        return true;
    }

    std::string map_handle_from_path(const std::string& object_path) {
        // "/org/bluez/obex/client/session5/message1086086778701665313" -> the
        // trailing id. The session number in the prefix changes on every
        // reconnect; the id does not.
        size_t slash = object_path.rfind('/');
        std::string tail = slash == std::string::npos ? object_path : object_path.substr(slash + 1);
        if (tail.rfind("message", 0) == 0)
            tail = tail.substr(7);
        return tail.empty() ? object_path : tail;
    }

    Message message_from_listing(const MapListing& listing) {
        // iOS sets Sent on messages in the sent folder, but not every phone
        // does, so the folder is the second opinion.
        const bool outgoing = listing.sent || is_outgoing_folder(listing.folder);

        // A conversation is keyed on the other party. For a message the phone
        // sent, that is the recipient — keying on the sender would file every
        // outgoing message under the user's own number instead of the thread it
        // belongs to, and would aim a reply there too.
        const std::string& address = outgoing ? listing.recipient_address : listing.sender_address;

        VCardParty peer;
        peer.name = outgoing ? listing.recipient_name : listing.sender_name;
        // MAP reports the address in one field without saying which kind it is;
        // an '@' is the only available discriminator.
        if (address.find('@') != std::string::npos)
            peer.email = address;
        else
            peer.tel = address;

        Message message;
        message.handle = map_handle_from_path(listing.handle);
        message.object_path = listing.handle;
        message.body = listing.subject;
        message.timestamp = parse_map_timestamp(listing.timestamp);
        message.read = listing.read;
        message.outgoing = outgoing;
        message.folder = listing.folder;
        // Empty when the phone gave no usable address for the other end. The
        // caller drops those rather than filing them under a guess.
        message.thread_key = thread_key_for(peer);
        message.peer_address = display_address(peer);
        message.peer_name = peer.name;
        return message;
    }

} // namespace tether::bluetooth
