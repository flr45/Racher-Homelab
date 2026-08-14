import unittest

from training_routes import _as_bool


class TrainingRouteBooleanTests(unittest.TestCase):
    def test_false_like_values_are_false(self):
        for value in (False, 0, "0", "false", "False", "no", "nej", "off", ""):
            with self.subTest(value=value):
                self.assertFalse(_as_bool(value, True))

    def test_true_like_values_are_true(self):
        for value in (True, 1, "1", "true", "TRUE", "yes", "ja", "on"):
            with self.subTest(value=value):
                self.assertTrue(_as_bool(value, False))

    def test_missing_value_uses_default(self):
        self.assertTrue(_as_bool(None, True))
        self.assertFalse(_as_bool(None, False))


if __name__ == "__main__":
    unittest.main()
