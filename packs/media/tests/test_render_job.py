import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_lab.errors import MediaLabError
from media_lab.render_job import Supervisor, check_review, progress_values, run_job


class JobTests(unittest.TestCase):
    def test_review_is_bound_to_exact_edit_and_requires_notes(self):
        data = b'{"edit":1}'
        review = {'edit_sha256': hashlib.sha256(data).hexdigest(),
                  'ready_to_render': True, 'notes': 'Reviewed picture and audio.'}
        check_review(data, review)
        for bad in [dict(review, ready_to_render=False), dict(review, notes=''),
                    dict(review, edit_sha256='0'*64)]:
            with self.assertRaises(MediaLabError):
                check_review(data, bad)
        with self.assertRaises(MediaLabError):
            check_review(data + b' ', review)

    def test_progress_eta_uses_media_time_and_speed_not_file_size(self):
        data = progress_values({'out_time_us': '5000000', 'speed': '2x',
                                'progress': 'continue'}, 20)
        self.assertEqual(data['media_percent'], 25)
        self.assertEqual(data['estimated_media_seconds_remaining'], 7.5)
        self.assertIsNone(progress_values({'speed': 'N/A'}, 20)['media_percent'])
        self.assertIsNone(progress_values({'out_time_us': 'nan'}, 20)['media_seconds'])
        self.assertIsNone(progress_values({'out_time_us': '1', 'speed': '0x'}, 20)
                          ['estimated_media_seconds_remaining'])

    def test_preflight_failure_is_reported_without_starting_renderer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('media_lab.render_job.render') as renderer:
                code, directory = run_job(root/'missing.json', root/'review.json', job_root=root/'jobs')
            self.assertEqual(code, 1)
            renderer.assert_not_called()
            self.assertEqual(json.loads((directory/'result.json').read_text())['status'], 'failed')
            self.assertEqual(json.loads((directory/'status.json').read_text())['status'], 'failed')

    def test_ffmpeg_end_is_not_job_completion_and_nonzero_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(Path(temp))
            supervisor.phase(phase='encoding', variant='landscape')
            child = Mock(pid=123, stdout=io.StringIO('out_time_us=1000000\nspeed=1x\nprogress=end\n'))
            child.wait.return_value = 7
            child.poll.return_value = 7
            with patch('media_lab.render_job.subprocess.Popen', return_value=child):
                result = supervisor.run(['ffmpeg', '-f', 'null', '-'], 1)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(supervisor.state['status'], 'running')
            self.assertEqual(supervisor.state['phase'], 'encoding')
            self.assertEqual(supervisor.state['progress']['media_percent'], 100)

    def test_interrupt_terminates_owned_child(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(Path(temp))
            child = Mock(pid=123, stdout=io.StringIO(''))
            child.wait.side_effect = [KeyboardInterrupt(), 0]
            child.poll.return_value = None
            with patch('media_lab.render_job.subprocess.Popen', return_value=child):
                with self.assertRaises(KeyboardInterrupt):
                    supervisor.run(['ffmpeg', 'out.mp4'], 1)
            child.terminate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
