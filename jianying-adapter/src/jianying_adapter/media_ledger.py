from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .validate import ValidationReport


DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "assets" / "media_source_policy_v1.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _domain_matches(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def validate_media_ledger(
    ledger: dict[str, Any],
    *,
    policy: dict[str, Any],
    verify_files: bool = True,
    _ancestry: frozenset[tuple[str, str]] = frozenset(),
) -> ValidationReport:
    report = ValidationReport()
    providers = {item["provider"]: item for item in policy.get("sources") or []}
    required = policy.get("ledger_required_fields") or []
    gate = policy.get("production_gate") or {}
    accepted_status = set(gate.get("accepted_license_status") or [])
    assets = ledger.get("assets")

    if ledger.get("schema") != "jianying_media_asset_ledger_v1":
        report.add("media_ledger_schema_invalid", "Expected jianying_media_asset_ledger_v1", "schema")
    if not isinstance(assets, list):
        report.add("media_ledger_assets_invalid", "assets must be a list", "assets")
        assets = []

    checked: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, asset in enumerate(assets):
        base = f"assets[{index}]"
        if not isinstance(asset, dict):
            report.add("media_asset_invalid", "Asset entry must be an object", base)
            continue
        source_kind = asset.get("source_kind", "download")
        non_download_fields = {"source_page_url", "license_url", "license_checked_at", "license_name"}
        applicable = [field for field in required if source_kind == "download" or field not in non_download_fields]
        missing = [field for field in applicable if asset.get(field) in (None, "")]
        if missing:
            report.add("media_asset_fields_missing", f"Missing required fields: {', '.join(missing)}", base)

        asset_id = str(asset.get("asset_id") or "")
        if asset_id in seen_ids:
            report.add("media_asset_id_duplicate", f"Duplicate asset_id: {asset_id}", f"{base}.asset_id")
        seen_ids.add(asset_id)

        provider_name = str(asset.get("provider") or "")
        provider = providers.get(provider_name)
        if source_kind in {"local", "generated", "authored"}:
            provider = {"media_types": ["image", "video"] if source_kind == "local" else ["image"]}
            expected_status = "user_authorized" if source_kind == "local" else "project_generated"
            if asset.get("license_status") != expected_status or not asset.get("authorization_source"):
                report.add("media_origin_authorization_missing", f"{source_kind} requires {expected_status} and authorization_source", base)
            if source_kind in {"generated", "authored"}:
                provenance = asset.get("generation" if source_kind == "generated" else "authoring") or {}
                if asset.get("truth_role") != "illustration" or not provenance.get("method") or not provenance.get("description"):
                    report.add("media_generated_provenance_invalid", "Created assets require illustration role and actual method/description", base)
        elif source_kind == "derived":
            provider = {"media_types": ["image", "video"]}
            try:
                from .media_processing import validate_processing_record
                derivation = asset.get("derivation") or {}
                record_path = Path(str(asset.get("processing_record_path") or ""))
                if not record_path.is_file() or load_json(record_path) != derivation:
                    raise ValueError("派生交接与实际生成的加工记录不一致")
                parent = derivation.get("parent") or {}
                ledger_path = Path(str(parent.get("ledger_path") or "")).resolve()
                identity = (str(ledger_path), str(parent.get("asset_id") or ""))
                if identity in _ancestry:
                    raise ValueError("派生来源形成循环")
                parent_ledger = load_json(ledger_path)
                parents = [row for row in parent_ledger.get("assets", []) if row.get("asset_id") == identity[1]]
                if len(parents) != 1:
                    raise ValueError("派生来源必须唯一绑定原素材台账")
                original = parents[0]
                parent_report = validate_media_ledger(
                    {"schema": parent_ledger.get("schema"), "assets": parents}, policy=policy,
                    verify_files=verify_files, _ancestry=_ancestry | {identity},
                )
                if not parent_report.ok:
                    raise ValueError("派生来源未通过原有许可/文件检查: " + json.dumps(parent_report.to_dict(), ensure_ascii=False))
                if parent.get("sha256") != original.get("sha256"):
                    raise ValueError("派生来源文件与加工时绑定不一致")
                if original.get("media_type") != "video":
                    raise ValueError("当前局部加工必须来自真实视频源")
                for field in ("provider", "license_status", "license_url", "license_name", "license_checked_at",
                              "source_page_url", "license_evidence", "usage_scope", "editorial_only",
                              "attribution_required", "attribution_text"):
                    if asset.get(field) != original.get(field):
                        raise ValueError(f"派生素材不能改写来源的 {field}")
                if not asset.get("authorization_source"):
                    raise ValueError("派生素材须记录当前加工授权来源")
                if asset.get("truth_role") == "evidence" and original.get("truth_role") != "evidence":
                    raise ValueError("原始示意素材不能经加工升级为事实证据")
                if derivation.get("output_sha256") != asset.get("sha256"):
                    raise ValueError("派生文件与实际加工结果不一致")
                validate_processing_record(derivation, asset.get("probe") or {})
                expected_type = "image" if derivation["parameters"]["mode"] == "frame" else "video"
                if asset.get("media_type") != expected_type:
                    raise ValueError("派生素材类型与实际加工方式不一致")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report.add("media_derivation_invalid", str(exc), f"{base}.derivation")
        elif source_kind != "download":
            report.add("media_origin_invalid", f"Unknown source_kind: {source_kind}", base)
        elif provider_name == "external":
            # ClipNav is a discovery list, not a commercial-license whitelist.
            provider = {"media_types": ["image", "video"], "domains": []}
            if not asset.get("license_evidence"):
                report.add("media_item_license_evidence_missing", "Other sites require item-specific license evidence", base)
        if not provider:
            report.add("media_provider_unknown", f"Unknown provider: {provider_name}", f"{base}.provider")
            continue

        source_url = str(asset.get("source_page_url") or "")
        if source_kind == "download" and provider_name != "external" and not _domain_matches(source_url, provider.get("domains") or []):
            report.add("media_source_domain_mismatch", f"Source URL does not match {provider_name}", f"{base}.source_page_url")
        if source_kind == "download" and any(urlparse(str(asset.get(field) or "")).scheme not in {"http", "https"} or not urlparse(str(asset.get(field) or "")).hostname for field in ("source_page_url", "license_url")):
            report.add("media_source_url_invalid", "Downloaded assets require source and license HTTP(S) URLs", base)

        media_type = asset.get("media_type")
        if media_type not in (provider.get("media_types") or []):
            report.add("media_type_not_supported", f"{provider_name} is not registered for media type {media_type}", f"{base}.media_type")

        if source_kind == "download" and asset.get("license_status") not in accepted_status:
            report.add("media_license_unverified", "Production assets require verified_commercial status", f"{base}.license_status")
        if asset.get("attribution_required") not in (True, False) or type(asset.get("attribution_required")) is not bool:
            report.add("media_attribution_unknown", "attribution_required must be explicitly true or false", base)
        if gate.get("reject_editorial_only") and asset.get("editorial_only") is not False:
            report.add("media_editorial_only", "Editorial-only or unknown usage is not allowed for production", f"{base}.editorial_only")
        if provider.get("requires_license_name") and not asset.get("license_name"):
            report.add("media_license_name_missing", f"{provider_name} requires the item license name", f"{base}.license_name")
        required_scope = provider.get("requires_usage_scope")
        if required_scope and asset.get("usage_scope") != required_scope:
            report.add("media_usage_scope_invalid", f"{provider_name} requires usage_scope={required_scope}", f"{base}.usage_scope")
        if gate.get("require_attribution_text_when_needed") and asset.get("attribution_required") is True and not asset.get("attribution_text"):
            report.add("media_attribution_missing", "Attribution text is required", f"{base}.attribution_text")

        if gate.get("require_semantic_query") and not (asset.get("query_zh") or asset.get("query_en")):
            report.add("media_query_missing", "At least one semantic search query is required", base)

        local_path = Path(str(asset.get("local_path") or ""))
        sha256 = str(asset.get("sha256") or "").lower()
        if not SHA256_RE.fullmatch(sha256):
            report.add("media_sha256_invalid", "sha256 must contain 64 lowercase hex characters", f"{base}.sha256")
        if verify_files and gate.get("require_local_file"):
            if not local_path.is_file():
                report.add("media_file_missing", "Frozen local media file does not exist", f"{base}.local_path")
            elif SHA256_RE.fullmatch(sha256) and file_sha256(local_path) != sha256:
                report.add("media_sha256_mismatch", "Frozen media file hash differs from ledger", f"{base}.sha256")

        checked.append(
            {
                "asset_id": asset_id,
                "provider": provider_name,
                "local_path": str(local_path),
                "license_status": asset.get("license_status"),
            }
        )

    report.facts.update(
        {
            "policy_schema": policy.get("schema"),
            "provider_order": [item["provider"] for item in sorted(providers.values(), key=lambda item: item["priority"])],
            "asset_count": len(assets),
            "checked_assets": checked,
        }
    )
    return report
