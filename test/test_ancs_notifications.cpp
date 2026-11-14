#include <algorithm>
#include <filesystem>
#include <gtest/gtest.h>
#include <tether/bluetooth/ancs/client.hpp>
#include <tether/bluetooth/ancs/hold.hpp>
#include <tether/bluetooth/ancs/notifications.hpp>
#include <tether/bluetooth/config.hpp>

using namespace tether::bluetooth;
using namespace tether::bluetooth::ancs;

namespace {

    Notification make(uint32_t uid, const std::string& app_id) {
        Notification n;
        n.uid = uid;
        n.app_id = app_id;
        n.app_name = derive_app_name(app_id);
        return n;
    }

} // namespace

TEST(AncsAppName, DerivesTheLastBundleComponent) {
    EXPECT_EQ(derive_app_name("com.burbn.instagram"), "Instagram");
    EXPECT_EQ(derive_app_name("com.apple.MobileSMS"), "MobileSMS");
    EXPECT_EQ(derive_app_name("Signal"), "Signal");
    EXPECT_EQ(derive_app_name("com.example."), "com.example.");
    EXPECT_EQ(derive_app_name(""), "");
}

TEST(AncsIcons, PrefersTheAppOverItsCategory) {
    Notification n = make(1, "com.burbn.instagram");
    n.category = CategoryId::Social;
    const auto icons = icon_candidates(n);
    ASSERT_FALSE(icons.empty());
    EXPECT_EQ(icons.front(), "instagram");
    EXPECT_NE(std::find(icons.begin(), icons.end(), "internet-chat"), icons.end())
        << "the category icon stands in for themes without brand logos";
}

TEST(AncsIcons, PrefersTheThemeOverBundledArt) {
    const std::vector<std::pair<std::string, std::vector<std::string>>> expected{
        {"com.apple.MobileSMS", {"internet-chat"}},
        {"com.microsoft.Office.Outlook", {"ms-outlook"}},
        {"com.hammerandchisel.discord", {"discord", "tether-app-discord"}},
        {"com.atebits.Tweetie2", {"twitter", "tether-app-x"}},
        {"org.whispersystems.signal", {"signal-desktop", "tether-app-signal"}},
        {"com.instructure.icanvas", {"tether-app-canvas"}},
    };

    for (const auto& [app_id, icons] : expected) {
        const auto candidates = icon_candidates(make(1, app_id));
        ASSERT_GE(candidates.size(), icons.size()) << app_id;
        EXPECT_TRUE(std::equal(icons.begin(), icons.end(), candidates.begin())) << app_id;
    }
}

TEST(AncsIcons, EveryBundledNameHasAnIconFile) {
    const char* app_ids[] = {
        "com.atebits.Tweetie2",
        "com.burbn.instagram",
        "com.facebook.Facebook",
        "com.github.github",
        "com.github.stormbreaker.prod",
        "com.google.Gmail",
        "com.google.ios.youtube",
        "com.hammerandchisel.discord",
        "com.instructure.icanvas",
        "com.reddit.Reddit",
        "com.spotify.client",
        "com.toyopagroup.picaboo",
        "com.zhiliaoapp.musically",
        "net.whatsapp.WhatsApp",
        "org.telegram.messenger",
        "org.whispersystems.signal",
    };
    const std::filesystem::path dir = TETHER_NOTIFICATION_ICONS_DIR;

    for (const char* app_id : app_ids) {
        const auto candidates = icon_candidates(make(1, app_id));
        const auto bundled = std::find_if(
            candidates.begin(), candidates.end(), [](const std::string& n) { return n.rfind("tether-app-", 0) == 0; });
        ASSERT_NE(bundled, candidates.end()) << app_id;
        EXPECT_TRUE(std::filesystem::exists(dir / (*bundled + ".svg"))) << *bundled;
    }
}

TEST(AncsIcons, FallsBackToTheCategoryForAnUnknownApp) {
    Notification n = make(2, "com.example.unheardof");
    n.category = CategoryId::Email;
    EXPECT_EQ(icon_candidates(n).front(), "mail-unread");
}

