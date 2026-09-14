from __future__ import annotations

import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from jianying_adapter.preset_registry import describe_visual_form


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
CATALOG = ADAPTER / "preset_catalog"
INDEX = CATALOG / "preset_library_index_v1.json"
CANDIDATES = CATALOG / "all_auto_candidates_v1.json"
LEDGER = PROJECT / "video_trials/模板库1200盘点_20260902/full_structure_ledger_v1.json"
OUTPUT_JSON = CATALOG / "preset_usage_registry_v1.json"
OUTPUT_MD = CATALOG / "preset_usage_guide_v1.md"

ADAPTATION_POLICY = (
    "先保证当前主要观看对象的有效尺寸、关键细节和必要连续性，再让字幕预设适配主画面。"
    "预设可直接叠加视频并避开关键内容；空间不足先调整位置、断行、出现时点或选择更合适的预设，"
    "不默认缩小视频腾位。内部版式或字号的适配沿用当前授权和检查。"
    "缩小、分屏、小窗或黑底须有明确的信息或表达收益；次主体可承担补充信息、反应、"
    "空间参照或视觉连贯，须说明当前作用，不能仅为填满画面而保留。"
)


CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "cover_main_title": {
        "label": "封面/主标题",
        "use_when": ["视频开头提出主题", "进入新的大段落", "需要一句标题建立预期"],
        "avoid_when": ["逐句复述口播", "一闪而过的过渡词", "同屏已经有等价大标题"],
        "spoken_cues": ["今天讲", "这期说", "核心问题", "先说结论"],
        "zone": "upper_or_center_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.3,
    },
    "question_hook": {
        "label": "提问/开场钩子",
        "use_when": ["开头直接抛问题", "段落转折处制造认知缺口", "口播中出现明确反问"],
        "avoid_when": ["原句不是问题", "问题超过一个完整长句", "问号只是装饰"],
        "spoken_cues": ["为什么", "怎么", "是不是", "有没有", "你知道吗"],
        "zone": "upper_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.6,
    },
    "quote_conclusion": {
        "label": "金句/观点/结论",
        "use_when": ["一句话给出核心判断", "段落收束", "价值判断或可被记住的结论"],
        "avoid_when": ["信息尚未讲完", "只是连接词或背景信息", "长段解释没有明确落点"],
        "spoken_cues": ["所以", "本质上", "真正重要的是", "结论是", "记住"],
        "zone": "upper_or_side_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "keyword_emphasis": {
        "label": "关键词/重点字",
        "use_when": ["一到两个关键词需要视觉重音", "数字、动作词、情绪词落拍", "需要短暂注意力重置"],
        "avoid_when": ["完整长句", "连续每句话都强调", "没有语义重音的位置"],
        "spoken_cues": ["唯一", "关键", "一定", "不要", "最", "数字或专有名词"],
        "zone": "adaptive_upper_or_side",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.0,
    },
    "kinetic_short_text": {
        "label": "动态短字/纯文字动效",
        "use_when": ["2到8字短语需要跟随节拍出现", "动作、情绪或结果词需要冲击", "段落间做轻量节奏变化"],
        "avoid_when": ["长解释", "步骤或并列项需要保持结构", "仅因为画面空就硬加文字"],
        "spoken_cues": ["短动作词", "短结论", "情绪词", "品牌或术语"],
        "zone": "adaptive_upper_or_side",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.0,
    },
    "standard_caption": {
        "label": "常规动态字幕",
        "use_when": ["普通口播句需要动态字幕样式", "一句话语义完整且无需额外标题", "需要替代默认白字字幕"],
        "avoid_when": ["原片已有烧录字幕", "同时再叠一套普通字幕", "长段文字超过槽位容量"],
        "spoken_cues": ["正常陈述句", "解释句", "短因果句"],
        "zone": "lower_third_caption_band",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.2,
    },
    "multi_line_explanation": {
        "label": "双行/多行解释",
        "use_when": ["两句解释共同构成一个观点", "原因与结果", "主标题加补充说明"],
        "avoid_when": ["单个关键词", "三项以上同级列表", "文字过长导致整块遮脸"],
        "spoken_cues": ["因为…所以…", "不是…而是…", "主张加解释", "两项短对照"],
        "zone": "upper_or_side_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.8,
    },
    "parallel_list": {
        "label": "并列清单",
        "use_when": ["2到5个同级卖点、例子或建议", "口播逐项列举", "多个项目需要保留到列表结束"],
        "avoid_when": ["项目不是同一层级", "一段因果解释被误拆成列表", "实际项目数与槽位数不一致"],
        "spoken_cues": ["第一第二第三", "包括", "分别是", "有几个", "一是二是"],
        "zone": "side_or_upper_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "numbered_steps": {
        "label": "序号/步骤",
        "use_when": ["方法、流程、排名有明确先后", "口播出现第一步第二步", "需要建立执行顺序"],
        "avoid_when": ["只是并列没有先后", "步骤数量与槽位不一致", "一句话还未讲完就跳号"],
        "spoken_cues": ["第一步", "第二", "接下来", "最后", "排名"],
        "zone": "side_or_upper_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.8,
    },
    "data_progress_price": {
        "label": "数字/费用/进度信息",
        "use_when": ["价格、比例、时间、数量或进度是论据", "数字需要被看清和记住", "前后数据对比"],
        "avoid_when": ["数字并非口播重点", "单位缺失", "用装饰数字替代真实信息"],
        "spoken_cues": ["百分比", "元", "万", "天", "倍", "增长或下降"],
        "zone": "center_or_side_data_card",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "typing_word_reveal": {
        "label": "逐字/打字机",
        "use_when": ["答案逐步揭晓", "悬念后的短句", "需要让观众按顺序读一个关键词"],
        "avoid_when": ["快节奏连续口播", "长句导致读不完", "结论需要瞬间冲击而非逐字出现"],
        "spoken_cues": ["答案是", "其实", "最后发现", "逐字揭晓"],
        "zone": "upper_or_center_negative_space",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.5,
    },
    "section_lower_third": {
        "label": "小标题/章节/人名条",
        "use_when": ["标记人物身份", "切换章节", "标记当前讨论主题"],
        "avoid_when": ["每句话都重复出现", "与普通字幕占用同一底部区域", "没有章节或身份变化"],
        "spoken_cues": ["新章节", "人物姓名职位", "话题标签"],
        "zone": "corner_or_above_caption_band",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.5,
    },
    "special_overlay": {
        "label": "特殊位置/覆盖字幕",
        "use_when": ["画面有明确留白可容纳特殊排版", "需要弹幕、环绕或边缘文字表达情绪", "语义确实需要覆盖感"],
        "avoid_when": ["人物居中且无留白", "作为常规字幕长期使用", "未经整段动画碰撞检查"],
        "spoken_cues": ["信息轰炸", "争议评论", "情绪爆发", "特殊段落包装"],
        "zone": "preserve_native_then_adapt_as_group",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.5,
    },
    "person_processing_composition": {
        "label": "人物处理/构图",
        "use_when": ["需要人物抠像、侧置、头像或字在人后", "人物与文字共同构图", "素材已具备当前人物分析结果"],
        "avoid_when": ["只替换视频路径却没有重新分析人物", "把结构字段存在当成画面成功", "人物与B-roll职责不明确"],
        "spoken_cues": ["人物反应镜头", "身份强调", "需要让出画面空间"],
        "zone": "content_dependent_recompute",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "media_showcase_broll": {
        "label": "素材展示/B-roll",
        "use_when": ["产品、截图或证据画面能直接解释口播", "需要展示操作或物品细节", "抽象观点需要视觉证据"],
        "avoid_when": ["素材与口播只关键词相关但不能证明观点", "B-roll遮挡主体且没有构图理由", "裁切后有效内容出画"],
        "spoken_cues": ["产品名称", "操作步骤", "案例证据", "界面或截图"],
        "zone": "evidence_led_or_dual_subject_layout",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "grid_split_screen": {
        "label": "分屏/宫格构图",
        "use_when": ["多个画面需要同时比较", "前后对照", "同类案例并列展示"],
        "avoid_when": ["只有一个有效画面", "小格裁切后主体不可辨认", "把同时出现误说成依次出现"],
        "spoken_cues": ["对比", "几种案例", "前后变化", "多个角度"],
        "zone": "full_canvas_composition",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.5,
    },
    "closing_cta": {
        "label": "结尾 CTA",
        "use_when": ["视频结尾明确要求关注、收藏、评论或私信", "行动指令与口播一致"],
        "avoid_when": ["内容中段", "口播没有行动指令", "同时堆叠多个互相竞争的CTA"],
        "spoken_cues": ["关注", "收藏", "评论", "私信", "转发"],
        "zone": "lower_or_center_end_card",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 2.0,
    },
    "true_transition": {
        "label": "真正转场",
        "use_when": ["前后确实存在两个镜头", "场景或段落发生变化", "转场动作与节奏点一致"],
        "avoid_when": ["单镜头内部只想加文字", "没有前后画面可连接", "把文字动画误当转场"],
        "spoken_cues": ["场景变化", "章节切换", "时间或地点跳转"],
        "zone": "between_two_visual_segments",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 0.5,
    },
    "unclear": {
        "label": "待人工辨认",
        "use_when": ["只有在看过原样动画并补齐语义说明后使用"],
        "avoid_when": ["自动生产", "仅凭名称猜用途"],
        "spoken_cues": [],
        "zone": "manual_review_required",
        "caption_policy": "同期同词只留一份，普通字幕缩为余句或关闭；特殊文字与剩余字幕共同保留否定、条件、数字和必要限定，特殊信息文字明显大于普通字幕",
        "duration_floor": 1.5,
    },
}


EXTERNAL_CATEGORY = {
    "MODERN-88-01": "keyword_emphasis",
    "MODERN-88-02": "kinetic_short_text",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(path)
    return value


def slot_windows(candidate: dict[str, Any]) -> list[dict[str, int]]:
    windows: list[dict[str, int]] = []
    for slot in (candidate.get("slots") or {}).get("text", []):
        refs: list[dict[str, Any]] = []
        for locator in slot.get("locators") or []:
            refs.extend(ref for ref in locator.get("segment_refs") or [] if isinstance(ref, dict))
        if not refs:
            continue
        starts = [int(ref.get("start", 0)) for ref in refs]
        ends = [int(ref.get("end", int(ref.get("start", 0)) + int(ref.get("duration", 0)))) for ref in refs]
        windows.append({"start_us": min(starts), "end_us": max(ends)})
    return windows


def animation_pattern(candidate: dict[str, Any]) -> str:
    windows = sorted(slot_windows(candidate), key=lambda item: (item["start_us"], item["end_us"]))
    if len(windows) <= 1:
        return "single_hit"
    starts = [item["start_us"] for item in windows]
    ends = [item["end_us"] for item in windows]
    tolerance = 120_000
    if max(starts) - min(starts) <= tolerance:
        return "simultaneous"
    if max(ends) - min(ends) <= tolerance:
        return "cumulative_reveal"
    if all(windows[index]["end_us"] <= windows[index + 1]["start_us"] + tolerance for index in range(len(windows) - 1)):
        return "sequential_replace"
    return "staggered_overlap"


def all_text_slots(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        slot for slot in (candidate.get("slots") or {}).get("text", [])
        if isinstance(slot, dict)
    ]


def punctuation_only(value: Any) -> bool:
    text = "".join(str(value or "").split())
    return bool(text) and all(unicodedata.category(character).startswith(("P", "S")) for character in text)


def editable_text_slots(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        slot for slot in all_text_slots(candidate)
        if slot.get("required")
        and not slot.get("decorative_locked")
        and not punctuation_only(slot.get("default_text"))
    ]


def char_count(value: Any) -> int:
    return len("".join(str(value or "").split()))


def production_gate(entry: dict[str, Any]) -> str:
    visual = entry.get("visual_validation")
    if visual == "accepted":
        return "guarded_production_eligible"
    if visual == "conditional":
        return "per_video_revalidation_required"
    if visual == "rejected":
        return "disabled_visual_rejected"
    return "classified_on_demand_preflight_required"


def dynamic_duration_floor(category: str, base_floor: float, candidate: dict[str, Any]) -> float:
    count = candidate.get("inferred_counts", {}).get("item_count")
    if category in {"parallel_list", "numbered_steps"} and isinstance(count, int) and count > 0:
        return max(base_floor, 0.75 * count + 0.8)
    return base_floor


def build_registry() -> dict[str, Any]:
    index = read_json(INDEX)
    source = read_json(CANDIDATES)
    preset_root = Path(source['preset_root'])
    ledger = read_json(LEDGER)
    candidates = {str(row["template_id"]): row for row in source.get("candidates", [])}
    purpose = {
        str(row["existing_template_id"]): row
        for row in ledger.get("structures", [])
        if row.get("existing_template_id")
    }
    records: list[dict[str, Any]] = []
    for entry in index.get("entries", []):
        template_id = str(entry["template_id"])
        candidate = candidates.get(template_id, {})
        purpose_row = purpose.get(template_id, {})
        category = str(purpose_row.get("primary_category") or EXTERNAL_CATEGORY.get(template_id) or "unclear")
        rule = CATEGORY_RULES[category]
        text_slots = all_text_slots(candidate)
        editable_slots = editable_text_slots(candidate)
        fixed_slots = [
            str(slot.get("default_text") or "")
            for slot in text_slots
            if slot.get("decorative_locked") or punctuation_only(slot.get("default_text"))
        ]
        capacities = [char_count(slot.get("default_text")) for slot in editable_slots]
        native_duration = float(entry.get("duration_seconds") or candidate.get("duration", {}).get("seconds") or 0.0)
        floor = dynamic_duration_floor(category, float(rule["duration_floor"]), candidate)
        inferred = candidate.get("inferred_counts") or {}
        source_path = entry.get('source_path') or candidate.get('source_path')
        native_path = Path(source_path) if source_path else None
        if native_path is not None and not native_path.is_absolute():
            native_path = preset_root / native_path
        features = {'evidence': 'unknown', 'reason': '无可读取的原生源文件'}
        if native_path is not None and native_path.is_file():
            try:
                features = describe_visual_form(read_json(native_path), resource_base=native_path.parent)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                features = {'evidence': 'unknown', 'reason': f'{type(exc).__name__}: {exc}'}
        records.append({
            "template_id": template_id,
            "display_name": entry.get("display_name"),
            "primary_category": category,
            "category_label": rule["label"],
            "visual_features": features,
            "semantic_use": {
                "use_when": rule["use_when"],
                "avoid_when": rule["avoid_when"],
                "spoken_cues": rule["spoken_cues"],
                "plain_purpose": purpose_row.get("plain_purpose") or rule["use_when"][0],
            },
            "content_contract": {
                "total_text_slot_count": len(text_slots) if candidate else int((entry.get("slot_counts") or {}).get("text", 0)),
                "editable_text_slot_count": len(editable_slots) if candidate else int((entry.get("slot_counts") or {}).get("text", 0)),
                "fixed_decorative_text": fixed_slots,
                "inferred_item_count_min": inferred.get("item_count_min"),
                "inferred_item_count_max": inferred.get("item_count_max"),
                "manual_slot_count": candidate.get('manual_slot_count', 0),
                "manual_style_slot_count": candidate.get('manual_style_slot_count', 0),
                "conservative_max_chars_per_slot": capacities or entry.get("observed_default_text_lengths") or [],
                "capacity_policy": "原占位文本字符数是默认容量参考，未适配时仍须遵守；当前视频明确授权的变体须记录实际全部文字轨及声明字数，并通过 final_layout 按最终绑定字体、字号和布局测量容量与碰撞；按当前阶段完成原生/用户验收，预览通过不等于生产可用，不得只提高声明字数绕过检查",
                "slot_audit_policy": "写入前检查全部文字槽；可编辑槽必须逐槽映射，固定标点和装饰槽必须明确保留，禁止遗留原占位文案",
            },
            "timing_contract": {
                "native_duration_seconds": native_duration,
                "animation_pattern": animation_pattern(candidate) if candidate else "unknown_external",
                "recommended_minimum_node_seconds": round(max(native_duration, floor), 3),
                "speech_fit_rule": "节点时长至少覆盖对应口播语义，并额外保留约0.2秒可读余量",
                "extension_policy": "保留原始入场和内部节奏；需要延长时只延长最终稳定状态，不拉伸整段动画",
            },
            "layout_contract": {
                "recommended_zone": rule["zone"],
                "ordinary_caption_policy": rule["caption_policy"],
                "adaptation_policy": ADAPTATION_POLICY,
                "collision_gate": "出画始终拒绝；人物遮挡按当前节点画面职责判断。普通字幕和无依据装饰应避让关键识别区域；人物已被明确设为模糊、定格、背景，或遮挡本身有用户/参考依据时，可按节点授权。普通字幕碰撞必须解决",
            },
            "current_video_contract": {
                "global_acceptance_is_not_current_video_readiness": True,
                "required_canvas_evidence": ["width", "height"],
                "required_render_phases": ["entry", "stable", "exit"],
                "required_phase_evidence": ["snapshot", "canvas_bounds"],
                "required_resource_state": "passed",
                "subject_overlap_policies": ["avoid_key_features", "intentional_overlap", "not_applicable"],
                "intentional_overlap_requires": ["design_basis", "authorized_by"],
                "current_video_ready_rule": "仅当当前画布、全部实际文字轨、逐槽容量、资源、入场/稳定/退场边界、字幕关系与人物情境判断均有证据并通过时为 true",
            },
            "technical_gate": {
                "compatibility_tier": entry.get("compatibility_tier"),
                "resource_gate": entry.get("resource_gate"),
                "reuse_class": purpose_row.get("reuse_class"),
                "visual_validation": entry.get("visual_validation"),
                "production_gate": production_gate(entry),
            },
            "source": {
                "source_path": entry.get("source_path"),
                "resolved_source_path": str(native_path) if native_path is not None and native_path.is_file() else None,
                "preview_path": entry.get("preview_path"),
                "semantic_tags": entry.get("semantic_tags") or [],
            },
        })
    category_counts = Counter(record["primary_category"] for record in records)
    production_counts = Counter(record["technical_gate"]["production_gate"] for record in records)
    return {
        "schema": "jianying-adapter.preset-usage-registry.v1",
        "policy": {
            "classification_is_not_visual_acceptance": True,
            "effect_development_reference": "reference_videos/analysis/教程学习与管线迭代_20260910/特效教程对应清单.md#执行对齐",
            "effect_development_boundary": "28项效果及其变体按清单开发；教程组合和待开发配方不是已可调用预设。先查实际能力与资源，沿用已有上下文选择，缺项在共享模块补齐后交同一CandidatePlan。代表组合的验证不自动覆盖其他变体。",
            "no_exhaustive_batch_visual_review": True,
            "on_demand_preflight": "未逐条验收的已分类预设不进入批量主观验收队列；被具体视频选中后，只对入选预设执行版本、资源、画布、全部实际文字轨、文字容量、入场/稳定/退场边界、人物情境关系和字幕碰撞检查",
            "design_candidate_default": "预设本身可作为设计候选；是否落到具体视频由语义、容量、时长、位置和技术门禁共同决定",
            "selection_order": [
                "spoken_semantic_role",
                "whole_film_expression_and_neighbor_context",
                "item_or_slot_count",
                "text_capacity",
                "timing_fit",
                "actual_layout_information_order_and_motion",
                "layout_and_caption_collision",
                "jianying_compatibility_and_resources",
                "visual_validation_gate",
            ],
        },
        "summary": {
            "record_count": len(records),
            "category_counts": dict(sorted(category_counts.items())),
            "production_gate_counts": dict(sorted(production_counts.items())),
        },
        "category_rules": CATEGORY_RULES,
        "records": records,
    }


def markdown(registry: dict[str, Any]) -> str:
    counts = registry["summary"]["category_counts"]
    lines = [
        "# 剪映预设使用场景说明书",
        "",
        "先确定整片表达与当前节点职责，再按槽位、容量、时长、实际表现和前后关系选择预设；选择结果不代表当前视频可写或视觉通过。",
        "本库服务手册第五步5.5的文字与阅读设计，5.8统一交接实际操作；完整5.1—5.8及后续检查见[完整管线](../docs/PIPELINE.md)。",
        "教程能力扩展见[28项特效清单](../../reference_videos/analysis/教程学习与管线迭代_20260910/特效教程对应清单.md#执行对齐)。清单包含待开发项，不能直接当作本库可调用预设；按具体变体、资源与验证状态使用，组合仍展开为同一CandidatePlan的实际操作。",
        "",
        "## 分类总览",
        "",
        "| 类别 | 数量 | 适合 | 不适合 | 普通字幕关系 | 最短停留基线 |",
        "|---|---:|---|---|---|---:|",
    ]
    for category, rule in CATEGORY_RULES.items():
        count = int(counts.get(category, 0))
        if count == 0:
            continue
        lines.append(
            f"| {rule['label']} | {count} | {rule['use_when'][0]} | {rule['avoid_when'][0]} | "
            f"{rule['caption_policy']} | {rule['duration_floor']:.1f}s |"
        )
    lines.extend([
        "",
        "## 预设选择与落地",
        "",
        "1. 先判断口播节点属于提问、结论、列表、步骤、数据、解释、普通字幕还是构图变化。",
        "2. 再匹配文字槽位数量与项目数量，不能把三项内容塞进双槽模板。",
        "3. 按实际文案核对槽位与容量；需要当前授权下的外观变体时，沿用 CandidatePlan 和 final_layout 检查，保留关键限定。",
        "4. 节点至少覆盖口播语义；若预设太短，只延长最终稳定状态，不拉伸入场动画。",
        f"5. {ADAPTATION_POLICY}",
        "6. 用 `preset-select PLAN --event EVENT_ID --output QUERY.json` 查询，带上整片目标、文字观察需求、前后段及已用预设。查看后用 `preset-use PLAN QUERY.json DECISION.json --output NEXT_PLAN.json` 保存取舍，再从新计划查询下一段，空或过期的已选上下文不能提交。该依赖不阻断素材检索和其他独立工作，也不要求每选一项就装配整片；已查看、选定的交接可用 `creative-batch` 按依赖保存到同一当前计划并失败续跑，参数见 CandidatePlan 合同。",
        "7. 只对入选项做版本、资源、画布、实际文字、容量、布局及当前阶段要求的检查。Agent原生代播不是已授权TEST写入前提；生产封版仍按 CandidatePlan 的三阶段证据要求。",
        "8. 人物遮挡不是一刀切：无依据遮挡拒绝；人物作为模糊/定格背景或遮挡属于明确设计意图时，记录设计依据和授权来源后按节点判断。",
        "9. 所有特殊信息文字明显大于普通字幕；同期同词只留一份，普通字幕缩为余句或关闭，两层共同保留完整含义。所选预设沿用原配音效和时序。",
        "10. 装配后读取现有报告的 visual_execution.sequence_summary；需要重新核对时运行 `creative-review PLAN --draft CANDIDATE.json`，已有可核实结果不固定重跑。核对字体颜色、同源音频、真实运动区间及辅助画面分布；attention_review 提示前3秒Hook与超过3.5秒未识别推进的区间，普通字幕换句不计推进，真实动作/阅读和运动有效性由主代理核实，再针对具体问题调整。音量风险按候选实际混音测量；不按预设数量判定质量。",
        "",
        "## 机器可读字段",
        "",
        "每条预设包含 `semantic_use`、`content_contract`、`timing_contract`、`layout_contract`、`visual_features`、`current_video_contract` 和 `technical_gate`。`visual_features` 读取原生引用、文字位置、时序、font_sources、text_colors、audio_sources。相同音频字节与相同缺失源引用分别报告；未知源保留 unknown，同源不等于已试听，也不代表真实播放。",
        "",
        f"完整注册表：`{OUTPUT_JSON}`",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    registry = build_registry()
    OUTPUT_JSON.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUTPUT_MD.write_text(markdown(registry), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT_JSON), **registry["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
