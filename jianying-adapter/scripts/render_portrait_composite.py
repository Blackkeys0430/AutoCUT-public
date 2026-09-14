"""Local BiRefNet portrait alpha and frozen, silent H.264 composites.

Usage: python render_portrait_composite.py --spec job.json --mode sample|render
The JSON-only job describes source interval, a local ONNX model, canvas,
foreground placement and an optional static background image. No downloads,
Writer calls or native matting claims. Import the finished silent video through
the existing add_broll operation; keep the original A-roll audio underneath.
Optional alpha_repair keeps explicit source-normalized polygons opaque. Use it
only for subject interiors verified against source frames; pixels outside the
polygons and original RGB are unchanged. It is not global hole filling.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance


SCHEMA = 'jianying-adapter.portrait-composite.v1'
MODELS = {
    'birefnet-portrait': {'md5': 'c3a64a6abf20250d090cd055f12a3b67', 'license': 'MIT',
                        'license_source': 'https://huggingface.co/ZhengPeng7/BiRefNet-portrait', 'inference_size': [1024, 1024]},
    'u2net': {'md5': '60024c5c889badc19c04ad937298a77b', 'license': 'Apache-2.0',
              'license_source': 'https://github.com/xuebinqin/U-2-Net/blob/master/LICENSE', 'inference_size': [320, 320]},
}


def finite(value, name, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} requires a finite number')
    if low is not None and value < low or high is not None and value > high:
        raise ValueError(f'{name} outside supported range')
    return value


def local_file(value, name):
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
        raise ValueError(f'{name} must be an existing absolute local file')
    return str(Path(value).resolve())


def validate_spec(raw, mode):
    if not isinstance(raw, dict) or raw.get('schema') != SCHEMA or mode not in ('sample', 'render'):
        raise ValueError('unsupported portrait composite schema/mode')
    if set(raw)-{'output_filename', 'alpha_repair'} != {'schema', 'source', 'model', 'canvas', 'foreground', 'background', 'output_dir', 'sample_offsets_us', 'ffmpeg'}:
        raise ValueError('portrait composite requires exactly the documented job fields')
    spec = json.loads(json.dumps(raw))
    source, model, canvas, fg = (spec[name] for name in ('source', 'model', 'canvas', 'foreground'))
    if not isinstance(source, dict) or set(source) != {'path', 'start_us', 'end_us'}:
        raise ValueError('source requires path/start_us/end_us')
    source['path'] = local_file(source['path'], 'source.path')
    for key in ('start_us', 'end_us'):
        if type(source[key]) is not int or source[key] < 0:
            raise ValueError(f'source.{key} must be a nonnegative integer')
    if source['end_us'] <= source['start_us']:
        raise ValueError('source end must follow start')
    if not isinstance(model, dict) or set(model) != {'path', 'kind', 'provider'} or model['kind'] not in MODELS or model['provider'] not in ('cuda', 'cpu'):
        raise ValueError('model requires local birefnet-portrait/u2net and explicit cuda/cpu provider')
    model['path'] = local_file(model['path'], 'model.path')
    if not isinstance(canvas, dict) or set(canvas) != {'width', 'height', 'fps'}:
        raise ValueError('canvas requires width/height/fps')
    for key, limit in [('width', 4096), ('height', 4096), ('fps', 60)]:
        if type(canvas[key]) is not int or not 1 <= canvas[key] <= limit:
            raise ValueError(f'canvas.{key} invalid')
    if canvas['width'] % 2 or canvas['height'] % 2:
        raise ValueError('H.264 canvas dimensions must be even')
    if not isinstance(fg, dict) or set(fg) != {'scale', 'center_x_px', 'center_y_px'}:
        raise ValueError('foreground requires scale and full-source centre coordinates')
    finite(fg['scale'], 'foreground.scale', .01, 4)
    finite(fg['center_x_px'], 'foreground.center_x_px', -canvas['width'], 2*canvas['width'])
    finite(fg['center_y_px'], 'foreground.center_y_px', -canvas['height'], 2*canvas['height'])
    if 'alpha_repair' in spec:
        repair = spec['alpha_repair']
        if not isinstance(repair, dict) or set(repair) != {'kind', 'polygons'} or repair['kind'] != 'keep_source_polygons':
            raise ValueError('alpha_repair requires keep_source_polygons and explicit polygons')
        polygons = repair['polygons']
        if not isinstance(polygons, list) or not 1 <= len(polygons) <= 8:
            raise ValueError('alpha_repair requires 1..8 source-normalized polygons')
        for polygon in polygons:
            if not isinstance(polygon, list) or not 3 <= len(polygon) <= 32:
                raise ValueError('alpha_repair polygon requires 3..32 points')
            for point in polygon:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError('alpha_repair polygon points require x/y')
                for value in point:
                    finite(value, 'alpha_repair source-normalized coordinate', 0, 1)
            area = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(polygon, polygon[1:]+polygon[:1]))
            if abs(area) < 1e-8:
                raise ValueError('alpha_repair polygon must have nonzero area')
    bg = spec['background']
    if bg is not None:
        if not isinstance(bg, dict):
            raise ValueError('background requires an explicit local resource')
        if bg.get('kind') == 'video':
            if set(bg) != {'path', 'kind', 'start_us', 'fit', 'brightness'} or bg['fit'] != 'cover' or type(bg['start_us']) is not int or bg['start_us'] < 0:
                raise ValueError('video background requires path/start_us/cover fit/brightness')
            finite(bg['brightness'], 'background.brightness', 0, 1)
        elif set(bg) != {'path'}:
            raise ValueError('image background requires only its path')
        bg['path'] = local_file(bg['path'], 'background.path')
        if bg.get('kind') != 'video':
            with Image.open(bg['path']) as image:
                if image.size != (canvas['width'], canvas['height']):
                    raise ValueError('background must already match canvas; no implicit crop/resize')
    elif mode == 'render':
        raise ValueError('render requires the final background image')
    spec['ffmpeg'] = local_file(spec['ffmpeg'], 'ffmpeg')
    if not isinstance(spec['output_dir'], str) or not Path(spec['output_dir']).is_absolute():
        raise ValueError('output_dir must be absolute')
    output = Path(spec['output_dir']).resolve()
    spec['output_dir'] = str(output)
    filename = spec.get('output_filename', 'portrait_composite.mp4')
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith('.mp4'):
        raise ValueError('output_filename must be a single .mp4 filename')
    spec['output_filename'] = filename
    if output.exists():
        if not output.is_dir() or (mode == 'sample' and any(output.iterdir())):
            raise ValueError('sample output_dir must be new or empty')
        if mode == 'render' and any(path.name == filename or path.name.startswith(Path(filename).stem+'.') for path in output.iterdir()):
            raise ValueError('existing composite or its sidecars must not be overwritten')
    offsets = spec['sample_offsets_us']
    duration = source['end_us']-source['start_us']
    if not isinstance(offsets, list) or not offsets or len(offsets) > 12 or any(type(x) is not int or not 0 <= x < duration for x in offsets) or offsets != sorted(set(offsets)):
        raise ValueError('sample_offsets_us needs 1..12 unique increasing in-range integers')
    return spec


def hashes(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            sha.update(chunk)
            md5.update(chunk)
    return {'path': str(Path(path).resolve()), 'sha256': sha.hexdigest(), 'md5': md5.hexdigest()}


def create_session(model):
    # rembg imports pymatting, whose unrelated CPU JIT can take minutes. This
    # process uses only the established portrait predictor, never alpha_matting.
    os.environ.setdefault('NUMBA_DISABLE_JIT', '1')
    if model['provider'] == 'cuda':
        import torch  # load the installed CUDA/cuDNN DLLs before ORT
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA was requested but torch cannot access it')
    import onnxruntime as ort
    from rembg.sessions.birefnet_portrait import BiRefNetSessionPortrait
    from rembg.sessions.u2net import U2netSession

    class LocalPortraitSession(BiRefNetSessionPortrait if model['kind'] == 'birefnet-portrait' else U2netSession):
        @classmethod
        def download_models(cls, *args, **kwargs):
            return model['path']  # explicit local file; never call pooch/download

    providers = ['CPUExecutionProvider']
    if model['provider'] == 'cuda':
        providers = [('CUDAExecutionProvider', {'cudnn_conv_algo_search': 'HEURISTIC',
                      'arena_extend_strategy': 'kSameAsRequested'}), 'CPUExecutionProvider']
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    started = time.perf_counter()
    session = LocalPortraitSession(model['kind'], options, providers=providers)
    active = session.inner_session.get_providers()
    if model['provider'] == 'cuda' and active[0] != 'CUDAExecutionProvider':
        raise RuntimeError(f'requested CUDA failed to initialize; active providers: {active}')
    session.inner_session.disable_fallback()
    return session, {'active_providers': active, 'session_load_seconds': time.perf_counter()-started,
                     'onnxruntime_version': ort.__version__, 'cpu_runtime_fallback_enabled': False}


def predict_alpha(session, source):
    started = time.perf_counter()
    masks = session.predict(source)
    if len(masks) != 1 or masks[0].size != source.size:
        raise RuntimeError('portrait predictor returned incompatible mask geometry')
    alpha = masks[0].convert('L')
    data = np.asarray(alpha)
    if data.max() <= 1 or data.min() >= 254:
        raise RuntimeError('portrait alpha is empty or all foreground')
    return alpha, time.perf_counter()-started


def repair_alpha(alpha, repair=None):
    """Restore only reviewed subject interiors; never expand other contours."""
    if repair is None:
        return alpha
    keep = Image.new('L', alpha.size, 0)
    draw = ImageDraw.Draw(keep)
    for polygon in repair['polygons']:
        points = [(round(x*(alpha.width-1)), round(y*(alpha.height-1))) for x, y in polygon]
        draw.polygon(points, fill=255)
    return Image.fromarray(np.maximum(np.asarray(alpha.convert('L')), np.asarray(keep)))


def composite(source, alpha, background, foreground):
    """Premultiply before resizing to keep source background out of soft edges."""
    if source.size != alpha.size:
        raise ValueError('source/alpha dimensions differ')
    fg = source.convert('RGBA')
    fg.putalpha(alpha)
    ratio = min(background.width/source.width, background.height/source.height)*foreground['scale']
    size = (max(1, round(source.width*ratio)), max(1, round(source.height*ratio)))
    # RGBa is Pillow's premultiplied mode; resizing straight RGB contaminates
    # translucent edge pixels with the discarded source background.
    fg = fg.convert('RGBa').resize(size, Image.Resampling.LANCZOS).convert('RGBA')
    x, y = (round(foreground['center_x_px']-size[0]/2), round(foreground['center_y_px']-size[1]/2))
    if x >= background.width or y >= background.height or x+size[0] <= 0 or y+size[1] <= 0:
        raise ValueError('foreground lies completely outside canvas')
    result = background.convert('RGBA')
    result.alpha_composite(fg, (x, y))
    return result.convert('RGB')


def probe(ffmpeg, source):
    ffprobe = Path(ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    if not ffprobe.is_file():
        raise ValueError('ffprobe must be available beside ffmpeg')
    run = subprocess.run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', source], capture_output=True, check=True)
    data = json.loads(run.stdout)
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    if len(videos) != 1:
        raise ValueError('source requires exactly one video stream')
    video = videos[0]
    if video.get('tags', {}).get('rotate') or any(s.get('rotation') for s in video.get('side_data_list', [])):
        raise ValueError('rotated source requires an explicitly normalized source first')
    return data, video


def read_exact(stream, size):
    parts, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if parts:
                raise RuntimeError('truncated decoded video frame')
            return None
        parts.append(chunk)
        remaining -= len(chunk)
    return b''.join(parts)


def sample_frame(spec, offset):
    source = spec['source']
    command = [spec['ffmpeg'], '-v', 'error', '-ss', str((source['start_us']+offset)/1e6),
               '-i', source['path'], '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']
    import io
    return Image.open(io.BytesIO(subprocess.run(command, capture_output=True, check=True).stdout)).convert('RGB')


def background_command(spec, count, offset_us=0):
    bg, canvas = spec['background'], spec['canvas']
    filters = f"scale={canvas['width']}:{canvas['height']}:force_original_aspect_ratio=increase,crop={canvas['width']}:{canvas['height']},setsar=1,fps={canvas['fps']}"
    return [spec['ffmpeg'], '-v', 'error', '-ss', str((bg['start_us']+offset_us)/1e6), '-i', bg['path'],
            '-an', '-vf', filters, '-frames:v', str(count), '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']


def sample_background(spec, offset):
    bg, canvas = spec['background'], spec['canvas']
    if bg is None:
        return Image.new('RGB', (canvas['width'], canvas['height']), '#825b87')
    if bg.get('kind') != 'video':
        return Image.open(bg['path']).convert('RGB')
    raw = subprocess.run(background_command(spec, 1, offset), capture_output=True, check=True).stdout
    if len(raw) != canvas['width']*canvas['height']*3:
        raise RuntimeError('background sample is missing or truncated')
    frame = Image.frombytes('RGB', (canvas['width'], canvas['height']), raw)
    return ImageEnhance.Brightness(frame).enhance(bg['brightness'])


def run(spec, mode):
    spec = validate_spec(spec, mode)
    source_info, video = probe(spec['ffmpeg'], spec['source']['path'])
    duration_us = round(float(video.get('duration', source_info['format']['duration']))*1e6)
    if spec['source']['end_us'] > duration_us:
        raise ValueError('requested interval exceeds source duration')
    if spec['background'] and spec['background'].get('kind') == 'video':
        bg_info, bg_video = probe(spec['ffmpeg'], spec['background']['path'])
        bg_duration = round(float(bg_video.get('duration', bg_info['format']['duration']))*1e6)
        if spec['background']['start_us']+spec['source']['end_us']-spec['source']['start_us'] > bg_duration:
            raise ValueError('background video is shorter than the requested interval')
    model_identity = hashes(spec['model']['path'])
    model_info = MODELS[spec['model']['kind']]
    if model_identity['md5'] != model_info['md5']:
        raise ValueError('local ONNX does not match the established rembg model')
    session, runtime = create_session(spec['model'])
    print(json.dumps({'event': 'session_ready', **runtime}), flush=True)
    canvas = spec['canvas']
    background = sample_background(spec, 0)
    output = Path(spec['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema': SCHEMA, 'spec': spec, 'mode': mode, 'source': hashes(spec['source']['path']),
              'model': {**model_identity, **model_info},
              'background': hashes(spec['background']['path']) if spec['background'] else None,
              'runtime': runtime, 'native_editable_matting': False, 'native_visual_verified': False,
              'audio': 'none; preserve original A-roll audio when importing',
              'limitations': 'Independent frame portrait segmentation; temporal edges require playback QA. H.264 is a frozen derived composite, not editable native matting. Requested microsecond interval is sampled by FFmpeg at the declared output fps.',
              'samples': []}
    try:
        if mode == 'sample':
            sheet = Image.new('RGB', (810, 510*len(spec['sample_offsets_us'])), '#202020')
            for i, offset in enumerate(spec['sample_offsets_us']):
                source = sample_frame(spec, offset)
                alpha, elapsed = predict_alpha(session, source)
                alpha = repair_alpha(alpha, spec.get('alpha_repair'))
                result = composite(source, alpha, sample_background(spec, offset), spec['foreground'])
                rgba = source.convert('RGBA'); rgba.putalpha(alpha)
                files = {}
                for name, picture in [('source', source), ('alpha', alpha), ('rgba', rgba), ('composite', result)]:
                    path = output/f'{i+1:02d}_{offset}us_{name}.png'
                    picture.save(path); files[name] = str(path)
                for col, (name, picture) in enumerate([('source', source), ('alpha', alpha), ('composite', result)]):
                    sheet.paste(picture.convert('RGB').resize((270, 480)), (270*col, 510*i+30))
                    ImageDraw.Draw(sheet).text((270*col+8, 510*i+8), f'{offset/1e6:.3f}s {name}', fill='white')
                row = {'offset_us': offset, 'inference_seconds': elapsed, 'files': files}
                report['samples'].append(row)
                print(json.dumps({'event': 'sample', 'offset_us': offset, 'inference_seconds': elapsed}), flush=True)
            sheet.save(output/'sample_contact.jpg')
            report['contact_sheet'] = str(output/'sample_contact.jpg')
        else:
            render_video(spec, video, session, background, output, report)
        report['status'] = 'generated_pending_visual_qa'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        report_path = output/('report.json' if mode == 'sample' else Path(spec['output_filename']).stem+'.report.json')
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return report


def render_video(spec, video, session, background, output, report):
    source, canvas = spec['source'], spec['canvas']
    duration = source['end_us']-source['start_us']
    count = math.ceil(duration*canvas['fps']/1e6)
    path = output/spec['output_filename']
    prefix = Path(spec['output_filename']).stem
    decoder_cmd = [spec['ffmpeg'], '-v', 'error', '-ss', str(source['start_us']/1e6), '-i', source['path'],
                   '-an', '-vf', f"fps={canvas['fps']}", '-frames:v', str(count), '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']
    encoder_cmd = [spec['ffmpeg'], '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                   '-s', f"{canvas['width']}x{canvas['height']}", '-r', str(canvas['fps']), '-i', '-', '-an',
                   '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)]
    timings = []
    with (output/(prefix+'.decode.log')).open('wb') as decode_log, (output/(prefix+'.encode.log')).open('wb') as encode_log, (output/(prefix+'.background_decode.log')).open('wb') as bg_log:
        decoder = subprocess.Popen(decoder_cmd, stdout=subprocess.PIPE, stderr=decode_log)
        encoder = subprocess.Popen(encoder_cmd, stdin=subprocess.PIPE, stderr=encode_log)
        bg_decoder = subprocess.Popen(background_command(spec, count), stdout=subprocess.PIPE, stderr=bg_log) if spec['background'].get('kind') == 'video' else None
        try:
            for index in range(count):
                raw = read_exact(decoder.stdout, video['width']*video['height']*3)
                if raw is None:
                    raise RuntimeError(f'source ended at frame {index}, expected {count}')
                frame = Image.frombytes('RGB', (video['width'], video['height']), raw)
                alpha, elapsed = predict_alpha(session, frame)
                alpha = repair_alpha(alpha, spec.get('alpha_repair'))
                if bg_decoder:
                    bg_raw = read_exact(bg_decoder.stdout, canvas['width']*canvas['height']*3)
                    if bg_raw is None:
                        raise RuntimeError(f'background ended at frame {index}, expected {count}')
                    bg_frame = Image.frombytes('RGB', (canvas['width'], canvas['height']), bg_raw)
                    background = ImageEnhance.Brightness(bg_frame).enhance(spec['background']['brightness'])
                rendered = composite(frame, alpha, background, spec['foreground'])
                encoder.stdin.write(rendered.tobytes())
                timings.append(elapsed)
                if index in (0, count//2, count-1):
                    rendered.save(output/f'{prefix}.render_check_{index:04d}.png')
                    alpha.save(output/f'{prefix}.alpha_check_{index:04d}.png')
                if index % 15 == 0 or index == count-1:
                    print(json.dumps({'event': 'frame', 'done': index+1, 'total': count, 'inference_seconds': elapsed}), flush=True)
            encoder.stdin.close()
            if decoder.wait() != 0 or encoder.wait() != 0 or (bg_decoder and bg_decoder.wait() != 0):
                raise RuntimeError('FFmpeg failed; inspect decode.log/encode.log')
        finally:
            for process in (decoder, encoder, bg_decoder):
                if process is None:
                    continue
                if process.poll() is None:
                    process.terminate(); process.wait()
    output_info, output_video = probe(spec['ffmpeg'], str(path))
    if any(s['codec_type'] == 'audio' for s in output_info['streams']):
        raise RuntimeError('derived visual unexpectedly contains audio')
    if int(output_video.get('nb_frames', -1)) != count:
        raise RuntimeError('encoded frame count mismatch')
    report['render'] = {'path': str(path), 'sha256': hashes(path)['sha256'], 'frame_count': count,
                        'logical_duration_us': duration, 'encoded_duration_us': round(count*1e6/canvas['fps']),
                        'inference_seconds_total': sum(timings), 'inference_seconds_mean': sum(timings)/len(timings),
                        'codec': output_video['codec_name'], 'audio_stream_count': 0,
                        'source_selection': 'FFmpeg accurate seek then CFR fps filter; final frame may extend beyond logical endpoint by less than one output frame'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--mode', choices=['sample', 'render'], required=True)
    args = parser.parse_args()
    run(json.loads(args.spec.read_text('utf-8-sig')), args.mode)
