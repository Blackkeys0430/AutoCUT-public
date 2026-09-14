"""A real decode/composite/mux fixture for the non-native continuous review."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_candidate_video_review as review
from test_preset_audio import existing_four_item_preset


@pytest.fixture(scope='module')
def fixture_media(tmp_path_factory):
    ffmpeg = os.environ.get('REVIEW_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('REVIEW_FFPROBE') or shutil.which('ffprobe')
    font = Path('C:/Windows/Fonts/arial.ttf')
    if not ffmpeg or not ffprobe or not font.is_file():
        pytest.skip('real FFmpeg/ffprobe and fixture font required')
    folder = tmp_path_factory.mktemp('continuous')
    # The test invocation supplies a workspace basetemp, just as production does.
    review.workspace_path(folder)
    base, broll = folder/'base.mp4', folder/'broll.mp4'
    def run(args):
        subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', *args], check=True, capture_output=True)
    run(['-f', 'lavfi', '-i', 'color=red:s=320x180:r=10:d=1',
         '-f', 'lavfi', '-i', 'color=blue:s=320x180:r=10:d=1',
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
         '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
         '-map', '[v]', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(base)])
    run(['-f', 'lavfi', '-i', 'color=green:s=320x180:r=10:d=1',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(broll)])
    def segment(mat, start, duration, source=0, **values):
        return dict(material_id=mat, target_timerange=dict(start=start, duration=duration),
                    source_timerange=dict(start=source, duration=duration), speed=1, volume=0,
                    **values)
    first = segment('base', 0, 500000)
    first['volume'] = .5
    second = segment('base', 500000, 500000, source=1000000)
    second['volume'] = .5
    detail = segment('detail', 200000, 300000, render_index=1,
                     clip={'scale': {'x': .4, 'y': .4}, 'transform': {'x': .5}})
    # Native mute must keep auxiliary video visible and suppress its audio,
    # even with a positive segment gain and a source without an audio stream.
    detail['volume'] = 1
    text = segment('caption', 500000, 500000, render_index=2, clip={'transform': {'y': -.65}})
    draft = {'duration': 1000000, 'canvas_config': {'width': 1920, 'height': 1080},
             'materials': {'videos': [{'id': 'base', 'type': 'video', 'path': str(base)},
                                      {'id': 'detail', 'type': 'video', 'path': str(broll)}],
                           'texts': [{'id': 'caption', 'content': json.dumps({'text': 'TEST', 'styles': [
                               {'range': [0, 4], 'size': 40, 'font': {'path': str(font)}}]})}]},
             'tracks': [{'type': 'video', 'name': 'base', 'segments': [first, second]},
                        {'type': 'video', 'name': 'broll', 'attribute': 1, 'flag': 2, 'segments': [detail]},
                        {'type': 'text', 'name': 'captions', 'segments': [text]}]}
    return folder, str(ffmpeg), str(ffprobe), draft


def test_continuous_source_trim_broll_text_and_muxed_audio(fixture_media, monkeypatch):
    folder, ffmpeg, ffprobe, draft = fixture_media
    constructed = []
    original = review.Decoder
    class CountingDecoder(original):
        def __init__(self, row, *args):
            constructed.append(row['track'])
            super().__init__(row, *args)
    monkeypatch.setattr(review, 'Decoder', CountingDecoder)
    output = folder/'review.mp4'
    result = review.render({}, draft, ffmpeg, ffprobe, output, text_unit_to_px=5, short_edge=180, fps=10)
    assert constructed == ['base', 'broll', 'base']  # per segment, never per frame
    info = review.probe(output, ffprobe)
    assert float(info['format']['duration']) == pytest.approx(1, abs=.025)
    assert result['frame_count'] == 10 and result['native_visual_verified'] is False
    assert result['audio']['audio_sources'] == 2
    raw = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-an', '-f', 'rawvideo',
                          '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape((-1, 180, 320, 3))
    assert len(frames) == 10
    assert frames[1, 90, 240, 0] > 200 and frames[1, 90, 240, 1] < 30
    assert frames[3, 90, 240, 1] > 90 and frames[3, 90, 240, 0] < 30
    assert frames[6, 90, 240, 2] > 200 and frames[6, 90, 240, 0] < 30
    # Caption appears only after the cut; ignore the permanent top review label.
    assert np.all(frames[3, 120:175, 90:230].min(axis=2) < 100)
    assert np.count_nonzero(frames[6, 120:175, 90:230].min(axis=2) > 200) > 50
    sound = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-vn', '-ac', '1', '-ar', '48000',
                            '-f', 'f32le', '-'], check=True, capture_output=True).stdout
    samples = np.frombuffer(sound, dtype='<f4')
    for chunk in (samples[2400:21600], samples[26400:45600]):
        assert .02 < np.sqrt(np.mean(chunk**2)) < .06
        peak = np.argmax(np.abs(np.fft.rfft(chunk))) * 48000 / len(chunk)
        assert peak == pytest.approx(440, abs=3)


def test_segment_sampling_uses_exact_integer_grid():
    assert [review.segment_sample_index(i, 0, 15) for i in range(1000)] == list(range(1000))
    assert review.sample_count(58_820_000, 15) == 883
    # The first global sample after this cut is 58.866... s, local index zero.
    assert review.segment_sample_index(883, 58_820_000, 15) == 0


@pytest.mark.parametrize('duration', [420_000, 460_000, 600_000])
def test_fractional_segment_last_sample_survives_decode_and_mux(fixture_media, duration):
    folder, ffmpeg, ffprobe, template = fixture_media
    draft = copy.deepcopy(template)
    draft['duration'] = duration
    draft['tracks'] = [draft['tracks'][0]]
    segment = draft['tracks'][0]['segments'][0]
    draft['tracks'][0]['segments'] = [segment]
    segment['target_timerange']['duration'] = duration
    segment['source_timerange']['duration'] = duration
    output = folder / f'fractional-{duration}.mp4'
    result = review.render({}, draft, ffmpeg, ffprobe, output,
                           text_unit_to_px=5, short_edge=90, fps=15)
    video = next(s for s in review.probe(output, ffprobe)['streams'] if s['codec_type'] == 'video')
    expected = review.sample_count(duration, 15)
    assert result['frame_count'] == int(video['nb_frames']) == expected
    assert 0 <= float(video['duration']) - duration / 1e6 <= 1 / 15


def test_decoder_does_not_borrow_next_shot_or_pad_missing_source(fixture_media):
    folder, ffmpeg, _, draft = fixture_media
    source = draft['materials']['videos'][0]
    # The next shot turns blue at 1 s. Rounding only the output duration up
    # would allow fps resampling to borrow that blue frame across a .99 s cut.
    row = {'material': source, 'segment': {'source_timerange': {'start': 0, 'duration': 990_000}}}
    with (folder / 'fractional-decoder.log').open('wb') as log:
        decoder = review.Decoder(row, ffmpeg, 15, (32, 18), log)
        try:
            last = np.asarray(decoder.at(14))
            assert last[9, 16, 0] > 200 and last[9, 16, 2] < 30
            with pytest.raises(ValueError, match='outside the declared source interval'):
                decoder.at(15)
        finally:
            decoder.close()
        # A real EOF well before the declared end remains an error. There is
        # no arbitrary reuse of the last decoded frame to satisfy a bad range.
        row['segment']['source_timerange']['duration'] = 3_000_000
        decoder = review.Decoder(row, ffmpeg, 15, (32, 18), log)
        try:
            with pytest.raises(ValueError, match='decoder ended before required'):
                decoder.at(44)
        finally:
            decoder.close()


def test_native_animation_refusal_and_explicit_omission(fixture_media):
    _, _, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    draft['materials']['material_animations'] = [{'id': 'intro', 'animations': [{'name': 'native'}]}]
    draft['tracks'][2]['segments'][0]['extra_material_refs'] = ['intro']
    with pytest.raises(ValueError, match='layout-only'):
        review.prepare(draft, ffprobe)
    assert 'native animations/glow/effects' in review.prepare(draft, ffprobe, layout_only=True)[3]


def test_native_audio_clip_none_is_mixed_at_its_actual_timeline_time(fixture_media):
    folder, ffmpeg, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    draft['tracks'][0]['attribute'] = 1
    audio_path = draft['materials']['videos'][0]['path']
    draft['materials']['audios'] = [{'id': 'native-audio', 'type': 'extract_music', 'path': audio_path}]
    # AudioSegment.export_json has clip=None: audio has no visual transform.
    draft['tracks'].append({'type': 'audio', 'name': 'audition', 'attribute': 0, 'flag': 0,
        'segments': [{'material_id': 'native-audio', 'clip': None, 'hdr_settings': None,
            'target_timerange': {'start': 250000, 'duration': 500000},
            'source_timerange': {'start': 0, 'duration': 500000},
            'common_keyframes': [], 'keyframe_refs': [], 'speed': 1, 'volume': .5}]})
    output = folder/'native-audio.mp4'
    result = review.render({}, draft, ffmpeg, ffprobe, output, text_unit_to_px=5, short_edge=180, fps=10)
    assert result['audio']['audio_sources'] == 1
    assert all(row['track'] != 'audition' for row in result['timeline'])
    sound = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-vn', '-ac', '1', '-ar', '48000',
                            '-f', 'f32le', '-'], check=True, capture_output=True).stdout
    samples = np.frombuffer(sound, dtype='<f4')
    assert np.sqrt(np.mean(samples[2400:9600]**2)) < .001
    assert .02 < np.sqrt(np.mean(samples[16800:31200]**2)) < .06
    assert np.sqrt(np.mean(samples[38400:45600]**2)) < .001


def test_fixed_text_rotation_is_supported_but_video_rotation_is_not(fixture_media):
    _, _, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    draft['tracks'][2]['segments'][0]['clip']['rotation'] = -10
    review.prepare(draft, ffprobe)
    # Real accepted JIANYING-25-194 text track flag; not the auxiliary-video bit.
    draft['tracks'][2]['flag'] = 1
    with pytest.raises(ValueError, match='layout-only'):
        review.prepare(draft, ffprobe)
    assert any('text track flag 1 semantics' in item
               for item in review.prepare(draft, ffprobe, layout_only=True)[3])
    draft['tracks'][1]['segments'][0]['clip']['rotation'] = -10
    with pytest.raises(ValueError, match='rotation'):
        review.prepare(draft, ffprobe)


def test_real_linear_transform_and_unsupported_speed(fixture_media):
    _, _, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    seg = draft['tracks'][1]['segments'][0]
    seg['common_keyframes'] = [{'property_type': 'KFTypePositionX', 'keyframe_list': [
        {'time_offset': 0, 'values': [0], 'curveType': 'Line'},
        {'time_offset': 300000, 'values': [.6], 'curveType': 'Line'}]}]
    review.prepare(draft, ffprobe)
    assert review.visual.transform(seg, 150000)['x'] == pytest.approx(.3)
    seg['speed'] = .5
    with pytest.raises(ValueError, match='speed'):
        review.prepare(draft, ffprobe)


@pytest.mark.parametrize('field,value', [('attribute', 2), ('flag', 1), ('flag', 4)])
def test_unknown_track_state_is_not_silently_ignored(fixture_media, field, value):
    _, _, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    draft['tracks'][1][field] = value
    with pytest.raises(ValueError, match='track attribute/flag'):
        review.prepare(draft, ffprobe)


@pytest.mark.parametrize('track_type,delta,accepted', [
    ('audio', 1, True), ('audio', 2, True), ('audio', 3, False), ('video', 1, False),
])
def test_audio_rounding_tolerance_does_not_relax_video_timing(fixture_media, track_type, delta, accepted):
    _, _, ffprobe, draft = fixture_media
    draft = copy.deepcopy(draft)
    track = draft['tracks'][0]
    track['type'] = track_type
    track['segments'][0]['source_timerange']['duration'] += delta
    if accepted:
        review.prepare(draft, ffprobe)
    else:
        with pytest.raises(ValueError, match='source/target durations'):
            review.prepare(draft, ffprobe)


@pytest.mark.parametrize('damage', ['missing_file', 'source_after_eof', 'non_original_tail'])
def test_original_audio_tail_support_still_rejects_invalid_sources(existing_four_item_preset, damage):
    _, draft, _, _ = existing_four_item_preset
    draft = copy.deepcopy(draft)
    track = next(t for t in draft['tracks'] if t['type'] == 'audio'
                 and any(s['source_timerange']['duration'] == 1_000_000 for s in t['segments']))
    segment = next(s for s in track['segments'] if s['source_timerange']['duration'] == 1_000_000)
    if damage == 'missing_file':
        material = next(m for m in draft['materials']['audios'] if m['id'] == segment['material_id'])
        material['path'] += '.missing'
    elif damage == 'source_after_eof':
        segment['source_timerange']['start'] = 1_000_000
    else:
        track['name'] = 'unrelated-added-sound'
        # This test isolates source validation from native font/effect previews.
        draft['tracks'] = [track]
    with pytest.raises(ValueError, match='source'):
        review.prepare(draft, shutil.which('ffprobe'), layout_only=True)


def test_real_preset_audio_continuous_cli_with_relative_plan_paths(tmp_path, existing_four_item_preset, monkeypatch):
    """Full mux/decode of four real authored cues on an explicit fixture layout."""
    from PIL import Image
    plan, draft, _, _ = existing_four_item_preset
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    font = Path('C:/Windows/Fonts/msyh.ttc')
    if not ffprobe or not font.is_file():
        pytest.skip('real ffprobe and local fixture font required')
    # The audio is unchanged. The visible fixture binds a known local font and
    # background; this does not claim reproduction of the original native look.
    background = tmp_path/'fixture-background.png'
    Image.new('RGB', (108, 192), '#26333f').save(background)
    draft['materials'].setdefault('videos', []).append(
        {'id': 'fixture-background', 'type': 'photo', 'path': str(background), 'width': 108, 'height': 192})
    draft['tracks'].insert(0, {'name': 'fixture-background', 'type': 'video', 'segments': [{
        'material_id': 'fixture-background', 'target_timerange': {'start': 0, 'duration': 7_000_000},
        'source_timerange': {'start': 0, 'duration': 7_000_000}, 'speed': 1, 'volume': 0}]})
    for material in draft['materials'].get('texts', []):
        payload = json.loads(material['content'])
        for style in payload.get('styles', []):
            style.setdefault('font', {})['path'] = str(font)
        material['content'] = json.dumps(payload, ensure_ascii=False)
    for key in ('preset_registry', 'preset_root'):
        plan[key] = os.path.relpath(plan[key], tmp_path)
    plan['audio_path_map'] = {source: os.path.relpath(path, tmp_path)
                              for source, path in plan['audio_path_map'].items()}
    plan_path, draft_path = tmp_path/'plan.json', tmp_path/'draft.json'
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
    output, report_path = tmp_path/'real-author-audio-review.mp4', tmp_path/'review-report.json'
    before = copy.deepcopy(draft)
    monkeypatch.setattr(sys, 'argv', ['render_candidate_video_review.py', '--plan', str(plan_path),
        '--draft', str(draft_path), '--ffmpeg', ffmpeg, '--ffprobe', ffprobe, '--output', str(output),
        '--report', str(report_path), '--text-unit-to-px', '5', '--short-edge', '108', '--fps', '10', '--layout-only'])
    review.main()
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert draft == before
    assert report['native_visual_verified'] is False and report['audio']['native_listening_verified'] is False
    assert report['audio']['audio_sources'] == 4
    original = report['audio']['original_preset_audio']
    assert original['ok'] and original['track_count'] == 2 and original['segment_count'] == 4
    assert original['source_tail_padding_us'] > 0
    assert report['frame_count'] == 70 and report['audio']['duration_seconds'] == 7
    info = review.probe(output, ffprobe)
    assert {stream['codec_type'] for stream in info['streams']} == {'video', 'audio'}
    assert float(info['format']['duration']) == pytest.approx(7, abs=.025)
    frames = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-an', '-f', 'rawvideo',
                             '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout
    assert len(frames) == 70 * 108 * 192 * 3
    sound = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-vn', '-ac', '1', '-ar', '48000',
                            '-f', 'f32le', '-'], check=True, capture_output=True).stdout
    samples = np.frombuffer(sound, dtype='<f4')
    assert np.max(np.abs(samples[:24000])) < .001
    assert np.max(np.abs(samples[48000:192000])) > .001
    assert np.max(np.abs(samples[240000:288000])) < .001
