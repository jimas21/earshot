#include "scoped_env.hpp"

#include <gtest/gtest.h>
#include <tether/secret_store.hpp>

#include <filesystem>
#include <fstream>

using namespace tether;
using tether::testing::ScopedEnv;

namespace {

    // A scratch HOME, so every key in this file is a throwaway keyfile. The bus
    // is already pointed at nothing for the whole binary (secret_test_env.cpp).
    class ScopedKeyStore {
    public:
        ScopedKeyStore()
            : dir_(std::filesystem::temp_directory_path() /
                   ("tether-secret-" + std::to_string(::getpid()) + "-" + std::to_string(counter())))
            , home_("HOME", dir_) {
            std::filesystem::create_directories(dir_);
            secret::reset_for_test();
        }

        ~ScopedKeyStore() {
            secret::reset_for_test();
            std::error_code ec;
            std::filesystem::remove_all(dir_, ec);
        }

        const std::filesystem::path& dir() const { return dir_; }

    private:
        static int counter() {
            static int n = 0;
            return ++n;
        }

        std::filesystem::path dir_;
        ScopedEnv home_;
    };

    constexpr const char* SAMPLE = R"({"handle":"/msg/1","body":"hello ø"})";

} // namespace

TEST(SecretStore, SealUnsealRoundTrip) {
    ScopedKeyStore store;
    ASSERT_TRUE(secret::have_key());

    const std::string sealed = secret::seal(SAMPLE);
    ASSERT_FALSE(sealed.empty());
    EXPECT_NE(sealed[0], '{') << "a sealed record must not look like the plaintext JSON it replaced";

    std::string out;
    ASSERT_TRUE(secret::unseal(sealed, out));
    EXPECT_EQ(out, SAMPLE);
}

TEST(SecretStore, SealIsNonDeterministic) {
    ScopedKeyStore store;
    ASSERT_TRUE(secret::have_key());
    // A fresh nonce per record, so identical messages do not produce identical
    // lines and leak that a body repeated.
    EXPECT_NE(secret::seal(SAMPLE), secret::seal(SAMPLE));
}

TEST(SecretStore, SealedRecordHasNoNewline) {
    ScopedKeyStore store;
    // The journal is newline-framed, so a record containing one would corrupt
    // every line after it.
    const std::string sealed = secret::seal("first\nsecond\nthird");
    ASSERT_FALSE(sealed.empty());
    EXPECT_EQ(sealed.find('\n'), std::string::npos);

    std::string out;
    ASSERT_TRUE(secret::unseal(sealed, out));
    EXPECT_EQ(out, "first\nsecond\nthird");
}

TEST(SecretStore, TamperedRecordFailsToUnseal) {
    ScopedKeyStore store;
    std::string sealed = secret::seal(SAMPLE);
    ASSERT_GT(sealed.size(), 20u);

    // Flip a character in the middle of the ciphertext. GCM's tag has to catch
    // it rather than handing back mangled plaintext.
    sealed[sealed.size() / 2] = sealed[sealed.size() / 2] == 'A' ? 'B' : 'A';

    std::string out;
    EXPECT_FALSE(secret::unseal(sealed, out));
}

TEST(SecretStore, TruncatedRecordFailsToUnseal) {
    ScopedKeyStore store;
    const std::string sealed = secret::seal(SAMPLE);
    std::string out;
    EXPECT_FALSE(secret::unseal(sealed.substr(0, sealed.size() / 2), out));
    EXPECT_FALSE(secret::unseal("", out));
    EXPECT_FALSE(secret::unseal("not base64 at all!!", out));
}

TEST(SecretStore, ADifferentKeyCannotUnseal) {
    std::string sealed;
    {
        ScopedKeyStore store;
        sealed = secret::seal(SAMPLE);
        ASSERT_FALSE(sealed.empty());
    }
    // A second store means a second generated key. Losing the key loses the
    // history, which is the documented trade.
    ScopedKeyStore other;
    std::string out;
    EXPECT_FALSE(secret::unseal(sealed, out));
}

TEST(SecretStore, KeySurvivesWithinTheSameHome) {
    ScopedKeyStore store;
    const std::string sealed = secret::seal(SAMPLE);

    // Drop the cached key: the keyfile has to be enough to read it back, or a
    // daemon restart would lose everything it just wrote.
    secret::reset_for_test();

    std::string out;
    ASSERT_TRUE(secret::unseal(sealed, out));
    EXPECT_EQ(out, SAMPLE);
}

TEST(SecretStore, KeyfileIsOwnerOnly) {
    ScopedKeyStore store;
    ASSERT_TRUE(secret::have_key());

    const std::filesystem::path key = store.dir() / ".config" / "tether" / "store.key";
    ASSERT_TRUE(std::filesystem::exists(key));
    const auto mode = std::filesystem::status(key).permissions();
    EXPECT_EQ(mode & std::filesystem::perms::group_all, std::filesystem::perms::none);
    EXPECT_EQ(mode & std::filesystem::perms::others_all, std::filesystem::perms::none);
}

TEST(SecretStore, PlaintextModeIsTheIdentity) {
    ScopedKeyStore store;
    secret::set_retention(Retention::Plaintext);

    EXPECT_EQ(secret::seal(SAMPLE), SAMPLE);
    std::string out;
    ASSERT_TRUE(secret::unseal(SAMPLE, out));
    EXPECT_EQ(out, SAMPLE);
    EXPECT_TRUE(secret::have_key()) << "plaintext needs no key, so nothing should ever wait on one";
}

TEST(SecretStore, NoneModeSealsNothing) {
    ScopedKeyStore store;
    secret::set_retention(Retention::None);

    EXPECT_TRUE(secret::seal(SAMPLE).empty());
    std::string out;
    EXPECT_FALSE(secret::unseal("anything", out));
}

TEST(SecretStore, RetentionNamesRoundTrip) {
    EXPECT_STREQ(to_string(Retention::Encrypted), "encrypted");
    EXPECT_STREQ(to_string(Retention::Plaintext), "plaintext");
    EXPECT_STREQ(to_string(Retention::None), "none");

    EXPECT_EQ(retention_from_string("plaintext"), Retention::Plaintext);
    EXPECT_EQ(retention_from_string("none"), Retention::None);
    EXPECT_EQ(retention_from_string("encrypted"), Retention::Encrypted);
    // An unreadable or absent value must not silently downgrade the store.
    EXPECT_EQ(retention_from_string(""), Retention::Encrypted);
    EXPECT_EQ(retention_from_string("nonsense"), Retention::Encrypted);
}

// Without root the keyring may not exist, so a value nobody can read maps to keeping nothing.
TEST(SecretStore, UnknownRetentionKeepsNothingWithoutRoot) {
    tether::testing::ScopedEnv root("TETHER_NO_ROOT", std::string("1"));
    EXPECT_EQ(retention_from_string("nonsense"), Retention::None);
    EXPECT_EQ(retention_from_string("Plaintext "), Retention::None);
    // Absent and valid values are unaffected.
    EXPECT_EQ(retention_from_string(""), Retention::Encrypted);
    EXPECT_EQ(retention_from_string("encrypted"), Retention::Encrypted);
    EXPECT_EQ(retention_from_string("plaintext"), Retention::Plaintext);
}
