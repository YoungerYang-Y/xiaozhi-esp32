"""Host regression for the production offline interaction policy."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class HomeOfflinePolicyTest(unittest.TestCase):
    def test_modes_windows_and_menu(self):
        compiler = shutil.which("g++")
        if not compiler:
            self.skipTest("host C++ compiler unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "policy.cc"
            source.write_text(r'''
#include "main/home_offline_policy.h"
#include <cassert>
int main() {
    HomeOfflinePolicy p;
    p.Start(true, 100);
    assert(p.mode() == HomeOfflinePolicy::Mode::ManualOffline);
    assert(!p.CanRecover(999999));
    p.AllowNetwork(1000);
    assert(p.CanRecover(1000));
    p.RecoveryFailed(1000);
    assert(!p.CanRecover(10999));
    assert(p.CanRecover(11000));
    for (int i=0; i<10; ++i) p.RecoveryFailed(0);
    assert(p.RetryDelayMs() == 300000);
    auto generation = p.BeginCommand(2000);
    assert(p.CommandActive());
    assert(p.AcceptCommand(generation, 9999));
    assert(!p.AcceptCommand(generation, 10000));
    p.OpenMenu();
    assert(!p.CommandActive());
    assert(p.Selected() == p.MenuSize()-1);
    p.NextMenuItem();
    assert(p.Selected() == 0);
    p.CloseMenu();
    assert(!p.AcceptCommand(generation, 3000));
    auto next = p.BeginCommand(4000);
    assert(next != generation);
    p.RecoverySucceeded();
    assert(!p.ApplyRecovery());
    p.CancelCommand();
    assert(p.ApplyRecovery());
    assert(p.mode() == HomeOfflinePolicy::Mode::Online);
    p.ManualOffline();
    p.RecoverySucceeded();
    assert(!p.ApplyRecovery());
    assert(p.mode() == HomeOfflinePolicy::Mode::ManualOffline);
}
''')
            binary = Path(tmp) / "policy"
            built = subprocess.run([compiler, "-std=c++17", "-I", str(ROOT), str(source), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            executed = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)
