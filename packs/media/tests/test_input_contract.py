import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from integration_support import handle_tool_error
from media_lab.errors import MediaLabError
from media_lab.probe import FFprobe, find_tool, normalize
from media_lab.render import render, verify_source_contract
from media_lab.render_job import run_job


class ContractTests(unittest.TestCase):
    def test_metadata_rejection_matrix(self):
        raw = {'format': {'start_time': '0'}, 'streams': [
            {'index': 0, 'codec_type': 'video', 'width': 1920, 'height': 1080,
             'avg_frame_rate': '25/1', 'r_frame_rate': '25/1', 'start_time': '0', 'nb_frames': '50',
             'sample_aspect_ratio': '1:1', 'display_aspect_ratio': '16:9',
             'color_transfer': 'bt709', 'color_primaries': 'bt709', 'color_space': 'bt709'},
            {'index': 1, 'codec_type': 'audio', 'start_time': '0'}]}
        edit = {'fps': 25, 'clips': [{'in_frame': 0, 'out_frame': 25}]}
        self.assertEqual(verify_source_contract(edit, normalize(raw), raw), 50)
        cases = [(1, 'start_time', value, 'unsupported_timing') for value in ('0.5', '-0.1', None)]
        cases += [(0, key, value, 'unsupported_shape') for key, value in
                  [('width', 2560), ('sample_aspect_ratio', '4:3'), ('sample_aspect_ratio', None),
                   ('display_aspect_ratio', '4:3'), ('display_aspect_ratio', None)]]
        cases += [(0, key, value, 'unsupported_color') for key in ('color_transfer','color_primaries','color_space')
                  for value in (None, 'unknown', 'bt2020', 'smpte2084', 'arib-std-b67')]
        for stream, key, value, code in cases:
            with self.subTest(stream=stream, key=key, value=value):
                sample = copy.deepcopy(raw)
                sample['streams'][stream][key] = value
                with self.assertRaises(MediaLabError) as caught:
                    verify_source_contract(edit, normalize(sample), sample)
                self.assertEqual(caught.exception.code, code)


class ContractIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = find_tool('ffmpeg')
            cls.probe = FFprobe()
        except MediaLabError as exc:
            handle_tool_error(exc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def generate(self, name, *, size='320x180', sar='1', transfer='bt709', color=True, offset=False, circle=False):
        path = self.root / (name + '.mp4')
        args = [self.ffmpeg, '-v','error','-nostdin','-n','-f','lavfi','-i',f'testsrc2=size={size}:rate=25:duration=2']
        if offset: args += ['-itsoffset','0.5']
        picture = f'setsar={sar}'
        if circle:
            picture += ",geq=lum='if(lte((X-160)*(X-160)+(Y-90)*(Y-90),1600),235,16)':cb=128:cr=128"
        args += ['-f','lavfi','-i','sine=frequency=600:sample_rate=48000:duration=2',
                 '-vf',picture,'-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-t','2']
        if color: args += ['-color_primaries:v','bt709','-color_trc:v',transfer,'-colorspace:v','bt709',
                          '-x264-params',f'colorprim=bt709:transfer={transfer}:colormatrix=bt709']
        subprocess.run(args + [str(path)], capture_output=True, check=True, timeout=30)
        return path

    def edit(self, source, name):
        edit = {'schema_version':1,'name':name,'fps':25,
                'source':{'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()},
                'clips':[{'in_frame':0,'out_frame':25}]}
        path = self.root / (name + '.json')
        path.write_text(json.dumps(edit))
        review = self.root / (name + '-review.json')
        review.write_text(json.dumps({'edit_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                                     'ready_to_render':True,'notes':'Synthetic contract fixture.'}))
        return path, review

    def test_unsupported_sources_fail_before_encoding(self):
        cases = [('delayed', {'offset':True}, 'unsupported_timing'),
                 ('wide', {'size':'426x180'}, 'unsupported_shape'),
                 ('anamorphic', {'sar':'4/3'}, 'unsupported_shape'),
                 ('pq', {'transfer':'smpte2084'}, 'unsupported_color'),
                 ('hlg', {'transfer':'arib-std-b67'}, 'unsupported_color'),
                 ('unknown', {'color':False}, 'unsupported_color')]
        for name, options, error in cases:
            with self.subTest(name=name):
                source = self.generate(name, **options)
                metadata, _ = self.probe.inspect(source)
                if name == 'delayed': self.assertGreater(metadata['audio_streams'][0]['start_seconds'], 0.4)
                if name in ('pq','hlg'): self.assertEqual(metadata['video']['color_transfer'], options['transfer'])
                original = source.read_bytes()
                edit, _ = self.edit(source, name)
                encoder = Mock(side_effect=AssertionError('encoding must not start'))
                with self.assertRaises(MediaLabError) as caught:
                    render(edit, self.root/'outputs', variants=('landscape',), run_command=encoder)
                self.assertEqual(caught.exception.code, error)
                encoder.assert_not_called()
                self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.root/'outputs').exists())

    def test_supported_circle_preserves_geometry(self):
        source = self.generate('circle', circle=True)
        edit, _ = self.edit(source, 'circle')
        report = render(edit, self.root/'circle-output', variants=('landscape',))
        output = report['outputs'][0]['path']
        pixels = subprocess.run([self.ffmpeg,'-v','error','-i',output,'-frames:v','1',
                                 '-pix_fmt','gray','-f','rawvideo','pipe:1'],
                                capture_output=True, check=True, timeout=30).stdout
        self.assertEqual(len(pixels), 1920*1080)
        horizontal = sum(value > 128 for value in pixels[540*1920:541*1920])
        vertical = sum(pixels[y*1920+960] > 128 for y in range(1080))
        self.assertGreater(horizontal, 400)
        self.assertAlmostEqual(horizontal/vertical, 1, delta=.01)

    def test_publication_failure_recovers_without_encoding(self):
        source = self.generate('supported')
        original = source.read_bytes()
        edit, review = self.edit(source, 'supported')
        from media_lab.publication import write_json
        def fail_manifest(path, value):
            if path.name.endswith('-render.json'):
                raise OSError('simulated manifest failure')
            return write_json(path, value)
        with patch('media_lab.publication.write_json', side_effect=fail_manifest):
            code, failed_job = run_job(edit, review, variants=('landscape','vertical'), job_root=self.root/'jobs')
        self.assertEqual(code, 1)
        failed_result = (failed_job/'result.json').read_bytes()
        self.assertEqual(json.loads(failed_result)['status'], 'failed')
        stage = next((failed_job/'outputs').glob('*.pending'))
        self.assertEqual(list((failed_job/'outputs').glob('*.bundle')), [])
        with patch('media_lab.render.build_command', side_effect=AssertionError('must not encode')):
            code, recovered_job = run_job(edit, review, variants=('landscape','vertical'),
                                         job_root=self.root/'jobs', recovery_bundle=stage)
        self.assertEqual(code, 0)
        report = json.loads((recovered_job/'result.json').read_text())
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(len(report['manifest']['outputs']), 2)
        self.assertEqual(len(list(recovered_job.glob('*-command.json'))), 2)  # Decode checks only.
        self.assertEqual((failed_job/'result.json').read_bytes(), failed_result)
        self.assertEqual(source.read_bytes(), original)