TEST(AncsIcons, AlwaysEndsWithAnIconThatShipsWithTether) {
    for (uint8_t category = 0; category <= static_cast<uint8_t>(CategoryId::Entertainment); ++category) {
        Notification n = make(3, "");
        n.category = static_cast<CategoryId>(category);
        const auto icons = icon_candidates(n);
        ASSERT_FALSE(icons.empty()) << "category " << int(category);
        EXPECT_EQ(icons.back(), "tether") << "category " << int(category);
    }
}

TEST(AncsLaunchTarget, OffersAWayToReachTheLinuxApp) {
    const auto whatsapp = launch_target("net.whatsapp.WhatsApp");
    EXPECT_EQ(whatsapp.uri_scheme, "whatsapp");
    EXPECT_FALSE(whatsapp.desktop_ids.empty());

    const auto telegram = launch_target("ph.telegra.Telegraph");
    EXPECT_EQ(telegram.uri_scheme, launch_target("org.telegram.messenger").uri_scheme)
        << "both Telegram bundle ids reach the same client";

    EXPECT_FALSE(launch_target("org.whispersystems.signal").empty());
}

TEST(AncsLaunchTarget, IsEmptyWithoutALinuxCounterpart) {
    EXPECT_TRUE(launch_target("com.example.unheardof").empty());
    EXPECT_TRUE(launch_target("").empty());
    // Messages notifications reply through tether-gtk instead.
    EXPECT_TRUE(launch_target(APP_ID_MESSAGES).empty());
}

TEST(AncsRegistry, RenameAppTouchesOnlyThatApp) {
    NotificationRegistry registry;
    registry.store(make(1, "com.burbn.instagram"));
    registry.store(make(2, "com.burbn.instagram"));
    registry.store(make(3, "com.apple.MobileSMS"));

    registry.rename_app("com.burbn.instagram", "Instagram");

    EXPECT_EQ(registry.find(1)->app_name, "Instagram");
    EXPECT_EQ(registry.find(2)->app_name, "Instagram");
    EXPECT_EQ(registry.find(3)->app_name, "MobileSMS");
}

TEST(AncsRegistry, RenameAppIgnoresEmptyArguments) {
    NotificationRegistry registry;
    registry.store(make(1, "com.burbn.instagram"));

    registry.rename_app("com.burbn.instagram", "");
    registry.rename_app("", "Instagram");

    EXPECT_EQ(registry.find(1)->app_name, "Instagram");
}

// Measured on a live phone: ANCS UIDs are per-connection counters that restart
// low on every LE session -- eight retained notifications carried uids 3, 9, 12,
// 13, 16, 17, 18 and 29, with one notification present twice under 9 and 29. A
// dedupe set carried across the session boundary therefore reads a genuinely new
// notification as a replay and drops it, which is a phone that has stopped
// mirroring for no visible reason.
TEST(AncsRegistry, ANewSessionStopsRecycledUidsReadingAsReplays) {
    NotificationRegistry registry;

    SourceEvent first;
    first.uid = 9;
    EXPECT_EQ(registry.classify(first), Decision::Fetch);
    registry.remember(first);
    EXPECT_EQ(registry.classify(first), Decision::Ignore) << "same session, same uid: a replay";

    registry.begin_session();

    SourceEvent recycled;
    recycled.uid = 9; // a different notification, same counter value
    EXPECT_EQ(registry.classify(recycled), Decision::Fetch)
        << "a recycled uid from a new session was dropped as a replay";
}

// The dedupe reset must not empty the notification list: an LE blip shows up as
// the device path dropping and returning, and wiping the list there would clear
// the UI on every flap.
TEST(AncsRegistry, ANewSessionKeepsWhatIsOnScreen) {
    NotificationRegistry registry;
    registry.store(make(9, "com.burbn.instagram"));
    const uint64_t before = registry.session();

    registry.begin_session();

    ASSERT_NE(registry.find(9), nullptr) << "a session change emptied the list";
    EXPECT_EQ(registry.find(9)->session, before) << "the stored copy must keep the session that issued its uid";
    EXPECT_NE(registry.session(), before);
}

