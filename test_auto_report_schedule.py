import unittest

import bot
import config


class RecordingJobQueue:
    def __init__(self):
        self.calls = []

    def run_daily(self, callback, **kwargs):
        self.calls.append((callback, kwargs))


class AutoReportScheduleTests(unittest.TestCase):
    def test_uses_kyiv_time_and_delayed_run_protection(self):
        queue = RecordingJobQueue()

        bot.register_auto_report_jobs(queue)

        self.assertEqual(len(queue.calls), 2)
        evening_callback, evening = queue.calls[0]
        longterm_callback, longterm = queue.calls[1]
        self.assertIs(evening_callback, bot.auto_scan_job)
        self.assertIs(longterm_callback, bot.auto_longterm_job)
        self.assertEqual(evening["time"].hour, 20)
        self.assertEqual(longterm["time"].hour, 22)
        self.assertEqual(evening["time"].tzinfo.key, "Europe/Kyiv")
        self.assertEqual(longterm["time"].tzinfo.key, "Europe/Kyiv")
        self.assertEqual(evening["job_kwargs"], {
            "misfire_grace_time": 7200,
            "coalesce": True,
            "max_instances": 1,
        })
        self.assertEqual(longterm["job_kwargs"], evening["job_kwargs"])

    def test_constants_and_telegram_settings_text_match(self):
        self.assertEqual(config.EVENING_REPORT_HOUR_LOCAL, 20)
        self.assertEqual(config.LONGTERM_REPORT_HOUR_LOCAL, 22)
        self.assertEqual(config.REPORT_JOB_MISFIRE_GRACE_SECONDS, 7200)
        self.assertIn("Вечерний отчёт 20:00 Киев", bot.settings_text())
        self.assertIn("Longterm отчёт 22:00 Киев", bot.settings_text())


if __name__ == "__main__":
    unittest.main()
