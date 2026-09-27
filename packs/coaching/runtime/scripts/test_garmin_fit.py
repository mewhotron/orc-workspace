"""Deterministic SDK-generated FIT fixtures; no athlete data or live APIs."""

import hashlib
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from garmin_fit_sdk import CrcCalculator, Encoder, Profile

from src.ingestion.garmin_fit import GarminFitError, decode_fit_bytes, decode_fit_file


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def fixture(with_session=True):
    encoder = Encoder()
    encoder.on_mesg(Profile['mesg_num']['FILE_ID'], {
        'type': 'activity', 'manufacturer': 'development', 'product': 1,
        'time_created': START - timedelta(seconds=20),
    })
    if with_session:
        encoder.on_mesg(Profile['mesg_num']['SESSION'], {
            'start_time': START, 'timestamp': START + timedelta(seconds=100),
            'sport': 'cycling', 'sub_sport': 'road',
            'total_elapsed_time': 100.125, 'total_timer_time': 90.25,
            'total_distance': 1234.56, 'avg_power': 150,
        })
    return bytes(encoder.close())


def append_messages(content, records):
    """Append minimal protocol diagnostics the SDK encoder cannot express."""
    data = bytearray(content[:-2] + records)
    data[4:8] = (len(data) - data[0]).to_bytes(4, 'little')
    data[12:14] = CrcCalculator.calculate_crc(data, 0, 12).to_bytes(2, 'little')
    data += CrcCalculator.calculate_crc(data, 0, len(data)).to_bytes(2, 'little')
    return bytes(data)


class GarminFitTests(unittest.TestCase):
    def test_unknown_messages_and_fields_remain_explicit(self):
        # Local 15, unknown global 60000, one uint8 field, then its data.
        unknown = bytes([0x4f, 0, 0]) + (60000).to_bytes(2, 'little')
        unknown += bytes([1, 0, 1, 2, 15, 7])
        # Known file_id message with an undocumented field 250.
        unknown += bytes([0x4f, 0, 0, 0, 0, 1, 250, 1, 2, 15, 8])
        result = decode_fit_bytes(append_messages(fixture(), unknown))
        self.assertEqual(result.unknown_message_numbers, (60000,))
        self.assertEqual(result.unknown_fields, ((0, 250), (60000, 0)))
        self.assertEqual(result.messages['60000'][0][0], 7)
        self.assertEqual(result.messages['file_id_mesgs'][-1][250], 8)

    def test_unsupported_compressed_timestamp_fails_closed(self):
        with self.assertRaisesRegex(GarminFitError, '^fit_decode_failed$'):
            decode_fit_bytes(append_messages(fixture(), bytes([0x80])))

    def test_semantics_and_distinct_time_fields(self):
        result = decode_fit_bytes(fixture())
        self.assertEqual(result.messages['file_id_mesgs'][0]['type'], 'activity')
        session = result.messages['session_mesgs'][0]
        self.assertEqual(session['start_time'], START)
        self.assertEqual((session['sport'], session['sub_sport']), ('cycling', 'road'))
        self.assertAlmostEqual(session['total_distance'], 1234.56)
        self.assertAlmostEqual(session['total_elapsed_time'], 100.125)
        self.assertAlmostEqual(session['total_timer_time'], 90.25)
        self.assertNotIn('total_moving_time', session)
        self.assertEqual(session['avg_power'], 150)
        self.assertNotEqual(result.messages['file_id_mesgs'][0]['time_created'], START)

    def test_optional_messages_and_fields_are_not_fabricated(self):
        result = decode_fit_bytes(fixture(False))
        self.assertNotIn('session_mesgs', result.messages)
        self.assertNotIn('activity_mesgs', result.messages)
        self.assertNotIn('serial_number', result.messages['file_id_mesgs'][0])

    def test_determinism_and_content_identity(self):
        content = fixture()
        first = decode_fit_bytes(content)
        second = decode_fit_bytes(content)
        self.assertEqual(first, second)
        self.assertEqual(first.provenance.checksum_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(first.unknown_message_numbers, ())
        self.assertEqual(first.unknown_fields, ())
        self.assertEqual(first.developer_field_definitions, ())

    def test_file_and_archive_provenance_and_immutability(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'private-source.fit'
            content = fixture()
            path.write_bytes(content)
            original_stat = path.stat()
            result = decode_fit_file(path)
            self.assertEqual(result.provenance.original_path, path)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
            archived = decode_fit_bytes(content, source_path=path.with_suffix('.zip'),
                                        archive_member='private-member.fit',
                                        archive_checksum_sha256='a' * 64)
            self.assertEqual(archived.provenance.archive_member, 'private-member.fit')
            self.assertEqual(archived.provenance.archive_checksum_sha256, 'a' * 64)
            self.assertEqual(archived.provenance.checksum_sha256, result.provenance.checksum_sha256)
            self.assertNotIn('private', repr(archived))
            self.assertNotIn('private', repr(archived.provenance))
            self.assertNotIn('session_mesgs', repr(archived))

    def test_malformed_crc_and_trailing_bytes(self):
        corrupt = bytearray(fixture())
        corrupt[-1] ^= 1
        for content in (b'', b'private invalid data', fixture()[:10],
                        fixture()[:-1], bytes(corrupt), fixture() + b'extra'):
            with self.subTest(size=len(content)), self.assertRaises(GarminFitError):
                decode_fit_bytes(content)

    def test_crc_valid_but_invalid_message_fails(self):
        corrupt = bytearray(fixture())
        corrupt[14] = 15  # Data referencing an undefined local message.
        corrupt[-2:] = CrcCalculator.calculate_crc(corrupt, 0, len(corrupt) - 2).to_bytes(2, 'little')
        with self.assertRaisesRegex(GarminFitError, '^fit_decode_failed$'):
            decode_fit_bytes(bytes(corrupt))

    def test_sdk_errors_discard_partial_results_and_private_details(self):
        for failure in ('returned', 'raised'):
            options = ({'return_value': ({'private_payload': []}, [ValueError('secret')])}
                       if failure == 'returned' else {'side_effect': ValueError('secret')})
            output = io.StringIO()
            with patch('src.ingestion.garmin_fit.Decoder.read', **options), redirect_stdout(output), redirect_stderr(output):
                with self.assertRaisesRegex(GarminFitError, '^fit_decode_failed$') as caught:
                    decode_fit_bytes(fixture())
            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(output.getvalue(), '')

    def test_read_errors_are_private(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(GarminFitError, '^fit_source_read_failed$'):
                decode_fit_file(Path(temporary) / 'private-missing.fit')

    def test_archive_provenance_validation(self):
        for kwargs in ({'archive_member': 'member.fit'},
                       {'archive_checksum_sha256': 'a' * 64},
                       {'archive_member': 'member.fit', 'archive_checksum_sha256': 'invalid'},
                       {'archive_member': 'member.fit', 'archive_checksum_sha256': 'a' * 64}):
            with self.assertRaises(GarminFitError):
                decode_fit_bytes(fixture(), **kwargs)


if __name__ == '__main__':
    unittest.main()
