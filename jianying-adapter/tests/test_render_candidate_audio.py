"""Real constant-tempo audition; the draft and native cue design stay unchanged."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_candidate_audio as audio


@pytest.fixture
def cue(tmp_path):
    ffmpeg = os.environ.get('REVIEW_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('real FFmpeg required')
    path = tmp_path / 'tone.wav'
    samples = .2 * np.sin(2 * np.pi * 440 * np.arange(5 * 48000) / 48000)
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes((samples * 32767).astype('<i2').tobytes())
    segment = dict(id='cue', material_id='tone', speed=1, volume=.5,
                   source_timerange={'start': 100000, 'duration': 1000000},
                   target_timerange={'start': 0, 'duration': 1000000}, extra_material_refs=[])
    draft = {'materials': {'audios': [{'id': 'tone', 'path': str(path)}]},
             'tracks': [{'type': 'audio', 'name': 'fixture', 'segments': [segment]}]}
    return ffmpeg, draft


@pytest.mark.parametrize('speed', [1, 1.22, .5, 4])
def test_constant_tempo_preserves_pitch_and_fills_real_source_interval(cue, tmp_path, speed):
    ffmpeg, draft = cue
    segment = draft['tracks'][0]['segments'][0]
    segment['speed'] = speed
    segment['source_timerange']['duration'] = round(speed * 1000000)
    before = copy.deepcopy(draft)
    output = tmp_path / 'mix.wav'
    report = audio.render({}, draft, ffmpeg, output)
    raw = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output), '-ac', '1', '-ar', '48000',
                          '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype='<f4')
    assert len(samples) == 48000
    # A fast cue read for target duration instead of source duration would end
    # early; inspect late real sound as well as unchanged pitch.
    for chunk in (samples[9600:19200], samples[33600:43200]):
        assert .04 < np.sqrt(np.mean(chunk ** 2)) < .09
        frequency = np.argmax(np.abs(np.fft.rfft(chunk))) * 48000 / len(chunk)
        assert frequency == pytest.approx(440, abs=5)
    assert bool(report['constant_speed_sources']) is (speed != 1)
    assert draft == before


@pytest.mark.parametrize('damage', ['zero', 'nan', 'curve', 'unknown_mode', 'duration'])
def test_bad_tempo_or_duration_is_not_hidden_with_padding(cue, tmp_path, damage):
    ffmpeg, draft = cue
    segment = draft['tracks'][0]['segments'][0]
    if damage in ('zero', 'nan'):
        segment['speed'] = 0 if damage == 'zero' else float('nan')
    elif damage == 'duration':
        segment['speed'] = 1.22
    else:
        segment['extra_material_refs'] = ['speed']
        draft['materials']['speeds'] = [{'id': 'speed', 'mode': 1 if damage == 'unknown_mode' else 0,
                                        'curve_speed': {'points': [1, 2]} if damage == 'curve' else None}]
    output = tmp_path / 'invalid.wav'
    with pytest.raises(ValueError):
        audio.render({}, draft, ffmpeg, output)
    assert not output.exists()


@pytest.mark.parametrize('background_volume, risk', [(1.0, True), (.1, False)])
def test_actual_background_balance_detects_masking_even_without_clipping(cue, tmp_path, background_volume, risk):
    ffmpeg, draft = cue
    draft['tracks'][0]['name'] = 'JY_ROUGH_CUT_VIDEO'
    draft['tracks'].append(copy.deepcopy(draft['tracks'][0]))
    draft['tracks'][1]['name'] = 'background_fixture'
    draft['tracks'][1]['segments'][0]['volume'] = background_volume
    before = copy.deepcopy(draft)
    result = audio.render({}, draft, ffmpeg, tmp_path/'mix.wav')
    assert result['headroom_at_least_1db']
    assert bool(result['voice_balance']['intervals']) is risk
    assert result['ok'] is not risk
    assert draft == before
    assert not list(tmp_path.glob('.audio-meter-*'))


@pytest.mark.parametrize('background_volume, risk', [(1.0, True), (.1, False)])
def test_assembly_meter_uses_native_gains_and_cleans_temporary_audio(cue, tmp_path, monkeypatch, background_volume, risk):
    from jianying_adapter.action_audio import measure_candidate_audio, validate_audio_measurement
    ffmpeg, draft = cue
    monkeypatch.setenv('REVIEW_FFMPEG', ffmpeg)
    draft['tracks'][0]['name'] = 'JY_ROUGH_CUT_VIDEO'
    draft['tracks'].append(copy.deepcopy(draft['tracks'][0]))
    draft['tracks'][1]['name'] = 'background_fixture'
    draft['tracks'][1]['segments'][0]['volume'] = background_volume
    before = copy.deepcopy(draft)
    report = measure_candidate_audio({}, draft, plan_path=tmp_path/'plan.json')
    assert report['ok'] is not risk
    assert bool(validate_audio_measurement(draft, report)) is risk
    assert validate_audio_measurement(draft, None)
    assert draft == before
    assert not list(tmp_path.glob('.audio-check-*'))


def test_meter_missing_ffmpeg_fails_without_leaving_audio(cue, tmp_path, monkeypatch):
    from jianying_adapter.action_audio import measure_candidate_audio
    _, draft = cue
    draft['tracks'][0]['name'] = 'JY_ROUGH_CUT_VIDEO'
    draft['tracks'].append(copy.deepcopy(draft['tracks'][0]))
    draft['tracks'][1]['name'] = 'background_fixture'
    monkeypatch.setenv('REVIEW_FFMPEG', str(tmp_path/'missing-ffmpeg'))
    report = measure_candidate_audio({}, draft, plan_path=tmp_path/'plan.json')
    assert report['ok'] is False
    assert report['status'] == 'measurement_failed'
    assert not list(tmp_path.glob('.audio-check-*'))


def test_short_simultaneous_cue_is_not_hidden_by_100ms_average(cue, tmp_path):
    ffmpeg, draft = cue
    voice = draft['tracks'][0]
    voice['name'] = 'JY_ROUGH_CUT_VIDEO'
    pulse = tmp_path/'pulse.wav'
    samples = np.zeros(48000)
    samples[:480] = .2 * np.sin(2 * np.pi * 440 * np.arange(480) / 48000)
    with wave.open(str(pulse), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(48000)
        out.writeframes((samples * 32767).astype('<i2').tobytes())
    background = copy.deepcopy(voice)
    background['name'] = 'background_fixture'
    background['segments'][0].update(material_id='pulse', source_timerange={'start': 0, 'duration': 1000000})
    draft['tracks'].append(background)
    draft['materials']['audios'].append({'id': 'pulse', 'path': str(pulse)})
    report = audio.render({}, draft, ffmpeg, tmp_path/'mix.wav')
    assert report['headroom_at_least_1db']
    assert not report['ok']
    assert report['voice_balance']['window_us'] == 10000
    assert report['voice_balance']['intervals'][0]['start_us'] == 0
    # With the same signal, a 100ms mean is ~10dB below speech and misses the cue.
    tone = .2 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48000)
    assert 20*np.log10(np.sqrt(np.mean(samples[:4800]**2)) / np.sqrt(np.mean(tone**2))) < -6


@pytest.mark.parametrize('change', ['visual_only', 'volume', 'time', 'source', 'threshold', 'fade'])
def test_reuse_depends_on_sound_not_new_visual_candidate(cue, tmp_path, monkeypatch, change):
    from jianying_adapter import action_audio
    ffmpeg, draft = cue
    monkeypatch.setenv('REVIEW_FFMPEG', ffmpeg)
    draft['tracks'][0]['name'] = 'JY_ROUGH_CUT_VIDEO'
    draft['tracks'].append(copy.deepcopy(draft['tracks'][0]))
    background = draft['tracks'][1]
    background['name'] = 'background_fixture'
    background['segments'][0]['volume'] = .1
    state_path = tmp_path/'state.json'
    state_path.write_text(json.dumps({'artifacts': {}}), encoding='utf-8')
    plan = {'project_state': str(state_path)}
    first = action_audio.measure_candidate_audio(plan, draft, plan_path=tmp_path/'plan.json')
    assert first['ok']
    report_path = tmp_path/'report.json'
    report_path.write_text(json.dumps({'ok': True, 'final_validation': {'audio_measurement': first}}), encoding='utf-8')
    state_path.write_text(json.dumps({'artifacts': {'structure_report': str(report_path)}}), encoding='utf-8')
    segment = background['segments'][0]
    if change == 'visual_only':
        segment['id'] = 'rebuilt-segment'
        segment['clip'] = {'scale': {'x': 2, 'y': 2}, 'alpha': .5}
        draft['materials']['texts'] = [{'id': 'new-title', 'content': '改了字幕'}]
    elif change == 'volume':
        segment['volume'] = .8
    elif change == 'time':
        segment['target_timerange']['start'] = 10000
    elif change == 'source':
        path = Path(draft['materials']['audios'][0]['path'])
        stamp = path.stat().st_mtime_ns + 1000000000
        os.utime(path, ns=(stamp, stamp))
    elif change == 'threshold':
        plan['audio_checks'] = {'background_margin_db': 9}
    else:
        draft['materials']['audio_fades'] = [{'id': 'fade', 'fade_in_duration': 10000, 'fade_out_duration': 0}]
        segment['extra_material_refs'] = ['fade']
    calls = []
    def must_remeasure(*a, **kw):
        calls.append(True)
        raise OSError('remeasure requested')
    monkeypatch.setattr(action_audio, 'render_candidate_audio', must_remeasure)
    result = action_audio.measure_candidate_audio(plan, draft, plan_path=tmp_path/'plan.json')
    if change == 'visual_only':
        assert result == first and not calls
        assert not action_audio.validate_audio_measurement(draft, first, plan=plan, plan_path=tmp_path/'plan.json')
    else:
        assert calls and result['status'] == 'measurement_failed'
        assert action_audio.validate_audio_measurement(draft, first, plan=plan, plan_path=tmp_path/'plan.json')
