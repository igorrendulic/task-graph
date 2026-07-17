import importlib
import subprocess
import sys
import unittest


class EvaluationModuleEntryPointTests(unittest.TestCase):
    def test_evaluation_harnesses_are_importable_from_evals(self):
        for module_name in ("evals.case_evaluator", "evals.run_case", "evals.run_controller"):
            with self.subTest(module_name=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_evaluation_module_help_is_available(self):
        for module_name in ("evals.run_case", "evals.run_controller"):
            with self.subTest(module_name=module_name):
                result = subprocess.run(
                    [sys.executable, "-m", module_name, "--help"],
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)
