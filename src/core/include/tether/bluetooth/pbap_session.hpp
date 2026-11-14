#pragma once

#include "tether/bluetooth/vcard.hpp"

#include <gio/gio.h>
#include <memory>
#include <string>
#include <vector>

namespace tether::bluetooth {

    // Pulling a large phonebook is a real OBEX transfer.
    inline constexpr int PBAP_CALL_TIMEOUT_MS = 60000;
    inline constexpr int PBAP_TRANSFER_TIMEOUT_SECONDS = 60;
    inline constexpr int PBAP_FILE_GRACE_SECONDS = 3;
    inline constexpr int PBAP_MAX_CONTACTS = 5000;
    // A failed phonebook pull (typically: Sync Contacts not switched on yet) is retried after 30 s, then twice as
    // long after each further failure, never less often than every 10 minutes. A good pull ends the retries.
    inline constexpr int PBAP_RETRY_FIRST_SECONDS = 30;
    inline constexpr int PBAP_RETRY_MAX_SECONDS = 600;
    inline constexpr int pbap_retry_delay(int failures) {
        int delay = PBAP_RETRY_FIRST_SECONDS;
        for (int i = 1; i < failures && delay < PBAP_RETRY_MAX_SECONDS; ++i)
            delay *= 2;
        return delay < PBAP_RETRY_MAX_SECONDS ? delay : PBAP_RETRY_MAX_SECONDS;
    }
    inline constexpr int PBAP_MAX_CALLS = 100;

    inline constexpr const char* PBAP_CONTACT_FIELDS = "['N','FN','TEL','EMAIL']";
    inline constexpr const char* PBAP_CALL_FIELDS = "['N','FN','TEL','X-IRMC-CALL-DATETIME']";

    struct PbapSessionState;

    // Drives org.bluez.obex.PhonebookAccess1 for one open OBEX session.
    class PbapSession {
    public:
        PbapSession(GDBusConnection* session_bus, std::string session_path);
        ~PbapSession();

        PbapSession(const PbapSession&) = delete;
        PbapSession& operator=(const PbapSession&) = delete;

        // Selects the phonebook. This is PBAP's Select("int", "pb")
        // (not MAP's SetFolder)
        bool select_phonebook(std::string& err,
                              const std::string& location = "int",
                              const std::string& phonebook = "pb");

        // Pulls the selected phonebook and parses it.
        //
        // Uses PullAll's MaxCount filter. MaxListCount is MAP's option and makes a
        // PBAP transfer fail or return nothing at all. `offset` 1 skips the main
        // phonebook's owner card (0.vcf); other phonebooks have none.
        std::vector<VCard> pull_all(std::string& err,
                                    int max = PBAP_MAX_CONTACTS,
                                    const char* fields = PBAP_CONTACT_FIELDS,
                                    int offset = 0);

        const std::string& path() const;

    private:
        std::unique_ptr<PbapSessionState> state_;
    };

} // namespace tether::bluetooth
