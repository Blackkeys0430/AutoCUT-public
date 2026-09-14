from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "build_multiclip_base.py"


@pytest.fixture(scope="module")
def multiclip_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_multiclip_base", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRange:
    def __init__(self, start: int, duration: int) -> None:
        self.start = start
        self.duration = duration


class _FakeMaterial:
    def __init__(self, path: str) -> None:
        self.path = path
        self.material_id = f"material-{Path(path).name}"
        self.duration = 10_000_000
        self.width = 1080
        self.height = 1920


class _FakeSegment:
    def __init__(self, material, target_timerange, *, source_timerange, volume=1.0, **_kwargs):
        self.material = material
        self.target_timerange = target_timerange
        self.source_timerange = source_timerange
        self.volume = volume
        self.fade_calls = []

    def add_fade(self, in_duration, out_duration):
        self.fade_calls.append((in_duration, out_duration))
        return self


class _FakeTextStyle:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeShadow:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeClipSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeTextSegment:
    def __init__(self, text, timerange, **kwargs):
        self.text = text
        self.timerange = timerange
        self.target_timerange = timerange
        self.kwargs = kwargs
        self.style_ranges = []

    def add_style_range(self, start, end, **kwargs):
        self.style_ranges.append((start, end, kwargs))


class _FakeTrackSpec:
    def __init__(self, track_type, name):
        self.track_type = track_type
        self.name = name


class _FakeTrackType:
    video = "video"
    text = "text"


class _FakeFontType:
    宋体 = "宋体"


class _FakeScript:
    def __init__(self, path: Path, width: int, height: int, fps: int) -> None:
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.duration = 0
        self.tracks = []

    def append_track(self, spec):
        track = {"name": spec.name, "type": spec.track_type, "segments": []}
        self.tracks.append(track)
        return track

    def add_segment(self, segment, *, track):
        track["segments"].append(segment)
        self.duration = max(self.duration, segment.target_timerange.start + segment.target_timerange.duration)

    def save(self):
        self.path.mkdir(parents=True, exist_ok=True)
        payload = {
            "duration": self.duration,
            "canvas_config": {"width": self.width, "height": self.height},
            "tracks": [
                {"name": track["name"], "type": track["type"], "segments": [
                    {
                        "target_timerange": {
                            "start": segment.target_timerange.start,
                            "duration": segment.target_timerange.duration,
                        },
                        "source_timerange": {
                            "start": getattr(getattr(segment, "source_timerange", None), "start", 0),
                            "duration": getattr(getattr(segment, "source_timerange", None), "duration", 0),
                        },
                        "volume": getattr(segment, "volume", 1.0),
                        **(segment.export_json() if hasattr(segment, "clip_settings") else {}),
                    }
                    for segment in track["segments"]
                    if hasattr(segment, "target_timerange")
                ]}
                for track in self.tracks
            ],
        }
        (self.path / "draft_content.json").write_text(json.dumps(payload), encoding="utf-8")


class _FakeDraftFolder:
    def __init__(self, root: str, *, user_data_path: str):
        self.root = Path(root)
        self.user_data_path = Path(user_data_path)

    def create_draft(self, name, width, height, fps):
        draft_path = self.root / name
        draft_path.mkdir(parents=True)
        return _FakeScript(draft_path, width, height, fps)


def _patch_runtime(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "VideoMaterial", _FakeMaterial)
    monkeypatch.setattr(module, "VideoSegment", _FakeSegment)
    monkeypatch.setattr(module, "TextSegment", _FakeTextSegment)
    monkeypatch.setattr(module, "TextStyle", _FakeTextStyle)
    monkeypatch.setattr(module, "TextShadow", _FakeShadow)
    monkeypatch.setattr(module, "ClipSettings", _FakeClipSettings)
    monkeypatch.setattr(module, "DraftFolder", _FakeDraftFolder)
    monkeypatch.setattr(module, "TrackSpec", _FakeTrackSpec)
    monkeypatch.setattr(module, "TrackType", _FakeTrackType)
    monkeypatch.setattr(module, "FontType", _FakeFontType)
    monkeypatch.setattr(module, "trange", _FakeRange)


def _plan(source_a: Path, source_b: Path) -> dict:
    return {
        "schema": "multiclip_edit_plan_v1",
        "canvas": {"width": 1080, "height": 1920, "fps": 30},
        "draft_name": "多源测试草稿",
        "timeline_segments": [
            {"id": "a", "source_path": str(source_a), "source_start_us": 1_000_000, "source_end_us": 3_000_000, "speed": 2.0},
            {"id": "b", "source_path": str(source_b), "source_start_us": 500_000, "source_end_us": 2_000_000},
        ],
        "captions": [
            {
                "start_us": 500_000,
                "duration_us": 2_000_000,
                "zh": "中文第一行\n中文第二行",
                "en": "one\ntwo\nthree",
                "zh_keywords": ["中文"],
            }
        ],
    }


