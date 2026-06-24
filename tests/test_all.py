import importlib.util
import sys
import unittest
from pathlib import Path


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str
):
    root = Path(__file__).parent
    suite = unittest.TestSuite()

    for subdir in ["unit", "integration"]:
        directory = root / subdir
        sys.path.insert(0, str(directory))
        try:
            for test_file in sorted(directory.glob(pattern or "test*.py")):
                module_name = f"tests_{subdir}_{test_file.stem}"
                spec = importlib.util.spec_from_file_location(module_name, test_file)
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                suite.addTests(loader.loadTestsFromModule(module))
        finally:
            sys.path.pop(0)
    return suite