// A reconnect replays the backlog under fresh uids. Without replacing the stale
// copy, every reconnect would list each notification again.
TEST(AncsRegistry, AReplayUnderANewSessionReplacesTheStaleCopy) {
    NotificationRegistry registry;
    Notification raid = make(9, "com.nianticlabs.pokemongo");
    raid.title = "Raid Invitation";
    raid.received = 100;
    registry.store(raid);
    Notification card = make(3, "com.apple.Passbook");
    card.title = "Apple Card";
    card.received = 100;
    registry.store(card);

    registry.begin_session();

    Notification replay = raid;
    replay.uid = 29;
    registry.store(replay);

    EXPECT_EQ(registry.size(), 2u) << "the replay was listed twice";
    EXPECT_EQ(registry.find(9), nullptr) << "the copy kept must carry the live uid";
    EXPECT_NE(registry.find(29), nullptr);
    EXPECT_NE(registry.find(3), nullptr) << "a different notification from the old session was dropped";

    Notification twin = replay;
    twin.uid = 30;
    registry.store(twin);
    EXPECT_EQ(registry.size(), 3u) << "identical notifications within one session are both real";
}

TEST(AncsRegistry, RecentIsOrderedByDeliveryTime) {
    NotificationRegistry registry;
    // Stored in the order iOS replayed them, which bears no relation to when the
    // phone delivered them.
    for (auto [uid, received] : {std::pair<uint32_t, int64_t>{1, 300}, {2, 100}, {3, 200}}) {
        Notification n = make(uid, "com.example.app");
        n.received = received;
        registry.store(n);
    }

    std::vector<Notification> recent = registry.recent();
    ASSERT_EQ(recent.size(), 3u);
    EXPECT_EQ(recent[0].uid, 1u);
    EXPECT_EQ(recent[1].uid, 3u);
    EXPECT_EQ(recent[2].uid, 2u);

    EXPECT_EQ(registry.recent(2).size(), 2u) << "the limit applies after sorting";
    EXPECT_EQ(registry.recent(2)[0].uid, 1u);
}

// Content mirroring was off with no way to turn it on, so a stored false from
// before the toggle existed must not pin the new default.
TEST(BluetoothConfig, UnversionedFileTakesTheContentDefault) {
    const Config config = deserialize_config(R"({"ancs_content_enabled": false})");
    EXPECT_TRUE(config.ancs_content_enabled);
}

TEST(BluetoothConfig, VersionedFileKeepsTheStoredChoice) {
    const Config off = deserialize_config(R"({"config_version": 1, "ancs_content_enabled": false})");
    EXPECT_FALSE(off.ancs_content_enabled);

    const Config on = deserialize_config(R"({"config_version": 1, "ancs_content_enabled": true})");
    EXPECT_TRUE(on.ancs_content_enabled);
}

TEST(BluetoothConfig, SerializeStampsTheVersion) {
    Config config;
    config.ancs_content_enabled = false;
    EXPECT_FALSE(deserialize_config(serialize_config(config)).ancs_content_enabled);
}

// While the LE link is down there is nothing to subscribe to, so the client waits for it instead of rebuilding.
TEST(AncsLink, WithTheBearerApiTheBearerDecides) {
    EXPECT_TRUE(ancs_link_usable(true, true, true));
    EXPECT_FALSE(ancs_link_usable(true, false, true)) << "a Classic-only link resolved services, but LE is down";
    EXPECT_FALSE(ancs_link_usable(true, false, false));
}

TEST(AncsLink, WithoutTheBearerApiServicesResolvedIsTheOnlySign) {
    EXPECT_TRUE(ancs_link_usable(false, false, true));
    EXPECT_FALSE(ancs_link_usable(false, false, false)) << "BlueZ clears ServicesResolved when the link drops";
}

// A notification waits for its app's real display name instead of showing the one guessed from the bundle id.
TEST(AncsNameHold, WaitsOnlyWhileALookupIsOutstanding) {
    EXPECT_EQ(name_action(false, false, true), NameAction::Hold);
    EXPECT_EQ(name_action(false, false, false), NameAction::Emit) << "no lookup in flight: nothing to wait for";
    EXPECT_EQ(name_action(false, true, true), NameAction::Emit) << "the name is known";
    EXPECT_EQ(name_action(false, true, false), NameAction::Emit);
    EXPECT_EQ(name_action(true, false, true), NameAction::Emit) << "no app id, no name to wait for";
}

