"""Static safety checks for the B05 macOS weekday collector automation."""

from __future__ import annotations

import plistlib
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = ROOT / "automation"


class B05AutomationTests(TestCase):
    def test_launch_agent_runs_each_weekday_at_market_open_only(self) -> None:
        config = plistlib.loads(
            (AUTOMATION / "com.openalgo.hermes-v0-option-metrics.plist").read_bytes()
        )

        self.assertEqual("com.openalgo.hermes-v0-option-metrics", config["Label"])
        self.assertEqual("/bin/zsh", config["ProgramArguments"][0])
        self.assertFalse(config["KeepAlive"])
        self.assertEqual("/Users/viswatej", config["EnvironmentVariables"]["HOME"])
        schedule = config["StartCalendarInterval"]
        self.assertEqual(
            [{"Weekday": weekday, "Hour": 9, "Minute": 15} for weekday in range(1, 6)],
            schedule,
        )

    def test_launcher_uses_keychain_and_the_read_only_collector(self) -> None:
        launcher = (AUTOMATION / "run_b04_option_metrics.zsh").read_text(encoding="utf-8")

        self.assertIn("find-generic-password", launcher)
        self.assertIn("com.openalgo.hermes-v0.option-metrics", launcher)
        self.assertIn("OPENALGO_API_KEY", launcher)
        self.assertIn("HERMES_DB_HOST", launcher)
        self.assertIn("recovery --interval-seconds 5", launcher)
        self.assertNotIn(".env", launcher)
        self.assertNotIn("placeorder", launcher.lower())
        self.assertNotIn("positions", launcher.lower())
