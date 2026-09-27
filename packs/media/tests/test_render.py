import copy
import hashlib
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

from media_lab.errors import MediaLabError
from media_lab.probe import FFprobe,find_tool
from media_lab.render import validate_edit,verify_source_contract,build_command,render
from integration_support import handle_tool_error


def edit_for(path):
    return {'schema_version':1,'name':'synthetic-v1','fps':25,
            'source':{'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
            'clips':[{'in_frame':20,'out_frame':30},{'in_frame':0,'out_frame':15}]}


class EditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'sample.mp4';self.source.write_bytes(b'unit fixture')
        self.edit=edit_for(self.source)

    def test_frame_ranges_determine_exact_total(self):
        self.assertEqual(validate_edit(self.edit),25)

    def test_invalid_ranges_and_nonfinite_crop_are_rejected(self):
        for change in [{'in_frame':-1},{'out_frame':20},{'in_frame':20.5},
                       {'vertical_center_start':float('nan')},{'vertical_center_end':1.1}]:
            edit=copy.deepcopy(self.edit);edit['clips'][0].update(change)
            with self.assertRaises(MediaLabError):validate_edit(edit)

    def test_bad_identity_and_path_are_rejected(self):
        for change in [{'sha256':'unknown'},{'path':'relative.mp4'}]:
            edit=copy.deepcopy(self.edit);edit['source'].update(change)
            with self.assertRaises(MediaLabError):validate_edit(edit)

    def test_empty_timeline_and_unsafe_output_name_rejected(self):
        for key,value in [('clips',[]),('name','../source'),('fps',True)]:
            edit=copy.deepcopy(self.edit);edit[key]=value
            with self.assertRaises(MediaLabError):validate_edit(edit)

    def test_render_uses_structured_source_inputs(self):
        args=build_command(self.edit,'vertical',Path('out.mp4'),'ffmpeg')
        self.assertEqual([args[i+1] for i,s in enumerate(args) if s=='-i'],[str(self.source)]*2)
        self.assertIn('-n',args)
        self.assertEqual(args[args.index('-frames:v')+1],'25')
        self.assertIn('crop=',args[args.index('-filter_complex')+1])

    def test_source_contract_checks_bounds_and_offsets(self):
        meta={'video':{'average_frame_rate':'25','nominal_frame_rate':'25','start_seconds':0,
                        'stream_index':0,'width':1920,'height':1080,
                        'sample_aspect_ratio':'1:1','display_aspect_ratio':'16:9',
                        'color_transfer':'bt709','color_primaries':'bt709','color_space':'bt709'},
              'container_start_seconds':0,'audio_streams':[{'stream_index':1,'start_seconds':0}]}
        raw={'streams':[{'index':0,'nb_frames':'50'}]}
        self.assertEqual(verify_source_contract(self.edit,meta,raw),50)
        for frames,start in [('24',0),('50',1)]:
            raw['streams'][0]['nb_frames']=frames;meta['video']['start_seconds']=start
            with self.assertRaises(MediaLabError):verify_source_contract(self.edit,meta,raw)


class RenderIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:cls.ffmpeg=find_tool('ffmpeg');cls.probe=FFprobe()
        except MediaLabError as exc:handle_tool_error(exc)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.mp4'
        subprocess.run([self.ffmpeg,'-nostdin','-v','error','-n','-f','lavfi','-i',
                        'testsrc2=size=320x180:rate=25:duration=2','-f','lavfi','-i',
                        'sine=frequency=600:sample_rate=48000:duration=2','-c:v','libx264',
                        '-threads','1','-pix_fmt','yuv420p','-c:a','aac',
                        '-color_primaries:v','bt709','-color_trc:v','bt709','-colorspace:v','bt709',
                        '-x264-params','colorprim=bt709:transfer=bt709:colormatrix=bt709',str(self.source)],
                       capture_output=True,check=True,timeout=30)
        self.edit=edit_for(self.source);self.edit_path=self.root/'edit.json'
        self.edit_path.write_text(json.dumps(self.edit))

    def frame(self,path,second):
        return subprocess.run([self.ffmpeg,'-nostdin','-v','error','-ss',str(second),'-i',str(path),
                               '-map','0:v:0','-frames:v','1','-vf','scale=64:36','-pix_fmt','gray',
                               '-f','rawvideo','pipe:1'],capture_output=True,check=True,timeout=30).stdout

    def test_both_variants_decode_and_cut_order_comes_from_edit(self):
        before=self.source.stat();original=self.source.read_bytes()
        report=render(self.edit_path,self.root/'renders')
        self.assertEqual(len(report['outputs']),2)
        for output in report['outputs']:
            self.assertEqual(output['validation']['exact_video_frames'],25)
            self.assertEqual(output['validation']['complete_decode'],'passed')
        landscape=Path(report['outputs'][0]['path'])
        def error(a,b):return sum((x-y)**2 for x,y in zip(a,b))/len(a)
        actual=self.frame(landscape,0);correct=self.frame(self.source,.8);wrong=self.frame(self.source,0)
        self.assertLess(error(actual,correct),error(actual,wrong)/2)
        second=self.frame(landscape,.4)
        self.assertLess(error(second,wrong),error(second,correct)/2)
        self.assertEqual(self.source.read_bytes(),original)
        self.assertEqual(self.source.stat().st_mtime_ns,before.st_mtime_ns)
        with self.assertRaises(MediaLabError) as failed:render(self.edit_path,self.root/'renders')
        self.assertEqual(failed.exception.code,'output_exists')

    def test_wrong_checksum_never_produces_a_render(self):
        self.edit['source']['sha256']='0'*64;self.edit_path.write_text(json.dumps(self.edit))
        with self.assertRaises(MediaLabError) as failed:render(self.edit_path,self.root/'renders')
        self.assertEqual(failed.exception.code,'checksum_mismatch')
        self.assertEqual(list((self.root/'renders').glob('*.mp4')),[])

    def test_supervised_job_validates_landscape_and_saves_result(self):
        from media_lab.render_job import run_job
        review=self.root/'review.json'
        review.write_text(json.dumps({'edit_sha256':hashlib.sha256(self.edit_path.read_bytes()).hexdigest(),
                                      'ready_to_render':True,'notes':'Synthetic cut order reviewed.'}))
        code,directory=run_job(self.edit_path,review,job_root=self.root/'jobs')
        self.assertEqual(code,0)
        status=json.loads((directory/'status.json').read_text())
        result=json.loads((directory/'result.json').read_text())
        self.assertEqual(status['status'],'completed')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['manifest']['outputs']),1)
        output=result['manifest']['outputs'][0]
        self.assertEqual(output['variant'],'landscape')
        self.assertEqual(output['validation']['complete_decode'],'passed')
        self.assertEqual(output['validation']['exact_video_frames'],25)
        self.assertTrue(Path(output['path']).exists())
        self.assertEqual((directory/'edit.json').read_bytes(),self.edit_path.read_bytes())
        self.assertEqual(len(list(directory.glob('*-command.json'))),2)

if __name__=='__main__':unittest.main()
