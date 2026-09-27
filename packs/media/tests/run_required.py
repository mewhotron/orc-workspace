"""Run all tests with mandatory real-tool integration coverage."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    'test_integration.RealMediaTests.test_h264_audio_identity_metadata_and_rename',
    'test_render.RenderIntegration.test_both_variants_decode_and_cut_order_comes_from_edit',
    'test_multi_render.MultiRenderTests.test_multi_job_order_duration_provenance_and_originals',
    'test_input_contract.ContractIntegration.test_unsupported_sources_fail_before_encoding',
    'test_input_contract.ContractIntegration.test_supported_circle_preserves_geometry',
    'test_input_contract.ContractIntegration.test_publication_failure_recovers_without_encoding',
}


class RequiredResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids = set()

    def addSuccess(self, test):
        self.passed_ids.add(test.id())
        super().addSuccess(test)


def gate_passed(result):
    return result.wasSuccessful() and not result.skipped and REQUIRED <= result.passed_ids


def main():
    os.environ['MEDIA_LAB_REQUIRE_INTEGRATION'] = '1'
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2, resultclass=RequiredResult).run(suite)
    missing = sorted(REQUIRED - result.passed_ids)
    print(f'Required gate: executed={result.testsRun}, skipped={len(result.skipped)}, missing_required={missing}')
    return 0 if gate_passed(result) else 1


if __name__ == '__main__':
    raise SystemExit(main())
