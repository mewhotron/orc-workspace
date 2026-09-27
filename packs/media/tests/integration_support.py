"""Optional local tests may skip absent tools; release checks may not."""
import os
import unittest


def handle_tool_error(error):
    if error.code == 'missing_dependency' and os.environ.get('MEDIA_LAB_REQUIRE_INTEGRATION') != '1':
        raise unittest.SkipTest(str(error)) from error
    raise error
