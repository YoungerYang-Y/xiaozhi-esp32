"""Run the real startup OTA policy with controlled host-side OTA responses."""

import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from test_startup_wake_word_hint import definition


ROOT = Path(__file__).resolve().parents[2]


class HomeStartupOtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest("requires a host C++17 compiler")
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        build = Path(cls.directory.name)
        method = definition(
            (ROOT / "main/application.cc").read_text(encoding="utf-8"),
            "void Application::CheckNewVersion()",
        )
        platform = r'''
            #include <cassert>
            #include <cstdio>
            #include <cstdlib>
            #include <string>
            #include <vector>
            #define TAG "HostApplication"
            #define ESP_LOGI(...) ((void)0)
            #define ESP_LOGW(...) ((void)0)
            #define ESP_LOGE(...) ((void)0)
            #define pdMS_TO_TICKS(ms) (ms)
            using esp_err_t = int;
            constexpr int ESP_OK = 0, ESP_ERR_TIMEOUT = 1;
            enum DeviceState { kDeviceStateActivating, kDeviceStateIdle };
            void vTaskDelay(int) { std::abort(); }
            namespace Lang {
                namespace Strings {
                    const char* CHECKING_NEW_VERSION = "Checking version";
                    const char* CHECK_NEW_VERSION_FAILED = "Retry in %d: %s";
                    const char* ERROR = "Error";
                    const char* ACTIVATION = "Activation";
                }
                namespace Sounds { const char* OGG_EXCLAMATION = ""; }
            }
            std::vector<std::string> operations;
            struct Display {
                void SetStatus(const char*) {}
            };
            struct Board {
                Display display;
                static Board& GetInstance() { static Board board; return board; }
                Display* GetDisplay() { return &display; }
            };
            struct NetworkError {
                std::string ToString() const { return "unexpected network failure"; }
                const char* Message() const { return "unexpected network failure"; }
            };
            struct CheckResult {
                explicit operator bool() const { return true; }
                NetworkError error() const { return {}; }
            };
            struct Ota {
                std::string response;
                int checks = 0, marks = 0, activations = 0;
                bool config_received = false;
                bool already_bound = false;
                CheckResult CheckVersion() {
                    assert(++checks <= 2);
                    operations.push_back("check");
                    // The HTTP/JSON boundary is controlled here. A force=1 response
                    // for the current version has the same HasNewVersion()==true
                    // contract as a newer version. This is not a JSON parser test.
                    config_received = true;
                    return {};
                }
                bool HasNewVersion() const {
                    return activations == 0 && response != "no-update";
                }
                std::string GetFirmwareUrl() const { return "https://example.test/app.bin"; }
                std::string GetFirmwareVersion() const {
                    return response == "new-version" ? "2.0.0" : "1.0.0";
                }
                void MarkCurrentVersionValid() {
                    assert(config_received);
                    ++marks;
                    operations.push_back("mark");
                }
                bool HasActivationCode() const { return !already_bound && activations == 0; }
                bool HasActivationChallenge() const { return !already_bound && activations == 0; }
                std::string GetActivationCode() const { return "123456"; }
                std::string GetActivationMessage() const { return "Bind this device"; }
                esp_err_t Activate() {
                    assert(marks == 1);
                    ++activations;
                    operations.push_back("activate");
                    return ESP_OK;
                }
            };
            struct Application {
                Ota* ota_;
                int upgrades = 0, activation_codes = 0;
                bool upgrade_success = true;
                void CheckNewVersion();
                DeviceState GetDeviceState() const { return kDeviceStateActivating; }
                template <class... T> void Alert(T&&...) { std::abort(); }
                void ShowActivationCode(const std::string& code, const std::string& message) {
                    assert(code == "123456" && message == "Bind this device");
                    ++activation_codes;
                    operations.push_back("show-code");
                }
                bool UpgradeFirmware(const std::string& url, const std::string& version) {
                    assert(url == ota_->GetFirmwareUrl());
                    assert(version == ota_->GetFirmwareVersion());
                    ++upgrades;
                    operations.push_back("upgrade");
                    return upgrade_success;
                }
            };
        '''
        checks = r'''
            int main(int argc, char** argv) {
                assert(argc == 5);
                Ota ota;
                ota.response = argv[1];
                ota.already_bound = std::string(argv[4]) == "bound";
                assert(ota.response == "new-version" || ota.response == "forced-current" ||
                       ota.response == "no-update");
                Application app{&ota};
                app.upgrade_success = std::string(argv[2]) == "upgrade-success";
                const bool expect_upgrade = std::string(argv[3]) == "upgrade";
                app.CheckNewVersion();
                // Returning to the caller makes subsequent protocol setup reachable.
                operations.push_back("returned");
                assert(ota.config_received);
                assert(app.upgrades == (expect_upgrade ? 1 : 0));
                if (expect_upgrade && app.upgrade_success) {
                    assert(ota.checks == 1 && ota.marks == 0 && ota.activations == 0);
                    assert(app.activation_codes == 0);
                    assert((operations == std::vector<std::string>{
                        "check", "upgrade", "returned"}));
                } else if (ota.already_bound) {
                    assert(ota.checks == 1 && ota.marks == 1 && ota.activations == 0);
                    assert(app.activation_codes == 0);
                    assert((operations == std::vector<std::string>{"check", "mark", "returned"}));
                } else {
                    assert(ota.checks == 2 && ota.marks == 2 && ota.activations == 1);
                    assert(app.activation_codes == 1);
                    auto expected = std::vector<std::string>{"check"};
                    if (expect_upgrade) expected.push_back("upgrade");
                    expected.insert(expected.end(), {"mark", "show-code", "activate",
                                                     "check", "mark", "returned"});
                    assert(operations == expected);
                }
            }
        '''
        source = build / "home_startup_ota.cc"
        source.write_text(textwrap.dedent(platform) + method + textwrap.dedent(checks),
                          encoding="utf-8")
        cls.binaries = {}
        for policy, flags in (
            ("home", ["-DCONFIG_WAVESHARE_1_85C_HOME=1"]),
            ("disabled", ["-DCONFIG_WAVESHARE_1_85C_HOME=0"]),
            ("undefined", []),
        ):
            binary = build / policy
            compiled = subprocess.run(
                compiler + ["-std=c++17", *flags, str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=30,
            )
            if compiled.returncode:
                raise AssertionError(compiled.stdout + compiled.stderr)
            cls.binaries[policy] = binary

    def run_scenario(self, policy, response, expect_upgrade=False, upgrade_success=True,
                     already_bound=False):
        result = subprocess.run(
            [str(self.binaries[policy]), response,
             "upgrade-success" if upgrade_success else "upgrade-failure",
             "upgrade" if expect_upgrade else "skip",
             "bound" if already_bound else "unbound"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_new_version_preserves_configuration_validation_and_activation(self):
        self.run_scenario("home", "new-version")

    def test_home_forced_current_version_preserves_validation_and_activation(self):
        self.run_scenario("home", "forced-current")

    def test_home_no_update_still_validates_and_activates(self):
        self.run_scenario("home", "no-update")

    def test_home_bound_device_returns_without_reactivation_for_all_update_responses(self):
        for response in ("new-version", "forced-current", "no-update"):
            with self.subTest(response=response):
                self.run_scenario("home", response, already_bound=True)

    def test_legacy_policies_still_upgrade_new_and_forced_versions(self):
        for policy in ("disabled", "undefined"):
            for response in ("new-version", "forced-current"):
                with self.subTest(policy=policy, response=response):
                    self.run_scenario(policy, response, expect_upgrade=True)

    def test_legacy_no_update_still_validates_and_activates(self):
        for policy in ("disabled", "undefined"):
            with self.subTest(policy=policy):
                self.run_scenario(policy, "no-update")

    def test_legacy_failed_upgrade_still_validates_and_activates(self):
        for policy in ("disabled", "undefined"):
            with self.subTest(policy=policy):
                self.run_scenario(policy, "new-version", expect_upgrade=True,
                                  upgrade_success=False)


if __name__ == "__main__":
    unittest.main()
