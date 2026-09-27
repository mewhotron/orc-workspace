import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_lab.errors import MediaLabError
from media_lab.publication import create_stage, prepare_bundle, publish_bundle, recover_bundle, write_json


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.destination = self.root / 'exports'
        self.destination.mkdir()
        self.name = 'test-v1'
        self.stage = create_stage(self.destination, self.name)
        self.edit = {'schema_version': 1, 'name': self.name, 'source': {'path': str(self.root / 'original.mp4')}}
        self.provenance = {'edit_sha256': 'a' * 64, 'outputs': []}
        for variant in ('landscape', 'vertical'):
            filename = f'{self.name}-{variant}.mp4'
            content = variant.encode()
            (self.stage / filename).with_suffix('.partial.mp4').write_bytes(content)
            self.provenance['outputs'].append({'variant': variant,
                'path': str(self.destination / (self.name + '.bundle') / filename),
                'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
        prepare_bundle(self.stage, self.provenance)
        self.final = self.destination / (self.name + '.bundle')

    def recover(self):
        validate = Mock()
        report = recover_bundle(self.stage, self.edit, 'a' * 64, validate, ('landscape', 'vertical'))
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(report, self.provenance)
        self.assertFalse(self.stage.exists())
        self.assertEqual(json.loads((self.final / f'{self.name}-render.json').read_text()), report)
        for output in report['outputs']:
            self.assertEqual(hashlib.sha256(Path(output['path']).read_bytes()).hexdigest(), output['sha256'])

    def fail_after_rename(self, failure_at):
        real_rename = Path.rename
        count = 0
        def interrupted(path, target):
            nonlocal count
            result = real_rename(path, target)
            if path.suffix == '.mp4':
                count += 1
                if count == failure_at:
                    raise OSError('simulated interruption after member rename')
            return result
        with patch.object(Path, 'rename', interrupted):
            with self.assertRaises(OSError):
                publish_bundle(self.stage, self.provenance, self.name)
        self.assertFalse(self.final.exists())
        self.recover()

    def test_failure_after_first_member_rename_is_recoverable(self):
        self.fail_after_rename(1)

    def test_failure_after_second_member_rename_is_recoverable(self):
        self.fail_after_rename(2)

    def test_recovery_after_promotion_does_not_change_published_files(self):
        publish_bundle(self.stage, self.provenance, self.name)
        before = {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.final.iterdir()}
        validate = Mock()
        report = recover_bundle(self.final, self.edit, 'a'*64, validate, ('landscape','vertical'))
        self.assertEqual(report, self.provenance)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual({p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.final.iterdir()}, before)

    def test_published_bundle_with_partial_member_is_rejected_unchanged(self):
        publish_bundle(self.stage, self.provenance, self.name)
        member = self.final / f'{self.name}-landscape.mp4'
        member.rename(member.with_suffix('.partial.mp4'))
        before = {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.final.iterdir()}
        validate = Mock()
        with self.assertRaises(MediaLabError) as caught:
            recover_bundle(self.final, self.edit, 'a'*64, validate, ('landscape','vertical'))
        self.assertEqual(caught.exception.code, 'invalid_bundle')
        validate.assert_not_called()
        self.assertEqual({p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.final.iterdir()}, before)

    def test_recovery_never_truncates_existing_temporary_hard_link(self):
        original = self.root / 'synthetic-original.mp4'
        original.write_bytes(b'synthetic original bytes to preserve')
        before = (original.read_bytes(), original.stat().st_mtime_ns)
        old_temporary = self.stage / f'{self.name}-render.json.tmp'
        os.link(original, old_temporary)
        self.recover()
        self.assertEqual((original.read_bytes(),original.stat().st_mtime_ns), before)
        self.assertEqual((self.final/old_temporary.name).read_bytes(), before[0])

    def test_atomic_manifest_failure_cleans_only_its_exclusive_temporary(self):
        original = self.root / 'synthetic-original.mp4'
        original.write_bytes(b'preserve original')
        old_temporary = self.stage / f'{self.name}-render.json.tmp'
        os.link(original, old_temporary)
        with patch('media_lab.publication.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                write_json(self.stage / f'{self.name}-render.json', self.provenance)
        self.assertEqual(original.read_bytes(), b'preserve original')
        self.assertTrue(old_temporary.exists())
        self.assertEqual(list(self.stage.glob('.*.tmp')), [])
        self.recover()

    def test_orphan_exclusive_manifest_temporary_is_preserved_on_recovery(self):
        orphan = self.stage / f'.{self.name}-render.json-interrupted.tmp'
        orphan.write_bytes(b'{unfinished')
        self.recover()
        self.assertEqual((self.final/orphan.name).read_bytes(), b'{unfinished')

    def test_partial_json_serialization_failure_remains_recoverable(self):
        original = self.root / 'synthetic-original.mp4'
        original.write_bytes(b'preserve original on partial serialization')
        before = (original.read_bytes(), original.stat().st_mtime_ns)
        existing = self.stage / f'{self.name}-render.json.tmp'
        os.link(original, existing)
        def interrupted_dump(value, stream, **kwargs):
            stream.write('{"outputs":[')
            stream.flush()
            # mkstemp created a new name, and bytes reached that temporary file.
            self.assertEqual(len(list(self.stage.glob('.*.tmp'))), 1)
            raise OSError('failure partway through JSON serialization')
        with patch('media_lab.publication.json.dump', side_effect=interrupted_dump):
            with self.assertRaises(OSError):
                publish_bundle(self.stage, self.provenance, self.name)
        self.assertFalse(self.final.exists())
        self.assertEqual(list(self.stage.glob('.*.tmp')), [])
        self.assertTrue(existing.exists())
        self.assertEqual((original.read_bytes(),original.stat().st_mtime_ns), before)
        self.recover()
        self.assertEqual((original.read_bytes(),original.stat().st_mtime_ns), before)

    def test_manifest_failure_keeps_prepared_evidence_and_recovers(self):
        def failed_write(path, value):
            path.with_suffix('.json.tmp').write_text('{unfinished')
            raise OSError('simulated manifest write failure')
        with patch('media_lab.publication.write_json', side_effect=failed_write):
            with self.assertRaises(OSError):
                publish_bundle(self.stage, self.provenance, self.name)
        self.assertFalse(self.final.exists())
        self.recover()

    def test_final_directory_promotion_failure_recovers(self):
        real_rename = Path.rename
        def failed(path, target):
            if path == self.stage:
                raise OSError('promotion failed')
            return real_rename(path, target)
        with patch.object(Path, 'rename', failed):
            with self.assertRaises(OSError): publish_bundle(self.stage, self.provenance, self.name)
        self.assertFalse(self.final.exists())
        self.recover()

    def test_collision_preserves_existing_bundle_and_pending_files(self):
        self.final.mkdir()
        sentinel = self.final / 'unrelated.txt'
        sentinel.write_text('preserve')
        with self.assertRaises(MediaLabError): publish_bundle(self.stage, self.provenance, self.name)
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.assertEqual(len(list(self.stage.glob('*.partial.mp4'))), 2)

    def test_recovery_rejects_changed_output(self):
        next(self.stage.glob('*.mp4')).write_bytes(b'changed')
        with self.assertRaises(MediaLabError) as caught:
            recover_bundle(self.stage, self.edit, 'a'*64, Mock(), ('landscape', 'vertical'))
        self.assertEqual(caught.exception.code, 'checksum_mismatch')
        self.assertFalse(self.final.exists())

    def test_decode_failure_blocks_recovery_publication(self):
        validate = Mock(side_effect=MediaLabError('render_validation', 'synthetic decode failure'))
        with self.assertRaises(MediaLabError):
            recover_bundle(self.stage, self.edit, 'a'*64, validate, ('landscape','vertical'))
        self.assertFalse(self.final.exists())
        self.assertTrue(self.stage.exists())

    def test_missing_prepared_record_does_not_publish(self):
        # Deliberately corrupt only this temporary fixture, never a real export.
        (self.stage/'prepared.json').write_text('{unfinished')
        with self.assertRaises(MediaLabError) as caught:
            recover_bundle(self.stage, self.edit, 'a'*64, Mock(), ('landscape','vertical'))
        self.assertEqual(caught.exception.code, 'recovery_unavailable')
        self.assertFalse(self.final.exists())

    def test_recovery_requires_matching_review_variants_and_paths(self):
        for sha, variants in [('b'*64, ('landscape','vertical')), ('a'*64, ('landscape',))]:
            with self.assertRaises(MediaLabError): recover_bundle(self.stage, self.edit, sha, Mock(), variants)
        self.provenance['outputs'][0]['path'] = str(self.root / 'original.mp4')
        prepare_bundle(self.stage, self.provenance)
        with self.assertRaises(MediaLabError): recover_bundle(self.stage, self.edit, 'a'*64, Mock(), ('landscape','vertical'))
        self.assertFalse(self.final.exists())
