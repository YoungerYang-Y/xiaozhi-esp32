"""Exercise the production connection health tracker on the host."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ConnectionHealthTests(unittest.TestCase):
    def run_driver(self, body):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "connection_health.cc"
            source.write_text(
                '#include <cassert>\n#include "main/connection_health.h"\n'
                'int main() { ConnectionHealth health;\n' + body + '\n}\n',
                encoding="utf-8",
            )
            binary = Path(directory) / "connection_health"
            result = subprocess.run(
                compiler + ["-std=c++23", "-I", str(ROOT), str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_requires_positive_evidence_and_required_stages(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto ws = StageMask(Stage::Configuration) | StageMask(Stage::Handshake) |
                                StageMask(Stage::Media);
            constexpr auto mqtt = ws | StageMask(Stage::Control);
            health.SetRequiredStages(ws);
            assert(health.GetServiceState() == ServiceState::Unknown);
            health.BeginAttempt(Stage::Handshake, 1);
            health.ReportSuccess(Stage::Handshake, 1);
            assert(health.GetServiceState() == ServiceState::Unknown);
            health.BeginAttempt(Stage::Configuration, 1);
            health.ReportSuccess(Stage::Configuration, 1);
            health.BeginAttempt(Stage::Media, 1);
            health.ReportSuccess(Stage::Media, 1);
            assert(health.GetServiceState() == ServiceState::Unknown);
            health.OnWifiConnected();
            assert(health.GetServiceState() == ServiceState::Available);
            health.SetRequiredStages(mqtt);
            assert(health.GetServiceState() == ServiceState::Unknown);
        ''')

    def test_stage_success_only_clears_its_own_fault_and_wifi_does_not_clear_faults(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto all = StageMask(Stage::Configuration) | StageMask(Stage::Control) |
                                 StageMask(Stage::Handshake) | StageMask(Stage::Media);
            health.SetRequiredStages(all);
            health.BeginAttempt(Stage::Control, 10);
            health.ReportFailure(Stage::Control, 10);
            health.BeginAttempt(Stage::Media, 20);
            health.ReportFailure(Stage::Media, 20);
            health.BeginAttempt(Stage::Handshake, 30);
            health.ReportFailure(Stage::Handshake, 30);
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Control, 11);
            health.ReportSuccess(Stage::Control, 11);
            health.BeginAttempt(Stage::Handshake, 31);
            health.ReportSuccess(Stage::Handshake, 31);
            assert(health.GetStageState(Stage::Media) == StageState::Failed);
            assert(health.GetServiceState() == ServiceState::Unavailable);
            health.BeginAttempt(Stage::Media, 21);
            health.ReportSuccess(Stage::Media, 21);
            assert(health.GetServiceState() == ServiceState::Unknown);
        ''')

    def test_attempt_identity_rejects_late_events_and_retire_keeps_fault(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            health.BeginAttempt(Stage::Media, 1);
            health.ReportFailure(Stage::Media, 1);
            health.BeginAttempt(Stage::Media, 2);
            assert(health.GetStageState(Stage::Media) == StageState::Failed);
            assert(!health.ReportSuccess(Stage::Media, 1));
            assert(!health.ReportFailure(Stage::Media, 1));
            assert(health.GetStageState(Stage::Media) == StageState::Failed);
            health.Retire(Stage::Media);
            assert(!health.ReportSuccess(Stage::Media, 2));
            assert(health.GetStageState(Stage::Media) == StageState::Failed);
        ''')

    def test_new_attempt_invalidates_prior_healthy_evidence(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto media = StageMask(Stage::Media);
            health.SetRequiredStages(media);
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Media, 1);
            health.ReportSuccess(Stage::Media, 1);
            assert(health.GetServiceState() == ServiceState::Available);
            health.BeginAttempt(Stage::Media, 2);
            assert(health.GetStageState(Stage::Media) == StageState::Unknown);
            assert(health.GetServiceState() == ServiceState::Unknown);
            health.BeginAttempt(Stage::Media, 3);
            health.ReportSuccess(Stage::Media, 3);
            health.Retire(Stage::Media);
            assert(health.GetStageState(Stage::Media) == StageState::Unknown);
            assert(health.GetServiceState() == ServiceState::Unknown);
        ''')

    def test_repeated_begin_for_same_attempt_keeps_verified_evidence(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto media = StageMask(Stage::Media);
            health.SetRequiredStages(media);
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Media, 7);
            health.ReportSuccess(Stage::Media, 7);
            health.BeginAttempt(Stage::Media, 7);
            assert(health.GetStageState(Stage::Media) == StageState::Healthy);
            assert(health.GetServiceState() == ServiceState::Available);
        ''')

    def test_repeated_attempts_preserve_service_prompt_episode_until_all_faults_clear(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto all = StageMask(Stage::Configuration) | StageMask(Stage::Control) |
                                 StageMask(Stage::Handshake) | StageMask(Stage::Media);
            health.SetRequiredStages(all);
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Media, 1);
            health.ReportFailure(Stage::Media, 1);
            health.BeginAttempt(Stage::Handshake, 1);
            health.ReportFailure(Stage::Handshake, 1);
            const auto first = health.GetPendingPrompt();
            assert(first.kind == PromptKind::ServiceUnavailable);
            assert(health.MarkPromptStarted(first));
            for (uint64_t id = 2; id <= 4; ++id) {
                health.BeginAttempt(Stage::Media, id);
                assert(health.GetServiceState() == ServiceState::Unavailable);
                health.BeginAttempt(Stage::Media, id + 10);
                health.ReportFailure(Stage::Media, id + 10);
                assert(health.GetPendingPrompt().kind == PromptKind::None);
            }
            health.BeginAttempt(Stage::Media, 30);
            health.ReportSuccess(Stage::Media, 30);
            health.BeginAttempt(Stage::Media, 31);
            health.ReportFailure(Stage::Media, 31);
            assert(health.GetServiceState() == ServiceState::Unavailable);
            assert(health.GetPendingPrompt().kind == PromptKind::None);
            health.BeginAttempt(Stage::Handshake, 2);
            health.ReportSuccess(Stage::Handshake, 2);
            health.BeginAttempt(Stage::Media, 32);
            health.ReportSuccess(Stage::Media, 32);
            health.BeginAttempt(Stage::Media, 33);
            health.ReportFailure(Stage::Media, 33);
            const auto second = health.GetPendingPrompt();
            assert(second.kind == PromptKind::ServiceUnavailable);
            assert(second.episode != first.episode);
        ''')

    def test_wifi_episode_is_independent_and_has_prompt_priority(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto all = StageMask(Stage::Configuration) | StageMask(Stage::Control) |
                                 StageMask(Stage::Handshake) | StageMask(Stage::Media);
            health.SetRequiredStages(all);
            health.BeginAttempt(Stage::Media, 1);
            health.ReportFailure(Stage::Media, 1);
            health.OnWifiDisconnected();
            const auto wifi = health.GetPendingPrompt();
            assert(wifi.kind == PromptKind::WifiUnavailable);
            assert(health.MarkPromptStarted(wifi));
            health.BeginAttempt(Stage::Control, 1);
            health.ReportFailure(Stage::Control, 1);
            assert(health.GetPendingPrompt().kind == PromptKind::None);
            health.OnWifiConnected();
            assert(health.GetPendingPrompt().kind == PromptKind::ServiceUnavailable);
            assert(health.GetStageState(Stage::Media) == StageState::Failed);
        ''')

    def test_queued_prompt_is_not_consumed_until_started_and_stale_ticket_is_rejected(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            constexpr auto all = StageMask(Stage::Configuration) | StageMask(Stage::Control) |
                                 StageMask(Stage::Handshake) | StageMask(Stage::Media);
            health.SetRequiredStages(all);
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Control, 1);
            health.ReportFailure(Stage::Control, 1);
            const auto queued = health.GetPendingPrompt();
            assert(health.GetPendingPrompt().episode == queued.episode); // queueing does not consume
            health.BeginAttempt(Stage::Control, 2);
            health.ReportSuccess(Stage::Control, 2);
            health.BeginAttempt(Stage::Media, 1);
            health.ReportFailure(Stage::Media, 1);
            const auto fresh = health.GetPendingPrompt();
            assert(fresh.episode != queued.episode);
            assert(!health.MarkPromptStarted(queued));
            assert(health.MarkPromptStarted(fresh));
            assert(health.GetPendingPrompt().kind == PromptKind::None);
            health.OnWifiDisconnected(); // interrupt after genuine start
            health.OnWifiConnected();
            assert(health.GetPendingPrompt().kind == PromptKind::None);
        ''')

    def test_started_service_prompt_is_consumed_even_if_wifi_priority_changes_before_callback(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Media, 10);
            health.ReportFailure(Stage::Media, 10);
            const auto service = health.GetPendingPrompt();
            // Audio has begun; its main-task receipt is delayed behind a link event.
            health.OnWifiDisconnected();
            assert(health.GetPendingPrompt().kind == PromptKind::WifiUnavailable);
            assert(health.MarkPromptStarted(service));
            assert(health.GetPendingPrompt().kind == PromptKind::WifiUnavailable);
            health.OnWifiConnected();
            assert(health.GetPendingPrompt().kind == PromptKind::None);
        ''')

    def test_late_retirement_cannot_invalidate_a_new_attempt(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            health.BeginAttempt(Stage::Media, 10);
            health.ReportFailure(Stage::Media, 10);
            health.BeginAttempt(Stage::Media, 11);
            assert(!health.Retire(Stage::Media, 10));
            assert(health.ReportSuccess(Stage::Media, 11));
            assert(health.Retire(Stage::Media, 11));
            assert(!health.ReportSuccess(Stage::Media, 11));
        ''')

    def test_configuration_fetch_success_does_not_clear_activation_failure(self):
        self.run_driver(r'''
            using namespace ConnectionHealthTypes;
            health.SetRequiredStages(StageMask(Stage::Configuration) | StageMask(Stage::Activation));
            health.OnWifiConnected();
            health.BeginAttempt(Stage::Activation, 1);
            health.ReportFailure(Stage::Activation, 1);
            health.BeginAttempt(Stage::Configuration, 2);
            health.ReportSuccess(Stage::Configuration, 2);
            assert(health.GetServiceState() == ServiceState::Unavailable);
            assert(health.GetStageState(Stage::Activation) == StageState::Failed);
            health.BeginAttempt(Stage::Activation, 2);
            health.ReportSuccess(Stage::Activation, 2);
            assert(health.GetServiceState() == ServiceState::Available);
        ''')


if __name__ == "__main__":
    unittest.main()
