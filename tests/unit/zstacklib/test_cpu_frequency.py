import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'zstacklib'))
from zstacklib.utils.cpu_frequency import get_static_cpu_ghz


class CpuFrequencyTest(unittest.TestCase):
    def test_model_frequency_takes_precedence(self):
        self.assertEqual('2.10', get_static_cpu_ghz(
            'Intel(R) Xeon(R) CPU E5-2620 v4 @ 2.10GHz', '3100 MHz\n3100 MHz'))

    def test_hygon_uses_consistent_socket_frequency(self):
        self.assertEqual('2.70', get_static_cpu_ghz(
            'Hygon C86-4G (OPN:7490)', '2700 MHz\n2700 MHz'))

    def test_invalid_model_frequency_uses_dmi(self):
        self.assertEqual('2.70', get_static_cpu_ghz('Invalid CPU @ 0GHz', '2700 MHz'))

    def test_untrustworthy_dmi_does_not_publish_frequency(self):
        for value in ('', 'Unknown', '2700 MHz\nUnknown', '2700 MHz\n3100 MHz',
                      '0 MHz', '-2700 MHz', 'invalid'):
            self.assertIsNone(get_static_cpu_ghz('Hygon C86-4G (OPN:7490)', value), value)

    def test_small_firmware_rounding_difference(self):
        self.assertEqual('2.70', get_static_cpu_ghz('Hygon', '2700 MHz\n2701 MHz'))


if __name__ == '__main__':
    unittest.main()
