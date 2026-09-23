import json
import unittest

from railassist.config import TaskConfig
from railassist.domain.errors import ConfigError


def base_config(**overrides) -> dict:
    data = {
        "from_station": "北京南",
        "to_station": "上海虹桥",
        "dates": ["2026-09-25"],
    }
    data.update(overrides)
    return data


class ConfigValidationTests(unittest.TestCase):
    def test_minimal_config_uses_defaults(self):
        config = TaskConfig.from_dict(base_config())
        self.assertEqual(config.interval_seconds, 60)
        self.assertEqual(config.positive_jitter_ratio, 0.2)
        self.assertEqual(config.sort_mode, "default")
        self.assertIsNone(config.start_at)
        self.assertIsNone(config.stop_at)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(webhook="https://example.com"))

    def test_same_station_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(to_station="北京南"))

    def test_interval_below_floor_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(interval_seconds=29))

    def test_interval_floor_is_30(self):
        TaskConfig.from_dict(base_config(interval_seconds=30))

    def test_bad_jitter_rejected(self):
        for value in (-0.1, 1.5, "0.2"):
            with self.assertRaises(ConfigError, msg=str(value)):
                TaskConfig.from_dict(base_config(positive_jitter_ratio=value))

    def test_bad_sort_mode_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(sort_mode="cheapest"))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(start_at="2026-09-18T09:00:00"))

    def test_start_after_stop_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(
                start_at="2026-09-20T09:00:00+08:00",
                stop_at="2026-09-19T09:00:00+08:00",
            ))

    def test_valid_window_accepted(self):
        config = TaskConfig.from_dict(base_config(
            start_at="2026-09-18T09:00:00+08:00",
            stop_at="2026-09-24T22:00:00+08:00",
        ))
        self.assertEqual(config.start_at, "2026-09-18T09:00:00+08:00")

    def test_duplicate_dates_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(base_config(dates=["2026-09-25", "2026-09-25"]))

    def test_roundtrip_via_json(self):
        config = TaskConfig.from_dict(base_config())
        restored = TaskConfig.from_dict(json.loads(json.dumps(config.to_dict())))
        self.assertEqual(config, restored)

    def test_non_object_rejected(self):
        with self.assertRaises(ConfigError):
            TaskConfig.from_dict(["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
