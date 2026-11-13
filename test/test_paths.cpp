#include "scoped_env.hpp"

#include <fstream>
#include <gtest/gtest.h>
#include <tether/paths.hpp>
#include <unistd.h>

namespace fs = std::filesystem;

namespace {

    // A throwaway $HOME per test, so a legacy directory made here cannot leak
    // into the next one.
    class PathsTest : public ::testing::Test {
    protected:
        void SetUp() override {
            fs::create_directories(home_);
            fs::create_directories(xdg_);
        }

        void TearDown() override {
            std::error_code ec;
            fs::remove_all(root_, ec);
        }

        const fs::path root_ =
            fs::temp_directory_path() / ("tether-paths-" + std::to_string(::getpid()) + "-" +
                                         ::testing::UnitTest::GetInstance()->current_test_info()->name());
        const fs::path home_ = root_ / "home";
        const fs::path xdg_ = root_ / "xdg";
    };

} // namespace

TEST_F(PathsTest, FallsBackToTheDefaultsWhenNoXdgVariableIsSet) {
    tether::testing::ScopedEnv home("HOME", home_);

    EXPECT_EQ(tether::paths::config_dir(), home_ / ".config/tether");
    EXPECT_EQ(tether::paths::data_dir(), home_ / ".local/share/tether");
    EXPECT_EQ(tether::paths::state_dir(), home_ / ".local/state/tether");
}

TEST_F(PathsTest, HonorsTheXdgVariables) {
    tether::testing::ScopedEnv home("HOME", home_);
    tether::testing::ScopedEnv config("XDG_CONFIG_HOME", xdg_ / "config");
    tether::testing::ScopedEnv data("XDG_DATA_HOME", xdg_ / "data");
    tether::testing::ScopedEnv state("XDG_STATE_HOME", xdg_ / "state");

    EXPECT_EQ(tether::paths::config_dir(), xdg_ / "config/tether");
    EXPECT_EQ(tether::paths::data_dir(), xdg_ / "data/tether");
    EXPECT_EQ(tether::paths::state_dir(), xdg_ / "state/tether");
}

TEST_F(PathsTest, IgnoresAnEmptyOrRelativeXdgValue) {
    tether::testing::ScopedEnv home("HOME", home_);

    {
        tether::testing::ScopedEnv config("XDG_CONFIG_HOME", std::string(""));
        EXPECT_EQ(tether::paths::config_dir(), home_ / ".config/tether");
    }
    {
        tether::testing::ScopedEnv config("XDG_CONFIG_HOME", std::string("relative/config"));
        EXPECT_EQ(tether::paths::config_dir(), home_ / ".config/tether");
    }
}

// An install that predates XDG support keeps its certificates and pairing.
TEST_F(PathsTest, PrefersAnExistingLegacyDirectoryOverAnAbsentXdgOne) {
    tether::testing::ScopedEnv home("HOME", home_);
    tether::testing::ScopedEnv config("XDG_CONFIG_HOME", xdg_ / "config");
    fs::create_directories(home_ / ".config/tether");

    EXPECT_EQ(tether::paths::config_dir(), home_ / ".config/tether");
}

TEST_F(PathsTest, UsesTheXdgDirectoryOnceItExists) {
    tether::testing::ScopedEnv home("HOME", home_);
    tether::testing::ScopedEnv config("XDG_CONFIG_HOME", xdg_ / "config");
    fs::create_directories(home_ / ".config/tether");
    fs::create_directories(xdg_ / "config/tether");

    EXPECT_EQ(tether::paths::config_dir(), xdg_ / "config/tether");
}

TEST(WritePrivateFile, CreatesOwnerOnlyAndReplacesAtomically) {
    const fs::path dir = fs::temp_directory_path() / ("tether_private_" + std::to_string(getpid()));
    fs::remove_all(dir);
    const fs::path file = dir / "nested" / "secret.json";

    ASSERT_TRUE(tether::paths::write_private_file(file, "first"));
    ASSERT_TRUE(tether::paths::write_private_file(file, "second"));

    std::ifstream in(file);
    std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    EXPECT_EQ(text, "second");
    EXPECT_EQ(fs::status(file).permissions() & (fs::perms::group_all | fs::perms::others_all), fs::perms::none);

    // nothing left beside it
    int entries = 0;
    for (const auto& entry : fs::directory_iterator(file.parent_path())) {
        (void)entry;
        ++entries;
    }
    EXPECT_EQ(entries, 1);
    fs::remove_all(dir);
}
