"""Compile the real state machine and exercise HOME offline transition paths."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from test_checked_settings import ROOT, compiler_command


DRIVER = r'''
#include <atomic>
#include <cassert>
#include <functional>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
// FatalError has no public entry transition. Seed only that terminal-state
// precondition directly; all assertions still execute the production methods.
#define private public
#include "device_state_machine.h"
#undef private

int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string scenario = argv[1];
    DeviceStateMachine machine;
    std::vector<std::pair<DeviceState, DeviceState>> changes;
    machine.AddStateChangeListener([&](DeviceState before, DeviceState after) {
        changes.emplace_back(before, after);
    });
    auto step = [&](DeviceState next) {
        const auto previous = machine.GetState();
        const auto count = changes.size();
        assert(machine.CanTransitionTo(next));
        assert(machine.TransitionTo(next) && machine.GetState() == next);
        assert(changes.size() == count + 1);
        assert(changes.back().first == previous && changes.back().second == next);
    };
    auto reject = [&](DeviceState next) {
        const auto previous = machine.GetState();
        const auto count = changes.size();
        assert(!machine.CanTransitionTo(next));
        assert(!machine.TransitionTo(next));
        assert(machine.GetState() == previous && changes.size() == count);
    };
    if (scenario == "terminal") {
        reject(kDeviceStateOffline);
        reject(kDeviceStateLocalCommand);
        machine.current_state_.store(kDeviceStateFatalError);
        for (int value = kDeviceStateUnknown; value < kDeviceStateFatalError; ++value)
            reject(static_cast<DeviceState>(value));
        assert(machine.TransitionTo(kDeviceStateFatalError)); // A no-op cannot notify.
        assert(changes.empty());
        return 0;
    }
    step(kDeviceStateStarting);
    if (scenario == "wifi-configuring") {
        step(kDeviceStateWifiConfiguring);
    } else if (scenario != "starting") {
        step(kDeviceStateActivating);
        step(kDeviceStateIdle);
        if (scenario == "listening") step(kDeviceStateListening);
        else if (scenario == "speaking") step(kDeviceStateSpeaking);
        else if (scenario == "connecting") step(kDeviceStateConnecting);
        else assert(false && "unknown source state");
    }
    step(kDeviceStateOffline);
    step(kDeviceStateLocalCommand);
    reject(kDeviceStateIdle); // Finish/cancel must pass through offline standby.
    step(kDeviceStateOffline);
    step(kDeviceStateIdle);
    reject(kDeviceStateLocalCommand);
}
'''


class HomeOfflineStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        path = Path(cls.temp.name)
        (path / "esp_log.h").write_text(
            "#pragma once\n#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGW(...) ((void)0)\n",
            encoding="utf-8",
        )
        source = path / "state_machine.cc"
        source.write_text(DRIVER, encoding="utf-8")
        cls.binary = path / "state_machine"
        compiled = subprocess.run(
            compiler_command() + ["-std=c++17", "-pthread", "-fsanitize=undefined",
                                  "-fno-sanitize-recover=undefined",
                                  "-DCONFIG_WAVESHARE_1_85C_HOME_OFFLINE=1",
                                  "-I", str(path), "-I", str(ROOT / "main"),
                                  str(source), str(ROOT / "main/device_state_machine.cc"),
                                  "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=30,
        )
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def run_scenario(self, scenario):
        executed = subprocess.run([str(self.binary), scenario], capture_output=True,
                                  text=True, timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_affected_states_can_degrade_execute_a_local_command_and_return_online(self):
        for scenario in ("starting", "wifi-configuring", "listening", "speaking", "connecting"):
            with self.subTest(source=scenario):
                self.run_scenario(scenario)

    def test_unknown_and_terminal_fatal_state_do_not_enter_offline(self):
        self.run_scenario("terminal")


if __name__ == "__main__":
    unittest.main()
