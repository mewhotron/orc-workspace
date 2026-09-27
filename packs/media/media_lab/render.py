"""Local CFR renderer: ordered cuts from one or multiple sources, original audio.

Run: python -m media_lab.render EDIT.json --output-directory media/renders
The JSON timeline uses source-frame in/out points; out is exclusive.
"""
import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .errors import MediaLabError
from .identity import fingerprint, hash_source, require_unchanged
from .probe import FFprobe, find_tool
from .publication import create_stage, prepare_bundle, publish_bundle


def edit_sources(edit):
    return {'source': edit['source']} if edit['schema_version'] == 1 else edit['sources']


def clip_source(edit, clip):
    return 'source' if edit['schema_version'] == 1 else clip['source_id']


def validate_edit(edit):
    if not isinstance(edit, dict) or type(edit.get('schema_version')) is not int or edit['schema_version'] not in (1, 2):
        raise MediaLabError('invalid_edit', 'Expected edit schema_version 1 or 2.')
    if type(edit.get('fps')) is not int or not 1 <= edit['fps'] <= 120:
        raise MediaLabError('invalid_edit', 'Use an integer frame rate from 1 to 120.')
    sources={'source': edit.get('source')} if edit['schema_version']==1 else edit.get('sources')
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 20:
        raise MediaLabError('invalid_edit', 'Provide 1 to 20 named sources.')
    if (edit['schema_version']==1 and 'sources' in edit) or (edit['schema_version']==2 and 'source' in edit):
        raise MediaLabError('invalid_edit', 'Do not mix single-source and multi-source schemas.')
    paths=set()
    for key,source in sources.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key):
            raise MediaLabError('invalid_edit', 'Use simple source IDs.')
        if not isinstance(source, dict) or not isinstance(source.get('path'), str) or not re.fullmatch(r'[0-9a-f]{64}', str(source.get('sha256', ''))):
            raise MediaLabError('invalid_edit', 'Each source needs an absolute path and SHA-256 identity.')
        if not Path(source['path']).is_absolute():
            raise MediaLabError('invalid_edit', 'The source path must be absolute.')
        path=Path(source['path']).resolve()
        if path in paths:
            raise MediaLabError('invalid_edit', 'Reuse a source ID instead of declaring the same path twice.')
        paths.add(path)
    clips=edit.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 20:
        raise MediaLabError('invalid_edit', 'Provide between 1 and 20 cuts.')
    for clip in clips:
        if not isinstance(clip, dict) or any(type(clip.get(k)) is not int for k in ('in_frame','out_frame')):
            raise MediaLabError('invalid_edit', 'Cut positions must be integer source-frame indices.')
        if not 0 <= clip['in_frame'] < clip['out_frame']:
            raise MediaLabError('invalid_edit', 'Each cut must have a non-negative start before its end.')
        if edit['schema_version']==2 and (not isinstance(clip.get('source_id'),str) or clip['source_id'] not in sources):
            raise MediaLabError('invalid_edit', 'Every cut must reference a declared source_id.')
        for key in ('vertical_center_start','vertical_center_end'):
            value=clip.get(key, .5)
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise MediaLabError('invalid_edit', 'Vertical crop centers must be finite fractions from 0 to 1.')
    if not isinstance(edit.get('name'), str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', edit['name']):
        raise MediaLabError('invalid_edit', 'Use a simple lowercase, hyphenated edit name.')
    return sum(c['out_frame']-c['in_frame'] for c in clips)


def verify_source_contract(edit, normalized, raw):
    video=normalized['video']
    fps=edit['fps']
    if video['average_frame_rate'] != str(fps) or video['nominal_frame_rate'] != str(fps):
        raise MediaLabError('unsupported_timing', 'This renderer requires source and edit frame rates to match exactly.')
    if video['start_seconds'] != 0 or normalized['container_start_seconds'] != 0:
        raise MediaLabError('unsupported_timing', 'Nonzero source start offsets are not supported by this first renderer.')
    streams=raw['streams']; stream=next(s for s in streams if s.get('index')==video['stream_index'])
    try:
        frame_count=int(stream['nb_frames'])
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaLabError('unsupported_timing', 'The source must report its video frame count.') from exc
    if any(c['out_frame'] > frame_count for c in edit['clips']):
        raise MediaLabError('invalid_edit', 'A cut extends past the end of the source.')
    if not normalized['audio_streams']:
        raise MediaLabError('unsupported_audio', 'This first renderer requires original audio.')
    if normalized['audio_streams'][0].get('start_seconds') != 0:
        raise MediaLabError('unsupported_timing', 'Selected audio must have a known zero start offset; offset correction is not supported.')
    def ratio(value):
        try:
            return Fraction(str(value).replace(':', '/'))
        except (ValueError, ZeroDivisionError):
            return None
    if (video['width']*9 != video['height']*16
            or ratio(video.get('sample_aspect_ratio')) != 1
            or ratio(video.get('display_aspect_ratio')) != Fraction(16, 9)):
        raise MediaLabError('unsupported_shape', 'Use known square-pixel 16:9 sources; automatic cropping or aspect correction is not supported.')
    if any(video.get(key) != 'bt709' for key in ('color_transfer', 'color_primaries', 'color_space')):
        raise MediaLabError('unsupported_color', 'Use explicitly tagged SDR BT.709 transfer, primaries and matrix; unknown color and HDR require a separately reviewed color pipeline.')
    return frame_count


def build_command(edit, variant, output, ffmpeg, video_stream=0, audio_stream=1, stream_indices=None):
    count=validate_edit(edit); fps=edit['fps']; duration=count/fps
    if variant not in ('landscape', 'vertical'):
        raise MediaLabError('invalid_variant', 'Choose landscape or vertical.')
    args=[ffmpeg,'-hide_banner','-nostdin','-v','warning','-n','-filter_complex_threads','2']
    filters=[]; labels=[]
    for i,clip in enumerate(edit['clips']):
        source_id=clip_source(edit,clip)
        source=edit_sources(edit)[source_id]
        vi,ai=stream_indices[source_id] if stream_indices else (video_stream,audio_stream)
        start=clip['in_frame']/fps; frames=clip['out_frame']-clip['in_frame']; length=frames/fps
        args += ['-threads','2','-ss',f'{start:.8f}','-t',f'{length:.8f}','-protocol_whitelist','file','-i',source['path']]
        if variant=='landscape':
            picture='scale=1920:1080:flags=lanczos'
        else:
            a=clip.get('vertical_center_start',.5); b=clip.get('vertical_center_end',a)
            # Keep an exact 9:16 crop, using multiples of 16x9 for chroma alignment.
            center=f'({a:.8f}+({b-a:.8f})*min(t/{length:.8f},1))'
            picture=(f"crop=w='trunc(ih/32)*18':h='trunc(ih/32)*32':"
                     f"x='max(0,min(iw-ow,iw*{center}-ow/2))':y='(ih-oh)/2',"
                     'scale=1080:1920:flags=lanczos')
        filters.append(f'[{i}:{vi}]trim=end_frame={frames},setpts=PTS-STARTPTS,{picture},setsar=1,format=yuv420p[v{i}]')
        filters.append(f'[{i}:{ai}]atrim=duration={length:.8f},asetpts=PTS-STARTPTS,'
                       f'aresample=48000:async=1:first_pts=0,apad=whole_dur={length:.8f},'
                       f'atrim=duration={length:.8f},afade=t=in:d=0.005,'
                       f'afade=t=out:st={max(0,length-.005):.8f}:d=0.005[a{i}]')
        labels += [f'[v{i}]', f'[a{i}]']
    filters.append(''.join(labels)+f'concat=n={len(edit["clips"])}:v=1:a=1[vjoin][ajoin]')
    filters.append(f'[vjoin]fps={fps}[vout]')
    filters.append(f'[ajoin]volume=-2dB,afade=t=in:d=0.02,afade=t=out:st={max(0,duration-.15):.8f}:d=0.15[aout]')
    args += ['-filter_complex',';'.join(filters),'-map','[vout]','-map','[aout]',
             '-map_metadata','-1','-map_chapters','-1','-c:v','libx264','-preset','medium','-crf','19',
             '-x264-params','colorprim=bt709:transfer=bt709:colormatrix=bt709',
             '-maxrate','16M','-bufsize','32M','-level:v','4.1','-threads','4','-pix_fmt','yuv420p','-r',str(fps),'-fps_mode','cfr',
             '-frames:v',str(count),'-t',f'{duration:.8f}','-c:a','aac','-b:a','192k','-ar','48000','-ac','2',
             '-movflags','+faststart','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',str(output)]
    return args


def validate_output(path, variant, expected_frames, fps, probe=None, run_command=None):
    probe=probe or FFprobe(); metadata,raw=probe.inspect(path)
    video=metadata['video']; expected=(1920,1080) if variant=='landscape' else (1080,1920)
    stream=next(s for s in raw['streams'] if s.get('index')==video['stream_index'])
    if (video['width'],video['height']) != expected or video['codec']!='h264':
        raise MediaLabError('render_validation', 'Output size or codec does not match the requested format.')
    if video.get('sample_aspect_ratio') != '1:1' or any(video.get(k) != 'bt709' for k in ('color_transfer','color_primaries','color_space')):
        raise MediaLabError('render_validation', 'Output must retain square pixels and explicit BT.709 color tags.')
    if video['average_frame_rate'] != str(fps) or int(stream.get('nb_frames',0)) != expected_frames:
        raise MediaLabError('render_validation', 'Output frame rate or exact frame count is wrong.')
    expected_duration=expected_frames/fps
    if abs((metadata['duration_seconds'] or 0)-expected_duration) > .05:
        raise MediaLabError('render_validation', 'Output duration is wrong.')
    audio=metadata['audio_streams']
    if len(audio)!=1 or audio[0]['codec']!='aac' or audio[0]['channels']!=2:
        raise MediaLabError('render_validation', 'Expected stereo AAC audio in the output.')
    if abs((video['start_seconds'] or 0)-(audio[0]['start_seconds'] or 0)) > .025:
        raise MediaLabError('render_validation', 'Output audio/video start times differ.')
    audio_raw=next(s for s in raw['streams'] if s.get('index')==audio[0]['stream_index'])
    if abs(float(audio_raw.get('duration',0))-expected_duration) > .05:
        raise MediaLabError('render_validation', 'Output audio duration differs from the timeline.')
    command=[find_tool('ffmpeg'),'-hide_banner','-nostdin','-v','error','-xerror',
                          '-threads','2','-protocol_whitelist','file','-i',str(path),'-map','0:v:0',
                          '-map','0:a:0','-f','null','-']
    check=(run_command(command, expected_duration) if run_command else
           subprocess.run(command,capture_output=True,text=True))
    if check.returncode:
        raise MediaLabError('render_validation', 'Output failed complete decode: '+check.stderr[-2000:])
    return {'metadata':metadata,'exact_video_frames':expected_frames,'complete_decode':'passed'}


def render(edit_path, output_directory, *, variants=('landscape','vertical'),
           on_phase=None, run_command=None):
    phase=on_phase or (lambda **event: None)
    if not variants or len(set(variants)) != len(variants) or any(v not in ('landscape','vertical') for v in variants):
        raise MediaLabError('invalid_variant', 'Choose distinct landscape and/or vertical outputs.')
    phase(phase='preflight', variant=None)
    edit_path=Path(edit_path).resolve(); edit_bytes=edit_path.read_bytes()
    edit=json.loads(edit_bytes); total=validate_edit(edit)
    sources=edit_sources(edit); inspected={}; probe=FFprobe()
    for key,entry in sources.items():
        source=Path(entry['path']).resolve(); signature=fingerprint(source)
        metadata,raw=probe.inspect(source)
        source_edit={**edit,'clips':[c for c in edit['clips'] if clip_source(edit,c)==key]}
        verify_source_contract(source_edit,metadata,raw)
        inspected[key]=(source,signature,metadata)
    destination=Path(output_directory).resolve()
    raw_root=Path(__file__).resolve().parent.parent/'media/raw'
    if destination.is_relative_to(raw_root.resolve()) or any(destination==item[0].parent for item in inspected.values()):
        raise MediaLabError('unsafe_output', 'Choose a derived-output directory separate from originals.')
    destination.mkdir(parents=True,exist_ok=True)
    bundle=destination/f'{edit["name"]}.bundle'
    legacy=[destination/f'{edit["name"]}-{v}.mp4' for v in variants]
    if bundle.exists() or (destination/f'{edit["name"]}-render.json').exists() or any(p.exists() or p.with_suffix('.partial.mp4').exists() for p in legacy):
        raise MediaLabError('output_exists', 'Use a new edit revision/name; completed or partial outputs already exist.')
    outputs={v:bundle/f'{edit["name"]}-{v}.mp4' for v in variants}
    print('Verifying source checksum before rendering...',flush=True)
    phase(phase='verifying_source', variant=None)
    for key,(source,signature,metadata) in inspected.items():
        if hash_source(source,signature)!=sources[key]['sha256']:
            raise MediaLabError('checksum_mismatch', f'Source {key} no longer matches its SHA-256 identity.')
    ffmpeg=find_tool('ffmpeg')
    version=subprocess.run([ffmpeg,'-version'],capture_output=True,text=True,check=True).stdout.splitlines()[0]
    provenance={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),
                'renderer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'edit_path':str(edit_path),'edit_sha256':hashlib.sha256(edit_bytes).hexdigest(),
                'sources':{key:{'path':str(item[0]),'sha256':sources[key]['sha256'],
                                'fingerprint':item[1]} for key,item in inspected.items()},
                'timeline':edit['clips'],'ffmpeg_version':version,'outputs':[]}
    if edit['schema_version']==1:
        provenance.update(source_path=str(source),source_sha256=edit['source']['sha256'],source_fingerprint=signature)
    stream_indices={key:(item[2]['video']['stream_index'],item[2]['audio_streams'][0]['stream_index'])
                    for key,item in inspected.items()}
    stage=create_stage(destination, edit['name'])
    for variant,final in outputs.items():
        temporary=stage/final.with_suffix('.partial.mp4').name; start=time.perf_counter()
        command=build_command(edit,variant,temporary,ffmpeg,stream_indices=stream_indices)
        print('Rendering '+variant+' from original footage...',flush=True)
        phase(phase='encoding', variant=variant)
        result=(run_command(command, total/edit['fps']) if run_command else
                subprocess.run(command,capture_output=True,text=True))
        if result.returncode:
            raise MediaLabError('render_failed', result.stderr[-4000:])
        phase(phase='validating', variant=variant)
        validation=validate_output(temporary,variant,total,edit['fps'],probe,run_command)
        for source,signature,_ in inspected.values():
            require_unchanged(source,signature)
        phase(phase='hashing_output', variant=variant)
        with temporary.open('rb') as stream: checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        provenance['outputs'].append({'variant':variant,'path':str(final),'sha256':checksum,
                                      'bytes':temporary.stat().st_size,'seconds':round(time.perf_counter()-start,3),
                                      'validation':validation,'command':command,'ffmpeg_messages':result.stderr})
        print(variant+' passed metadata, frame-count and complete-decode checks.',flush=True)
    for source,signature,_ in inspected.values():
        require_unchanged(source,signature)
    phase(phase='publishing', variant=None)
    prepare_bundle(stage, provenance)
    publish_bundle(stage, provenance, edit['name'])
    manifest=bundle/f'{edit["name"]}-render.json'
    print(json.dumps({'outputs':[str(p) for p in outputs.values()],'manifest':str(manifest)}),flush=True)
    return provenance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('edit',type=Path)
    parser.add_argument('--output-directory',type=Path,default=Path('media/renders'))
    args=parser.parse_args()
    try:
        render(args.edit,args.output_directory)
    except (MediaLabError,OSError,ValueError,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':getattr(exc,'code','render_failed'),'message':str(exc)}))
        return 1
    return 0

if __name__=='__main__':
    raise SystemExit(main())
