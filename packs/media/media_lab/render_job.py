"""One synchronous, locally supervised render with saved progress and a result.

Run once and wait for process exit; progress updates do not require model calls.
"""
import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .errors import MediaLabError
from .render import render, validate_edit, validate_output
from .publication import recover_bundle


JOB_ROOT = Path(__file__).resolve().parent.parent / 'local' / 'render-jobs'


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def check_review(edit_bytes, review):
    if (not isinstance(review, dict) or review.get('ready_to_render') is not True
            or review.get('edit_sha256') != hashlib.sha256(edit_bytes).hexdigest()
            or not isinstance(review.get('notes'), str) or not review['notes'].strip()):
        raise MediaLabError('review_required',
                            'Record completed editorial review for this exact edit before rendering.')


def progress_values(block, duration):
    """FFmpeg out_time_us is media time, not proof of completion or muxing phase."""
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (ValueError, TypeError):
            return None
    micros = number(block.get('out_time_us'))
    seconds = max(0, micros / 1_000_000) if micros is not None else None
    speed = number(block.get('speed', '').removesuffix('x'))
    remaining = max(0, duration - seconds) if seconds is not None else None
    return {'media_seconds': seconds,
            'media_percent': min(100, seconds / duration * 100) if seconds is not None and duration > 0 else None,
            'speed': speed,
            'estimated_media_seconds_remaining': remaining / speed if remaining is not None and speed and speed > 0 else None,
            'ffmpeg_progress': block.get('progress')}


class Supervisor:
    def __init__(self, directory):
        self.directory = directory
        self.state = {'status': 'running', 'phase': 'preflight', 'variant': None,
                      'started_at': now(), 'pid': os.getpid(), 'progress': None}
        self.command_count = 0
        self.update()

    def update(self, **fields):
        self.state.update(fields)
        self.state['updated_at'] = now()
        save_json(self.directory / 'status.json', self.state)

    def phase(self, **fields):
        self.update(**fields, progress=None)

    def run(self, command, duration):
        self.command_count += 1
        stem = f'{self.command_count:02d}-{self.state["phase"]}'
        log = self.directory / (stem + '.log')
        # Null decode output must not share stdout with the progress protocol.
        args = list(command)
        if args[-2:] == ['null', '-']:
            args[-1] = os.devnull
        args[1:1] = ['-nostats', '-stats_period', '5', '-progress', 'pipe:1']
        save_json(self.directory / (stem + '-command.json'), args)
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as stderr:
            child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr,
                                     text=True, encoding='utf-8', errors='replace')
            try:
                self.update(child_pid=child.pid, log=str(log))
                block = {}
                for line in child.stdout:
                    key, separator, value = line.strip().partition('=')
                    if separator:
                        block[key] = value
                    if key == 'progress':
                        self.update(progress=progress_values(block, duration),
                                    command_elapsed_seconds=round(time.monotonic() - started, 2))
                        block = {}
                returncode = child.wait()
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                child.stdout.close()
                self.update(child_pid=None)
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 4000))
            tail = stream.read().decode('utf-8', errors='replace')
        return subprocess.CompletedProcess(args, returncode, '', tail)


def run_job(edit_path, review_path, *, variants=('landscape',), job_root=JOB_ROOT, recovery_bundle=None):
    # A unique private directory preserves failed attempts and avoids overwrites.
    job_root = Path(job_root)
    job_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='render-', dir=job_root)).resolve()
    supervisor = Supervisor(directory)
    print(json.dumps({'job': str(directory), 'status': 'running'}), flush=True)
    report = {'job': str(directory), 'started_at': supervisor.state['started_at']}
    code = 1
    try:
        edit_bytes = Path(edit_path).read_bytes()
        review = json.loads(Path(review_path).read_text(encoding='utf-8'))
        check_review(edit_bytes, review)
        edit = json.loads(edit_bytes)
        frames = validate_edit(edit)
        # Render the reviewed bytes even if the caller changes the original JSON.
        snapshot = directory / 'edit.json'
        snapshot.write_bytes(edit_bytes)
        save_json(directory / 'review.json', review)
        report['edit_sha256'] = hashlib.sha256(edit_bytes).hexdigest()
        if recovery_bundle is None:
            report['manifest'] = render(snapshot, directory / 'outputs', variants=variants,
                                        on_phase=supervisor.phase, run_command=supervisor.run)
        else:
            report['recovery_bundle'] = str(Path(recovery_bundle).absolute())
            supervisor.phase(phase='recovering', variant=None)
            def validate(path, variant):
                supervisor.phase(phase='validating', variant=variant)
                return validate_output(path, variant, frames, edit['fps'], run_command=supervisor.run)
            report['manifest'] = recover_bundle(recovery_bundle, edit, report['edit_sha256'], validate, variants)
        report['status'] = 'completed'
        code = 0
    except KeyboardInterrupt:
        report.update(status='cancelled', error={'code': 'interrupted', 'message': 'Render interrupted.'})
        code = 130
    except Exception as exc:
        report.update(status='failed', error={'code': getattr(exc, 'code', 'render_failed'),
                                             'message': str(exc)})
    report['finished_at'] = now()
    report['last_phase'] = supervisor.state['phase']
    save_json(directory / 'result.json', report)
    supervisor.update(status=report['status'], result=str(directory / 'result.json'))
    print(json.dumps({'status': report['status'], 'result': str(directory / 'result.json')}), flush=True)
    return code, directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('edit', type=Path)
    parser.add_argument('--review-record', required=True, type=Path)
    parser.add_argument('--variant', choices=('landscape', 'vertical', 'both'), default='landscape')
    parser.add_argument('--recover-bundle', type=Path, help='Revalidate a prepared .pending or published .bundle directory without encoding.')
    args = parser.parse_args()
    variants = ('landscape', 'vertical') if args.variant == 'both' else (args.variant,)
    code, _ = run_job(args.edit, args.review_record, variants=variants, recovery_bundle=args.recover_bundle)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
