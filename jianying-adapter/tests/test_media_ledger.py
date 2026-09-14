from __future__ import annotations

import json
from pathlib import Path

from jianying_adapter.media_ledger import DEFAULT_POLICY, file_sha256, load_json, validate_media_ledger


def valid_asset(tmp_path: Path) -> dict:
    media = tmp_path / "office.mp4"
    media.write_bytes(b"synthetic media")
    return {
        "asset_id": "broll-office-001",
        "semantic_node_id": "node-01",
        "semantic": "办公室使用电脑",
        "query_zh": "办公室 使用电脑",
        "query_en": "office worker using computer",
        "provider": "pexels",
        "source_page_url": "https://www.pexels.com/video/example-123/",
        "license_url": "https://www.pexels.com/legal-pages/license/",
        "license_checked_at": "2026-09-02T00:00:00+08:00",
        "license_name": "Pexels License",
        "license_status": "verified_commercial",
        "attribution_required": False,
        "editorial_only": False,
        "local_path": str(media),
        "sha256": file_sha256(media),
        "media_type": "video",
    }


def test_valid_pexels_asset_passes(tmp_path: Path):
    policy = load_json(DEFAULT_POLICY)
    ledger = {"schema": "jianying_media_asset_ledger_v1", "assets": [valid_asset(tmp_path)]}
    assert validate_media_ledger(ledger, policy=policy).ok


def test_videvo_requires_commercial_scope_and_attribution(tmp_path: Path):
    policy = load_json(DEFAULT_POLICY)
    asset = valid_asset(tmp_path)
    asset.update(
        {
            "provider": "videvo",
            "source_page_url": "https://www.videvo.net/video/example/",
            "license_url": "https://www.videvo.net/blog/how-we-license-our-footage-on-videvo-net/",
            "license_name": "Videvo Attribution License",
            "attribution_required": True,
        }
    )
    report = validate_media_ledger(
        {"schema": "jianying_media_asset_ledger_v1", "assets": [asset]},
        policy=policy,
    )
    assert not report.ok
    codes = {issue.code for issue in report.issues}
    assert "media_usage_scope_invalid" in codes
    assert "media_attribution_missing" in codes


def test_unknown_or_editorial_asset_is_rejected(tmp_path: Path):
    policy = load_json(DEFAULT_POLICY)
    asset = valid_asset(tmp_path)
    asset["license_status"] = "unverified"
    asset["editorial_only"] = True
    report = validate_media_ledger(
        {"schema": "jianying_media_asset_ledger_v1", "assets": [asset]},
        policy=policy,
    )
    codes = {issue.code for issue in report.issues}
    assert "media_license_unverified" in codes
    assert "media_editorial_only" in codes
