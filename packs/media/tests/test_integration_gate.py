import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from integration_support import handle_tool_error
from run_required import REQUIRED, gate_passed
from media_lab.errors import MediaLabError
from media_lab.probe import find_tool


class IntegrationGateTests(unittest.TestCase):
    def test_optional_missing_tools_skip_but_required_tools_fail(self):
        error = MediaLabError('missing_dependency', 'synthetic absent dependency')
        with patch.dict(os.environ, {'MEDIA_LAB_REQUIRE_INTEGRATION':'0'}):
            with self.assertRaises(unittest.SkipTest): handle_tool_error(error)
        with patch.dict(os.environ, {'MEDIA_LAB_REQUIRE_INTEGRATION':'1'}):
            with self.assertRaises(MediaLabError): handle_tool_error(error)

    def test_other_setup_errors_are_never_skips(self):
        for code in ('invalid_tool_config','probe_failed','tool_unavailable','probe_timeout'):
            with self.subTest(code=code), self.assertRaises(MediaLabError):
                handle_tool_error(MediaLabError(code, 'synthetic setup error'))

    def test_explicit_invalid_executable_is_configuration_failure(self):
        with patch('media_lab.probe.shutil.which', return_value=None):
            with self.assertRaises(MediaLabError) as caught:
                find_tool('ffprobe', 'synthetic-missing-tool')
        self.assertEqual(caught.exception.code, 'invalid_tool_config')

    def test_required_gate_rejects_skips_missing_coverage_and_errors(self):
        def result(passed=REQUIRED, skipped=(), success=True):
            return SimpleNamespace(passed_ids=set(passed), skipped=skipped, wasSuccessful=lambda:success)
        self.assertTrue(gate_passed(result()))
        self.assertFalse(gate_passed(result(skipped=[('test','reason')])))
        self.assertFalse(gate_passed(result(passed=[])))
        self.assertFalse(gate_passed(result(success=False)))