TEST(AncsNameHold, ReleasesAnAppsNotificationsInOrderUnderTheRealName) {
    HeldNotifications held;
    held.hold(make(1, "com.security.BatteryCam"), 10);
    held.hold(make(2, "com.other.App"), 10);
    held.hold(make(3, "com.security.BatteryCam"), 11);
    EXPECT_TRUE(held.holding("com.security.BatteryCam"));

    const auto released = held.release("com.security.BatteryCam", "Anker eufy");
    ASSERT_EQ(released.size(), 2u);
    EXPECT_EQ(released[0].uid, 1u);
    EXPECT_EQ(released[1].uid, 3u);
    EXPECT_EQ(released[0].app_name, "Anker eufy");
    EXPECT_EQ(released[1].app_name, "Anker eufy");
    EXPECT_FALSE(held.holding("com.security.BatteryCam"));
    EXPECT_TRUE(held.holding("com.other.App")) << "another app's notification is not released with it";
    EXPECT_EQ(held.size(), 1u);
}

TEST(AncsNameHold, AnAnswerWithNoNameReleasesUnderTheDerivedOne) {
    HeldNotifications held;
    held.hold(make(1, "com.security.BatteryCam"), 0);
    const auto released = held.release("com.security.BatteryCam", {});
    ASSERT_EQ(released.size(), 1u);
    EXPECT_EQ(released[0].app_name, derive_app_name("com.security.BatteryCam"));
}

TEST(AncsNameHold, GivesUpAfterTheWaitAndNeverDropsAnything) {
    HeldNotifications held;
    held.hold(make(1, "com.a.A"), 100);
    held.hold(make(2, "com.b.B"), 102);
    held.hold(make(3, "com.a.A"), 102);

    std::vector<std::string> apps;
    EXPECT_TRUE(held.release_expired(100 + APP_NAME_WAIT_SECONDS - 1, &apps).empty());
    EXPECT_TRUE(apps.empty());

    // The oldest of app A is due, so all of A goes, in order, and B (younger) stays.
    const auto expired = held.release_expired(100 + APP_NAME_WAIT_SECONDS, &apps);
    ASSERT_EQ(expired.size(), 2u);
    EXPECT_EQ(expired[0].uid, 1u);
    EXPECT_EQ(expired[1].uid, 3u);
    EXPECT_EQ(expired[0].app_name, derive_app_name("com.a.A"));
    ASSERT_EQ(apps.size(), 1u);
    EXPECT_EQ(apps[0], "com.a.A");
    EXPECT_EQ(held.size(), 1u);

    const auto rest = held.release_expired(102 + APP_NAME_WAIT_SECONDS);
    ASSERT_EQ(rest.size(), 1u);
    EXPECT_EQ(rest[0].uid, 2u);
    EXPECT_EQ(held.size(), 0u);
}

TEST(AncsNameHold, ARemovalCancelsAHeldNotification) {
    HeldNotifications held;
    held.hold(make(1, "com.a.A"), 0);
    held.hold(make(2, "com.a.A"), 0);
    EXPECT_TRUE(held.cancel(1));
    EXPECT_FALSE(held.cancel(1));
    EXPECT_FALSE(held.cancel(99));
    const auto released = held.release("com.a.A", "A");
    ASSERT_EQ(released.size(), 1u);
    EXPECT_EQ(released[0].uid, 2u);
}

TEST(AncsNameHold, AnUpdateKeepsThePlaceOfTheNotificationItUpdates) {
    HeldNotifications held;
    held.hold(make(1, "com.a.A"), 0);
    held.hold(make(2, "com.a.A"), 0);
    Notification updated = make(1, "com.a.A");
    updated.body = "newer text";
    held.hold(updated, 5);

    const auto released = held.release("com.a.A", "A");
    ASSERT_EQ(released.size(), 2u);
    EXPECT_EQ(released[0].uid, 1u);
    EXPECT_EQ(released[0].body, "newer text");
    EXPECT_EQ(released[1].uid, 2u);
}

TEST(AncsNameHold, EndingASessionLosesNothing) {
    HeldNotifications held;
    held.hold(make(1, "com.a.A"), 0);
    held.hold(make(2, "com.b.B"), 0);
    const auto all = held.release_all();
    ASSERT_EQ(all.size(), 2u);
    EXPECT_EQ(all[0].app_name, derive_app_name("com.a.A"));
    EXPECT_EQ(all[1].app_name, derive_app_name("com.b.B"));
    EXPECT_EQ(held.size(), 0u);
}