def test_retake_deletion_must_match_kept_ranges(multiclip_module, tmp_path):
    plan = _plan(tmp_path / "a.mov", tmp_path / "b.mov")
    plan["retake_cuts"] = [{"source_path": str(tmp_path / "a.mov"),
                            "source_start_us": 1_500_000, "source_end_us": 2_000_000,
                            "reason": "废弃重录"}]
    with pytest.raises(multiclip_module.MulticlipPlanError, match="废弃重录仍在"):
        multiclip_module.validate_edit_plan(plan)
    plan["timeline_segments"][0]["source_start_us"] = 2_000_000
    normalized = multiclip_module.validate_edit_plan(plan)
    assert normalized["retake_cuts"] == plan["retake_cuts"]


def test_build_multiclip_writes_continuous_draft_and_sidecars(
    multiclip_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runtime(multiclip_module, monkeypatch)
    source_a = tmp_path / "a.mov"
    source_b = tmp_path / "b.mov"
    source_a.write_bytes(b"source-a")
    source_b.write_bytes(b"source-b")
    plan = _plan(source_a, source_b)
    plan_path = tmp_path / "edit_plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    output_root = tmp_path / "output"

    result = multiclip_module.build(plan_path, output_root)

    draft = output_root / "多源测试草稿"
    assert Path(result["draft"]) == draft
    assert (draft / "draft_content.json").is_file()
    assert {name for name in ("edl.json", "time_map.json", "subtitle_plan.json", "manifest.json") if (output_root / name).is_file()} == {
        "edl.json", "time_map.json", "subtitle_plan.json", "manifest.json"
    }

    edl = json.loads((output_root / "edl.json").read_text(encoding="utf-8"))
    assert edl["duration_us"] == 2_500_000
    assert [item["target_start_us"] for item in edl["segments"]] == [0, 1_000_000]
    assert [item["target_end_us"] for item in edl["segments"]] == [1_000_000, 2_500_000]
    assert [item["speed"] for item in edl["segments"]] == [2.0, 1.0]

    time_map = json.loads((output_root / "time_map.json").read_text(encoding="utf-8"))
    assert [item["segment_id"] for item in time_map["segments"]] == ["a", "b"]
    assert time_map["segments"][1]["source_path"] == str(source_b.resolve())
    assert time_map["segments"][0]["speed"] == 2.0

    subtitle = json.loads((output_root / "subtitle_plan.json").read_text(encoding="utf-8"))
    card = subtitle["cards"][0]
    assert card["zh_lines"] == 2
    assert card["en_lines"] == 3
    assert card["zh_transform_y"] == -0.375
    assert card["en_transform_y"] == -0.605
    assert card["zh_transform_y"] - card["en_transform_y"] <= 0.25
    assert subtitle["style"]["font"] == "宋体"
    assert subtitle["style"]["keyword_color"] == "#FFD600"

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["timeline_segment_count"] == 2
    assert manifest["audio"] == "original_video_audio_preserved"
    assert manifest["english_subtitles"] is True
    assert manifest["started_jianying"] is False
    assert manifest["exported"] is False
    assert manifest["audio_fade"]["enabled"] is True
    assert all(item["in_us"] == 30_000 and item["out_us"] == 30_000 for item in manifest["audio_fade"]["segments"])


def test_build_refuses_existing_draft_and_invalid_source(multiclip_module: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "a.mov"
    plan = _plan(source, source)
    with pytest.raises(FileNotFoundError):
        multiclip_module.build(plan, tmp_path / "output")

    source.write_bytes(b"source")
    output_root = tmp_path / "output"
    (output_root / "多源测试草稿").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        multiclip_module.build(plan, output_root)


@pytest.mark.parametrize("volume", [0, 0.4, 1.5])
def test_segment_volume_reaches_native_video_and_sidecars(
    multiclip_module, tmp_path, monkeypatch, volume
):
    # Use the real VideoSegment serializer, replacing only media IO/container.
    native_video = multiclip_module.VideoSegment
    native_range = multiclip_module.trange
    _patch_runtime(multiclip_module, monkeypatch)
    monkeypatch.setattr(multiclip_module, "VideoSegment", native_video)
    monkeypatch.setattr(multiclip_module, "trange", native_range)
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")
    plan = _plan(source, source)
    plan["timeline_segments"][1]["volume"] = volume
    output = tmp_path / "output"
    multiclip_module.build(plan, output)
    content = json.loads((output / plan["draft_name"] / "draft_content.json").read_text("utf-8"))
    segments = content["tracks"][0]["segments"]
    assert [segment["volume"] for segment in segments] == [1.0, volume]
    assert [segment["target_timerange"]["duration"] for segment in segments] == [1_000_000, 1_500_000]
    assert [segment["speed"] for segment in segments] == [2.0, 1.0]
    for name in ("edl.json", "time_map.json"):
        sidecar = json.loads((output / name).read_text("utf-8"))
        assert [segment["volume"] for segment in sidecar["segments"]] == [1.0, volume]
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["audio"] == "original_video_audio_with_segment_volume"


@pytest.mark.parametrize("volume", [None, True, False, "0", -0.01, float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_volume_is_rejected_before_output(multiclip_module, tmp_path, volume):
    plan = _plan(tmp_path / "a.mov", tmp_path / "b.mov")
    plan["timeline_segments"][1]["volume"] = volume
    with pytest.raises(multiclip_module.MulticlipPlanError, match=r"timeline_segments\[1\].volume"):
        multiclip_module.build(plan, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_validate_rejects_relative_source_and_duplicate_segment(multiclip_module: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "a.mov"
    source.write_bytes(b"source")
    plan = _plan(source, source)
    plan["timeline_segments"][1]["source_path"] = "relative.mov"
    with pytest.raises(multiclip_module.MulticlipPlanError):
        multiclip_module.validate_edit_plan(plan)

    plan = _plan(source, source)
    plan["timeline_segments"][1]["id"] = "a"
    with pytest.raises(multiclip_module.MulticlipPlanError):
        multiclip_module.validate_edit_plan(plan)


def test_english_subtitles_can_be_disabled(
    multiclip_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runtime(multiclip_module, monkeypatch)
    source = tmp_path / "a.mov"
    source.write_bytes(b"source")
    plan = _plan(source, source)
    plan["subtitle_options"] = {"english_enabled": False}
    plan["captions"][0].pop("en")
    output_root = tmp_path / "output"

    multiclip_module.build(plan, output_root)

    content = json.loads(
        (output_root / "多源测试草稿" / "draft_content.json").read_text(encoding="utf-8")
    )
    assert [track["name"] for track in content["tracks"]] == [
        "JY_ROUGH_CUT_VIDEO",
        "JY_ZH_SUBTITLES",
    ]
    subtitle = json.loads((output_root / "subtitle_plan.json").read_text(encoding="utf-8"))
    assert subtitle["tracks"]["en"] is None
    assert subtitle["cards"][0]["en_lines"] == 0
    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["english_subtitles"] is False


@pytest.mark.parametrize('value', [None, True, False, '0', float('nan'), float('inf'), -float('inf'), -1.01, 1.01])
def test_invalid_subtitle_y_is_rejected_before_output(multiclip_module, tmp_path, value):
    plan = _plan(tmp_path / 'a.mov', tmp_path / 'b.mov')
    plan['subtitle_options'] = {'zh_transform_y': value}
    with pytest.raises(multiclip_module.MulticlipPlanError, match='subtitle_options.zh_transform_y'):
        multiclip_module.build(plan, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('value', [-1, 0, .25, 1])
def test_explicit_subtitle_y_survives_normalization(multiclip_module, tmp_path, value):
    plan = _plan(tmp_path / 'a.mov', tmp_path / 'b.mov')
    plan['subtitle_options'] = {'english_enabled': False, 'zh_transform_y': value}
    normalized = multiclip_module.validate_edit_plan(plan)
    assert normalized['subtitle_options'] == {'english_enabled': False, 'zh_transform_y': float(value)}
    assert multiclip_module.validate_edit_plan(normalized) == normalized


def test_default_subtitle_y_and_normalized_options_are_unchanged(multiclip_module, tmp_path):
    plan = _plan(tmp_path / 'a.mov', tmp_path / 'b.mov')
    normalized = multiclip_module.validate_edit_plan(plan)
    assert normalized['subtitle_options'] == {'english_enabled': True}
    assert multiclip_module._layout('中文', 'English', english_enabled=True)['zh_transform_y'] == -.43
    assert multiclip_module._layout('中文\n两行', 'English', english_enabled=True)['zh_transform_y'] == -.375


@pytest.mark.parametrize('english_enabled', [True, False])
def test_explicit_y_reaches_generated_tracks_and_keeps_language_gap(
    multiclip_module, tmp_path, monkeypatch, english_enabled
):
    # Keep the vendored native text/clip serializer; only video IO and the
    # workspace draft container use the existing test doubles.
    native_text = {name: getattr(multiclip_module, name) for name in
                   ('TextSegment', 'TextStyle', 'TextShadow', 'ClipSettings', 'FontType', 'trange')}
    _patch_runtime(multiclip_module, monkeypatch)
    for name, value in native_text.items():
        monkeypatch.setattr(multiclip_module, name, value)
    source = tmp_path / 'a.mov'
    source.write_bytes(b'source')
    plan = _plan(source, source)
    plan['subtitle_options'] = {'english_enabled': english_enabled, 'zh_transform_y': -.65}
    output = tmp_path / 'output'
    multiclip_module.build(plan, output)
    content = json.loads((output / plan['draft_name'] / 'draft_content.json').read_text('utf-8'))
    tracks = {track['name']: track for track in content['tracks']}
    assert tracks['JY_ZH_SUBTITLES']['segments'][0]['clip']['transform']['y'] == -.65
    subtitle = json.loads((output / 'subtitle_plan.json').read_text('utf-8'))['cards'][0]
    assert subtitle['zh_transform_y'] == -.65  # explicit centre, without multiline adjustment
    if english_enabled:
        assert tracks['JY_EN_SUBTITLES']['segments'][0]['clip']['transform']['y'] == -.88
        assert subtitle['zh_transform_y'] - subtitle['en_transform_y'] == pytest.approx(.23)
    else:
        assert 'JY_EN_SUBTITLES' not in tracks
        assert subtitle['en_transform_y'] is None


@pytest.mark.parametrize('value', [None, True, False, '0', float('nan'), float('inf'), -float('inf'), -1.01, 1.01, 10 ** 400])
def test_invalid_per_caption_y_is_rejected_before_output(multiclip_module, tmp_path, value):
    plan = _plan(tmp_path / 'a.mov', tmp_path / 'b.mov')
    plan['captions'][0]['zh_transform_y'] = value
    with pytest.raises(multiclip_module.MulticlipPlanError, match=r'captions\[0\].zh_transform_y'):
        multiclip_module.build(plan, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('global_y', [None, -.65])
@pytest.mark.parametrize('english_enabled', [True, False])
def test_per_caption_y_overrides_global_and_roundtrips_native_positions(
    multiclip_module, tmp_path, monkeypatch, global_y, english_enabled
):
    native_text = {name: getattr(multiclip_module, name) for name in
                   ('TextSegment', 'TextStyle', 'TextShadow', 'ClipSettings', 'FontType', 'trange')}
    _patch_runtime(multiclip_module, monkeypatch)
    for name, value in native_text.items():
        monkeypatch.setattr(multiclip_module, name, value)
    source = tmp_path / 'a.mov'
    source.write_bytes(b'source')
    plan = _plan(source, source)
    plan['subtitle_options'] = {'english_enabled': english_enabled}
    if global_y is not None:
        plan['subtitle_options']['zh_transform_y'] = global_y
    original = plan['captions'][0]
    plan['captions'] = [
        {**original, 'start_us': 0, 'duration_us': 500_000, 'zh_transform_y': .6},
        {**original, 'start_us': 500_000, 'duration_us': 500_000},
        {**original, 'start_us': 1_000_000, 'duration_us': 500_000, 'zh_transform_y': 0},
    ]
    normalized = multiclip_module.validate_edit_plan(plan)
    assert multiclip_module.validate_edit_plan(normalized) == normalized
    assert 'zh_transform_y' not in normalized['captions'][1]
    output = tmp_path / 'output'
    multiclip_module.build(plan, output)
    content = json.loads((output / plan['draft_name'] / 'draft_content.json').read_text('utf-8'))
    tracks = {track['name']: track for track in content['tracks']}
    cards = json.loads((output / 'subtitle_plan.json').read_text('utf-8'))['cards']
    expected = [.6, -.375 if global_y is None else global_y, 0.]
    assert [segment['clip']['transform']['y'] for segment in tracks['JY_ZH_SUBTITLES']['segments']] == expected
    assert [card['zh_transform_y'] for card in cards] == expected
    assert [(card['zh'], card['start_us'], card['duration_us']) for card in cards] == [
        (item['zh'], item['start_us'], item['duration_us']) for item in plan['captions']]
    if english_enabled:
        assert [segment['clip']['transform']['y'] for segment in tracks['JY_EN_SUBTITLES']['segments']] == pytest.approx([
            value - .23 for value in expected])
    else:
        assert 'JY_EN_SUBTITLES' not in tracks
