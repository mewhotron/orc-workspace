import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from media_lab.errors import MediaLabError
from media_lab.probe import FFprobe, find_tool
from media_lab.render import validate_edit, build_command, render
from media_lab.render_job import run_job
from integration_support import handle_tool_error


class MultiRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = find_tool('ffmpeg')
            FFprobe()
        except MediaLabError as exc:
            handle_tool_error(exc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        sources = {}
        for name, color, size, tone in [('a','red','320x180',400),('b','blue','640x360',900)]:
            path = self.root / (name + '.mp4')
            subprocess.run([self.ffmpeg,'-v','error','-nostdin','-n','-f','lavfi','-i',
                            f'color=c={color}:s={size}:r=25:d=2','-f','lavfi','-i',
                            f'sine=frequency={tone}:sample_rate=48000:duration=2',
                            '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',
                            '-color_primaries:v','bt709','-color_trc:v','bt709','-colorspace:v','bt709',
                            '-x264-params','colorprim=bt709:transfer=bt709:colormatrix=bt709',str(path)],
                           capture_output=True,check=True,timeout=30)
            sources[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.edit = {'schema_version':2,'name':'multi-test','fps':25,'sources':sources,
                     'clips':[{'source_id':'b','in_frame':10,'out_frame':35},
                              {'source_id':'a','in_frame':0,'out_frame':25},
                              {'source_id':'b','in_frame':25,'out_frame':50}]}
        self.path = self.root / 'edit.json'

    def save(self):
        self.path.write_text(json.dumps(self.edit))

    def test_multi_job_order_duration_provenance_and_originals(self):
        self.save()
        review = self.root / 'review.json'
        review.write_text(json.dumps({'ready_to_render':True,'notes':'Synthetic blue/red/blue timeline.',
                                      'edit_sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}))
        before = {k:(Path(v['path']).read_bytes(),Path(v['path']).stat().st_mtime_ns)
                  for k,v in self.edit['sources'].items()}
        code,directory = run_job(self.path,review,variants=('landscape','vertical'),job_root=self.root/'jobs')
        self.assertEqual(code,0)
        manifest = json.loads((directory/'result.json').read_text())['manifest']
        self.assertEqual(set(manifest['sources']), {'a','b'})
        self.assertEqual(manifest['timeline'],self.edit['clips'])
        for output in manifest['outputs']:
            self.assertEqual(output['validation']['exact_video_frames'],75)
            self.assertEqual(output['validation']['complete_decode'],'passed')
            for t,channel in [(.5,2),(1.5,0),(2.5,2)]:
                pixel = subprocess.run([self.ffmpeg,'-v','error','-ss',str(t),'-i',output['path'],
                                        '-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24',
                                        '-f','rawvideo','pipe:1'],capture_output=True,check=True,timeout=30).stdout
                self.assertGreater(pixel[channel],180)
        for key,entry in self.edit['sources'].items():
            path=Path(entry['path'])
            self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),before[key])

    def test_invalid_references_duplicate_paths_and_missing_identity(self):
        for kind in ('unknown','duplicate','hash','ambiguous'):
            edit=copy.deepcopy(self.edit)
            if kind=='unknown': edit['clips'][0]['source_id']='absent'
            if kind=='duplicate': edit['sources']['b']=edit['sources']['a']
            if kind=='hash': del edit['sources']['b']['sha256']
            if kind=='ambiguous': edit['source']=edit['sources']['a']
            with self.assertRaises(MediaLabError): validate_edit(edit)

    def test_per_source_stream_mapping_and_input_order(self):
        args=build_command(self.edit,'landscape',self.root/'out.mp4',self.ffmpeg,
                           stream_indices={'a':(1,2),'b':(0,3)})
        self.assertEqual([args[i+1] for i,v in enumerate(args) if v=='-i'],
                         [self.edit['sources'][key]['path'] for key in ('b','a','b')])
        graph=args[args.index('-filter_complex')+1]
        self.assertIn('[0:3]atrim',graph)
        self.assertIn('[1:1]trim',graph)

    def test_bad_second_source_checksum_blocks_all_encoding(self):
        self.edit['sources']['b']['sha256']='0'*64
        self.save()
        with self.assertRaises(MediaLabError) as caught:
            render(self.path,self.root/'out',variants=('landscape',))
        self.assertEqual(caught.exception.code,'checksum_mismatch')
        self.assertEqual(list((self.root/'out').glob('*.mp4')),[])

    def test_bounds_checked_against_the_referenced_source(self):
        self.edit['clips'][0]['out_frame']=51
        self.save()
        with self.assertRaises(MediaLabError) as caught:
            render(self.path,self.root/'out')
        self.assertEqual(caught.exception.code,'invalid_edit')

    def test_missing_second_source_blocks_export(self):
        self.edit['sources']['b']['path']=str(self.root/'missing.mp4')
        self.save()
        with self.assertRaises((MediaLabError,OSError)):
            render(self.path,self.root/'out')
        self.assertFalse((self.root/'out').exists())


if __name__=='__main__':
    unittest.main()
