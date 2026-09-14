"""Continuous workspace-only NON-NATIVE review; never a Jianying render/export."""
import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

import render_candidate_text_review as visual
import render_candidate_audio as audio
from jianying_adapter.preset_audio import audio_rows


WORKSPACE = Path(__file__).resolve().parents[2]


def workspace_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(WORKSPACE.resolve()):
        raise ValueError('review output/cache must be inside the project workspace')
    return path


def interval(value):
    if (not isinstance(value, dict) or any(type(value.get(k)) is not int for k in ('start', 'duration'))
            or value['start'] < 0 or value['duration'] <= 0):
        raise ValueError('source/target range requires nonnegative start and positive duration in microseconds')
    return value['start'], value['start'] + value['duration']


def probe(path, ffprobe):
    result = subprocess.run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, check=True)
    return json.loads(result.stdout)


def prepare(draft, ffprobe, *, layout_only=False):
    canvas = draft.get('canvas_config', {})
    size = (canvas.get('width'), canvas.get('height'))
    if size not in ((1080, 1920), (1920, 1080)):
        raise ValueError('review requires 1080x1920 or 1920x1080 canvas')
    duration = draft.get('duration')
    interval({'start': 0, 'duration': duration})
    buckets = draft.get('materials', {})
    materials = {m['id']: m for entries in buckets.values() if isinstance(entries, list)
                 for m in entries if isinstance(m, dict) and 'id' in m}
    # Reuse the imported-audio validator: authored clips may reserve a silent
    # tail beyond the decoded sound, but must still have real media and valid
    # source ranges/dependencies. The mix checks the selected source preset too.
    original_audio = audio_rows(draft)
    rows, omitted, probes = [], set(), {}
    for bucket in ('video_effects', 'transitions', 'effects', 'canvases'):
        if buckets.get(bucket):
            # These resources need Jianying; never imply our local compositing reproduces them.
            omitted.add('native ' + bucket)
    for track in draft.get('tracks', []):
        if not track.get('segments'):
            continue
        if track.get('type') not in ('video', 'text', 'audio'):
            omitted.add('native ' + str(track.get('type')) + ' track')
            continue
        # Vendor Track.export_json stores mute in attribute; our 8.8 native
        # roundtrip adapter uses flag=2 for auxiliary video, not hidden video.
        # The user-accepted subtitle_position_audition_v1 native draft includes
        # visible JIANYING-25-194 text with flag=1 and attribute=0.
        supported_flags = {'video': (0, 2), 'text': (0, 1), 'audio': (0,)}
        if (type(track.get('attribute', 0)) is not int or track.get('attribute', 0) not in (0, 1)
                or type(track.get('flag', 0)) is not int
                or track.get('flag', 0) not in supported_flags[track['type']]):
            raise ValueError('unsupported track attribute/flag')
        if track['type'] == 'text' and track.get('flag', 0) == 1:
            # One accepted native instance does not establish the flag's general
            # meaning. Render its text only in the explicit layout approximation.
            omitted.add('native text track flag 1 semantics (accepted JIANYING-25-194 reference)')
        for segment in track['segments']:
            start, end = interval(segment.get('target_timerange'))
            if end > duration:
                raise ValueError('target range exceeds candidate duration')
            if (segment.get('visible', True) is not True
                    or segment.get('track_attribute', 0) or segment.get('state', 0)):
                raise ValueError('disabled/muted tracks or segments require explicit candidate correction')
            clip = segment.get('clip') or {}
            if track['type'] != 'audio':
                position = visual.transform(segment, 0)
                if (any(type(v) not in (int, float) or not math.isfinite(v) for v in position.values())
                        or position['sx'] <= 0 or position['sy'] <= 0):
                    raise ValueError('finite position and positive scale required')
            if ((track['type'] != 'text' and clip.get('rotation', 0))
                    or any((clip.get('flip') or {}).values()) or segment.get('reverse')
                    or segment.get('speed', 1) != 1 or segment.get('keyframe_refs')):
                raise ValueError('unsupported rotation/flip/reverse/speed or unresolved keyframes')
            uniform = segment.get('uniform_scale') or {}
            if uniform.get('on') and uniform.get('value', 1) != 1:
                raise ValueError('unsupported uniform scale')
            seen = set()
            for group in segment.get('common_keyframes', []):
                kind = group.get('property_type')
                if kind == 'KFTypeTextColor':
                    omitted.add('native text colour animation')
                    continue
                if kind not in ('KFTypePositionX', 'KFTypePositionY', 'KFTypeScaleX', 'KFTypeScaleY') or kind in seen:
                    raise ValueError('unsupported/duplicate keyframe property')
                seen.add(kind)
                times = set()
                if not group.get('keyframe_list'):
                    raise ValueError('empty keyframes')
                for key in group['keyframe_list']:
                    time, values = key.get('time_offset'), key.get('values', [])
                    if (type(time) is not int or not 0 <= time <= end-start or time in times
                            or key.get('curveType', 'Line') != 'Line' or key.get('graphID')
                            or len(values) != 1 or type(values[0]) not in (int, float) or not math.isfinite(values[0])
                            or ('Scale' in kind and values[0] <= 0)):
                        raise ValueError('only finite linear keyframes inside segment supported')
                    times.add(time)
            if segment.get('animations'):
                omitted.add('native animations')
            for ref in segment.get('extra_material_refs', []):
                resource = materials.get(ref, {})
                if resource.get('animations') or resource.get('type') in ('bloom', 'video_effect'):
                    omitted.add('native animations/glow/effects')
            material = materials.get(segment.get('material_id'))
            if material is None:
                raise ValueError('missing actual material')
            if track['type'] == 'text':
                content = json.loads(material['content'])
                if any(s.get('effectStyle') for s in content.get('styles', [])):
                    omitted.add('native text effect style')
                visual.draw_text(Image.new('RGB', (4, 4)), material, visual.transform(segment, 0),
                                 text_unit_to_px=1, rotation=clip.get('rotation', 0))
            else:
                source_start, source_end = interval(segment.get('source_timerange'))
                tolerance = 2 if track['type'] == 'audio' else 0
                if abs((source_end-source_start) - (end-start)) > tolerance:
                    raise ValueError('source/target durations must match original speed')
                path = Path(material.get('path', ''))
                if not path.is_file():
                    raise ValueError('source file missing')
                if material.get('type') != 'photo':
                    info = probes.setdefault(str(path), None)
                    if info is None:
                        info = probes[str(path)] = probe(path, ffprobe)
                    kind = 'video' if track['type'] == 'video' else 'audio'
                    streams = [s for s in info['streams'] if s['codec_type'] == kind]
                    if not streams:
                        raise ValueError('source lacks required ' + kind + ' stream')
                    stream = streams[0]
                    available = float(stream.get('duration', info['format'].get('duration', 0)))
                    authored_sound = track['type'] == 'audio' and track.get('name') in original_audio
                    if source_end > round(available*1e6) and not authored_sound:
                        raise ValueError('source range exceeds actual media duration')
                    if kind == 'video' and (float(stream.get('tags', {}).get('rotate', 0))
                            or any(s.get('rotation', 0) for s in stream.get('side_data_list', []))
                            or stream.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A')):
                        raise ValueError('only upright square-pixel source video supported')
                    if not track.get('attribute', 0) and segment.get('volume', 0) > 0 and not any(s['codec_type'] == 'audio' for s in info['streams']):
                        raise ValueError('audible source lacks audio stream')
                if track['type'] == 'video':
                    visual.segment_mask(segment, buckets)
            if track['type'] != 'audio':
                rows.append({'track': track['name'] if 'name' in track else track['type'], 'kind': track['type'],
                             'segment': segment, 'material': material, 'start': start, 'end': end})
    if omitted and not layout_only:
        raise ValueError('native effects/animations unsupported; explicit --layout-only required: ' + ', '.join(sorted(omitted)))
    return size, duration, rows, sorted(omitted)


def sample_count(duration_us, fps):
    """Count sampling instants in the half-open interval [0, duration)."""
    return (duration_us * fps + 999_999) // 1_000_000


def segment_sample_index(frame_index, start_us, fps):
    # Do not round-trip through float seconds: an exact index such as 61 can
    # become 60.99999999999999 and repeat the preceding source frame.
    return (frame_index * 1_000_000 - start_us * fps) // 1_000_000


class Decoder:
    """One persistent, sequential FFmpeg decoder per source segment."""
    def __init__(self, row, ffmpeg, fps, size, log):
        source = row['segment']['source_timerange']
        self.frame_count = sample_count(source['duration'], fps)
        # Output -t rounds to the output time base: at 15 fps, 0.42 s becomes
        # six frames although the seventh sample (0.4 s) is still in range.
        # Trim *before* resampling so a rounded-up sample count cannot borrow
        # a frame from the next shot. eof_action=pass rounds the final sample
        # interval up; it does not pad an actually truncated source to our count.
        self.proc = subprocess.Popen([str(ffmpeg), '-v', 'error', '-nostdin', '-ss', str(source['start']/1e6),
            '-i', row['material']['path'], '-frames:v', str(self.frame_count), '-an',
            '-vf', f'trim=duration={source["duration"]/1e6},fps={fps}:start_time=0:eof_action=pass,scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE, stderr=log)
        self.size, self.index, self.frame = size, -1, None

    def at(self, index):
        if type(index) is not int or not 0 <= index < self.frame_count:
            raise ValueError('requested sample is outside the declared source interval')
        while self.index < index:
            length, parts = self.size[0]*self.size[1]*3, []
            while length:
                chunk = self.proc.stdout.read(length)
                if not chunk:
                    raise ValueError('source decoder ended before required timeline frame')
                parts.append(chunk)
                length -= len(chunk)
            self.frame = Image.frombytes('RGB', self.size, b''.join(parts))
            self.index += 1
        return self.frame

    def close(self):
        self.proc.stdout.close()
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait()


def render(plan, draft, ffmpeg, ffprobe, output, *, text_unit_to_px, short_edge=540, fps=30,
           layout_only=False, plan_path=None):
    visual.checked_text_unit_to_px(text_unit_to_px)
    if type(short_edge) is not int or short_edge <= 0 or short_edge % 2 or type(fps) is not int or not 1 <= fps <= 60:
        raise ValueError('positive even short_edge and integer fps in [1,60] required')
    output = workspace_path(output)
    if output.exists():
        raise ValueError('review output already exists')
    size, duration, rows, omitted = prepare(draft, ffprobe, layout_only=layout_only)
    ratio = short_edge/min(size)
    target = tuple(2*round(v*ratio/2) for v in size)
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = sample_count(duration, fps)
    with tempfile.TemporaryDirectory(prefix='continuous-review-', dir=output.parent) as temp:
        temp = Path(temp)
        mix = audio.render(plan, draft, str(ffmpeg), temp/'audio.wav', plan_path=plan_path)
        if abs(mix['duration_seconds']-duration/1e6) > 1e-6:
            raise ValueError('audio mix timeline differs from candidate duration')
        decoders, text_cache, photo_cache = {}, {}, {}
        with (temp/'ffmpeg.log').open('wb') as log:
            encoder = subprocess.Popen([str(ffmpeg), '-v', 'error', '-nostdin', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                '-s', f'{target[0]}x{target[1]}', '-r', str(fps), '-i', '-', '-i', str(temp/'audio.wav'),
                '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-frames:v', str(frames), '-movflags', '+faststart', str(temp/'review.mp4')],
                stdin=subprocess.PIPE, stderr=log)
            try:
                for frame_index in range(frames):
                    time_numerator = frame_index * 1_000_000
                    canvas = Image.new('RGB', target, '#151515')
                    for i, decoder in list(decoders.items()):
                        if time_numerator >= rows[i]['end'] * fps:
                            decoder.close()
                            del decoders[i]
                    active = [(i, r) for i, r in enumerate(rows)
                              if r['start'] * fps <= time_numerator < r['end'] * fps]
                    for i, row in sorted(active, key=lambda pair: pair[1]['segment'].get('render_index', 0)):
                        segment, material = row['segment'], row['material']
                        relative = (time_numerator - row['start'] * fps) / fps
                        position = visual.transform(segment, relative)
                        if row['kind'] == 'text':
                            key = (i, tuple(position.values()))
                            if i not in text_cache or text_cache[i][0] != key:
                                layer = Image.new('RGBA', target)
                                visual.draw_text(layer, material, position, text_unit_to_px=text_unit_to_px*ratio,
                                                 rotation=(segment.get('clip') or {}).get('rotation', 0))
                                alpha = segment.get('clip', {}).get('alpha', 1)
                                if not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
                                    raise ValueError('clip alpha must be between zero and one')
                                if alpha != 1:
                                    layer.putalpha(layer.getchannel('A').point(lambda v: round(v*alpha)))
                                text_cache[i] = (key, layer)  # one entry per text, including animated text
                            layer = text_cache[i][1]
                            canvas.paste(layer, (0, 0), layer)
                        else:
                            if material.get('type') == 'photo':
                                if i not in photo_cache:
                                    photo_cache[i] = visual.read_source_frame(material, 0, ffmpeg)
                                source = photo_cache[i]
                            else:
                                if i not in decoders:
                                    # Decode original aspect, so mask coordinates remain source-relative.
                                    info = probe(material['path'], ffprobe)
                                    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
                                    fit = min(target[0]/video['width'], target[1]/video['height'])
                                    decode_size = (max(1, round(video['width']*fit)), max(1, round(video['height']*fit)))
                                    decoders[i] = Decoder(row, ffmpeg, fps, decode_size, log)
                                source = decoders[i].at(segment_sample_index(frame_index, row['start'], fps))
                            visual.composite_video(canvas, source, position, visual.segment_mask(segment, draft['materials']),
                                                   segment.get('clip', {}).get('alpha', 1), visual.material_crop(material))
                    label = 'NON-NATIVE / FONT ESTIMATE' + (' / LAYOUT ONLY: FX OMITTED' if omitted else '')
                    ImageDraw.Draw(canvas).text((8, 8), label, fill='white', stroke_width=2, stroke_fill='black')
                    encoder.stdin.write(canvas.tobytes())
                encoder.stdin.close()
                if encoder.wait() != 0:
                    raise ValueError('continuous preview encoder failed')
            finally:
                for decoder in decoders.values():
                    decoder.close()
                if encoder.poll() is None:
                    encoder.stdin.close()
                    encoder.terminate()
                    encoder.wait()
        (temp/'review.mp4').replace(output)
    return {'ok': mix.get('ok', True), 'native_visual_verified': False, 'output': str(output), 'duration_us': duration, 'frame_count': frames,
            'fps': fps, 'size': target, 'text_unit_to_px': text_unit_to_px, 'layout_only': layout_only,
            'omitted': omitted, 'audio': mix, 'timeline': [{'track': r['track'], 'start_us': r['start'], 'end_us': r['end'],
                'source_timerange': r['segment'].get('source_timerange')} for r in rows],
            'limitations': 'NON-NATIVE continuous review. Frame boundaries quantized to fps; font units, baseline, strokes, shadows and line spacing are estimates. Listed native effects/animations omitted only in explicit layout-only mode. Not Jianying playback or visual acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'draft', 'ffmpeg', 'ffprobe', 'output', 'report'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--text-unit-to-px', required=True, type=float)
    parser.add_argument('--short-edge', default=540, type=int)
    parser.add_argument('--fps', default=30, type=int)
    parser.add_argument('--layout-only', action='store_true')
    args = parser.parse_args()
    report = workspace_path(args.report)
    if report.exists():
        raise ValueError('review report already exists')
    read = lambda p: json.loads(Path(p).read_text('utf-8-sig'))
    result = render(read(args.plan), read(args.draft), args.ffmpeg, args.ffprobe, args.output,
                    text_unit_to_px=args.text_unit_to_px, short_edge=args.short_edge, fps=args.fps,
                    layout_only=args.layout_only, plan_path=args.plan)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
