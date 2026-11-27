"""Exercise reminder episodes independently of uncalibrated voltage thresholds."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LowBatteryReminderTests(unittest.TestCase):
    def run_driver(self, body):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or not shutil.which(compiler[0]):
            self.skipTest("requires a host C++ compiler")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reminder.cc"
            source.write_text(
                '#include <cassert>\n#include "main/low_battery_reminder.h"\n'
                'int main() { LowBatteryReminder reminder;\n' + body + '\n}\n',
                encoding="utf-8",
            )
            binary = Path(directory) / "reminder"
            result = subprocess.run(compiler + ["-std=c++23", "-I", str(ROOT), str(source),
                                                "-o", str(binary)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pending_survives_unknown_and_only_plays_with_a_current_low_reading(self):
        self.run_driver(r'''
            reminder.Observe({true, BatteryState::Unknown, BatteryBand::Unknown});
            assert(reminder.PendingEpisode() == 0);
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            const auto episode = reminder.PendingEpisode();
            assert(episode != 0 && reminder.HasPending());
            reminder.Observe({true, BatteryState::Unknown, BatteryBand::Unknown});
            assert(reminder.PendingEpisode() == 0 && reminder.HasPending());
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            assert(reminder.PendingEpisode() == episode);
        ''')

    def test_started_episode_is_not_replayed_until_stable_recovery(self):
        self.run_driver(r'''
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            const auto first = reminder.PendingEpisode();
            assert(!reminder.MarkStarted(0));
            assert(reminder.MarkStarted(first));
            assert(!reminder.MarkStarted(first));
            assert(!reminder.HasPending());
            reminder.Observe({true, BatteryState::Unknown, BatteryBand::Unknown});
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            assert(reminder.PendingEpisode() == 0);
            reminder.Observe({true, BatteryState::Normal, BatteryBand::TwoBars});
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            const auto second = reminder.PendingEpisode();
            assert(second != 0 && second != first);
            assert(!reminder.MarkStarted(first));
            assert(reminder.PendingEpisode() == second);
        ''')

    def test_uncalibrated_or_inconsistent_reading_cannot_trigger_or_clear_an_episode(self):
        self.run_driver(r'''
            reminder.Observe({true, BatteryState::Unclassified, BatteryBand::OneBar});
            assert(!reminder.HasPending());
            reminder.Observe({false, BatteryState::Low, BatteryBand::OneBar});
            assert(!reminder.HasPending());
            reminder.Observe({true, BatteryState::Low, BatteryBand::Unknown});
            assert(!reminder.HasPending());
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            const auto episode = reminder.PendingEpisode();
            reminder.Observe({true, BatteryState::Unclassified, BatteryBand::ThreeBars});
            assert(reminder.HasPending() && reminder.PendingEpisode() == 0);
            reminder.Observe({true, BatteryState::Normal, BatteryBand::Unknown});
            assert(reminder.HasPending());
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            assert(reminder.PendingEpisode() == episode);
        ''')

    def test_delayed_started_receipt_survives_a_transient_unknown_reading(self):
        self.run_driver(r'''
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            const auto episode = reminder.PendingEpisode();
            // Output began before this reading and the main-task receipt is delayed.
            reminder.Observe({true, BatteryState::Unknown, BatteryBand::Unknown});
            assert(reminder.MarkStarted(episode));
            reminder.Observe({true, BatteryState::Low, BatteryBand::OneBar});
            assert(!reminder.HasPending());
        ''')
