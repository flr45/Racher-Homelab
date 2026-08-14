import unittest

from system_agent import COMMANDS


class SystemAgentTests(unittest.TestCase):
    def test_only_expected_actions_are_executable(self):
        self.assertEqual(set(COMMANDS), {"restart-pdl", "restart-gateway", "reboot"})
        for argv in COMMANDS.values():
            self.assertIsInstance(argv, list)
            self.assertTrue(argv)
            self.assertNotIn("sh", argv[0])
            self.assertNotIn("bash", argv[0])


if __name__ == "__main__":
    unittest.main()
