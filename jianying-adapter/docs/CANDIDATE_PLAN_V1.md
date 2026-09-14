# 候选构建计划 v1

目标：保留现有 Whisper/EDL/FFmpeg/剪映适配层和单 Writer，让观众的理解任务贯通到具体画面、资源、操作及最终时间轴。`CandidatePlan` 同时保存创作意图和可执行选择；各视频按内容设计，不从操作或模板倒推作品。[完整管线](PIPELINE.md)提供端到端入口，本文件维护技术合同。

## 重录裁决与实际剪切

当一路转写比最终文稿多出与相邻内容重合的重复片段（至少两个字符）时，语义检查要求主Agent解释这一差异，不自动判定应删。需要剪除的先修剪切、更新粗剪及证据；确属识别插入或有意重复的，在已有adjudication中保存`repeat_resolutions`列表，每项为`evidence_hash`、`extra_text`（检查报告指出的多余片段）、`action`（`asr_artifact`或`intentional_repetition`）和具体`reason`。这是重复候选提示，不能证明实际发音，也不保证发现所有口吃。不能用“不影响事实”代替判断重复是否有表达用途。

已决定删除的重录在现有multiclip edit_plan中保存`retake_cuts`，每项使用`source_path`、`source_start_us`、`source_end_us`和`reason`。区间为原片微秒半开区间；build_multiclip_base拒绝任何仍与保留段相交的删除区间。CandidatePlan用`edit_plan`路径引用同一份数据，不另抄删除表；装配及Writer实际回读也检查原生视频/音频源区间。存在重录删除时必须提供此引用。旧计划没有声明时不迁移、不声称已检查重录；源区间核对不能代替实际切口听感。

## 边界

- `CandidateAssembler` 负责读取计划、状态/语义/预设预检、按顺序调用操作、最终验证和一次性输出。
- 生产视频目录只保存数据计划和证据，不允许出现任何 `.py`；改名或放入子目录同样拒绝。操作实现必须位于 `jianying-adapter` 通用模块，并通过命名共享 `OperationRegistry` 注入；直接传 callback 字典会失败。通用能力缺失时修共享模块和测试，暂停依赖部分并继续独立工作，再从干净候选重建。
- 候选输出仍是 workspace-only JSON。注册真实剪映草稿继续由现有单 Writer 完成。
- 结构成功仍不代表画面成功。
- 自动语义检查不能代替必要的原音回听和内容判断；两路转写分歧必须保留可核验的裁决出处。
- 每条视频仍可选择不同字幕样式、预设、文案、位置和时点；统一的只是字段、检查和写入顺序。
- 适配层只执行计划，不把不同预设强行改成同一字号、配色或版式。

## 必需输入

### 必做画面交付

以后新建及整体重做视频统一使用v2，状态`visual_planning_min_version >= 2`自动具有以下通用下限；`required_visual_techniques`可提高要求，省略字段、漏写某类或写低值均不能降低通用下限：

```json
"required_visual_techniques": {"mask": 1, "video_effect": 3}
```

`mask`、`video_effect` 只接受正整数。既有候选预检、装配和唯一 Writer 从当前状态读取要求，不能用计划副本、调用授权、文字动画或重复引用抵扣。蒙版只计绑定有效事件的 `apply_video_mask`；特效只计 `add_video_effect`，预设自带效果完整保留但不抵扣额外要求。

实际草稿必须有对应视频片段、蒙版/特效引用、可用资源、正确时段及可见区间；全透明或隐藏对象不计数。未声明要求的旧任务保持原有行为。最低量用于防漏，不是每次制作的目标数量。主Agent先按内容完成充分的整片表现，不能每次卡在最低线；不足时继续增加有效表现，更高要求不降低。完整包装与用户验收仍按第五步执行。

圆形蒙版可使用项目现有技术参考 `assets/native_masks/circle_8_8_reference.json`，通过既有操作装配，不读取历史视频草稿。特效可以绑定主画面或同计划新增的辅助视频段；所选原生预设的自带效果与明确添加的画面特效分别回读，避免误删、漏装和重复计数。


### 包装判断先于预设选择

整片包装遵循[完整创作的执行原则](../../AGENTS.md#完整创作的执行原则)，先按记忆路由`packaging_design`直接读取[剪辑手册首版工作法](../../reference_videos/analysis/教程统一整理_v1/教程使用说明书_v1.md#首版工作法)及五类决策表，再核对视觉偏好、素材与共享能力边界；随后仅按实际设计补读问题Memo。
第五步职责只引用当前[手册首版工作法](../../reference_videos/analysis/教程统一整理_v1/教程使用说明书_v1.md#首版工作法)，不在本合同另维护一套步骤。5.6已取消为独立规划：模板自带动画直接沿用，额外运动参数在5.8依据既定表现、时间和构图落实；必要程序检查随装配执行，实际运动、裁切、遮挡与前后衔接交用户播放审核，不增加独立Agent截图或切片检查。开场及段落表现、逐段声画和关键构图共用同一CandidatePlan；各字段在对应步骤记录，装配时完成实际资源与operations绑定，不另建计划或审批。
既定观察与表达目标不能因实现方便被删减；具体形式可在完成同一目标的前提下调整，按手册5.4让字幕与辅助元素适配主要观看对象。普通字体调整不等于使用真实预设，恒定关键帧只表示固定构图，不能作为实现运动的依据。这里是设计与表述要求，不代表现有校验器已能自动识别全部此类降级。
不要求每条视频用齐效果或达到固定数量；选用与不选用均从当前内容及用户偏好出发，不能以“不堆效果”为由跳过必要设计，也不能先缩减操作再补设计理由。

既有能力入口（可执行与原生视觉通过分开）：

| 需要什么 | 已有实现与衔接 | 当前边界 |
| --- | --- | --- |
| 粗剪逐段原声音量 | `scripts/build_multiclip_base.py` 的 `multiclip_edit_plan_v1.timeline_segments[].volume`，写入原生 `VideoSegment.volume` 与 EDL/time_map | 可选非负有限数值，默认 `1.0` 保留原声，`0` 静音；与 `speed` 独立。口述段可保持默认，动作观察段用 `volume: 0, speed: 1.0` 保持原速；布尔、字符串、负数及非有限数值拒绝。仍为工作区基础草稿，再交 CandidateAssembler |
| 粗剪逐条字幕位置 | 同一 `build_multiclip_base.py` 输入 `captions[].zh_transform_y`，例如 `{"zh":"翻看内网","start_us":1000000,"duration_us":1500000,"zh_transform_y":0.6}` | 可选有限数值 `[-1,1]`，拒绝布尔、字符串、null及非有限数；只覆盖该条位置，缺省回退 `subtitle_options.zh_transform_y`，两处均省略时保持原多行布局。文字及时间不变，`subtitle_plan.cards[].zh_transform_y` 记录实际使用值；启用英文时沿用原双语相对间距 |
| 人物景别、位置关键帧 | `split_video_track` → `animate_aroll_transform`；底层还有`animation_writer.compile_native_keyframes` | 原速粗剪可先显式分段，再按segment_id做局部变换；后者是底层编译器，不代表任意组合都接好了 |
| 人物蒙版 | `apply_video_mask`复用显式冻结的双8.8原生参考；vendor只用于计算几何 | 圆形已有符合规则的结构依据；矩形须另有合格参考，不能直接复用旧冲突开关。底层add_mask默认输出不等于8.8兼容 |
| 原生视频特效 | `add_video_effect`，独立effect轨或指定视频片段挂载 | 必须绑定本地完整资源、当前授权与visual_event；SDK构造和缓存存在不代表剪映8.8实际播放通过 |
| 人物缩小、素材展开 | `animation_recipes.person_shrink_broll_expand` | 现有设计配方，标记`unverified`；不能把配方存在称为整组可直接生产 |
| 资料画面、双画面 | `add_broll`与声明的轨道层级/布局 | 需要真实冻结媒体和当前授权；单个视频插入能力不等于任意图文卡片 |
| 原生文字组 | `add_preset_group`、`place_preset_group`等 | 当前画布、字体、资源、容量和三阶段验收继续保留 |
| 现有文字片段入场 | `apply_text_animation`复用`animation_writer`与SDK `TextIntro` | 明确单段、免费枚举与本地冻结资源；不需要导入整套文字预设，原生效果仍待当前TEST验收 |

新建或整体重做的包装计划设`project_format.visual_planning_version=2`，在同一CandidatePlan中写`visual_events`，
并在对应项目状态设`visual_planning_required=true`、`visual_planning_min_version=2`，防止省略或降级；旧计划v1仍可读取，不自动改写历史产物。
不另建审批或报告流程。5.3只负责所选表现的使用区间及台词时间绑定；表达职责与选择承接5.2，具体构图在5.4—5.5完成后写入同一计划。下列完整字段是装配时的要求，不是在5.3重新执行全部设计的步骤。
每个事件声明`id`、`start_us/end_us`、`audience_need`、`primary_visual`
（`person/media/relationship/text`）、`composition`，以及已选`techniques`及对应的`operation_ids`。
`techniques.kind`当前支持`text_preset/text_animation/keyframes/mask/media/sound/video_effect`；普通人物保持原样可显式写空列表。
预检会拒绝已选效果没有操作、引用不存在，或把蒙版/关键帧连接成文字操作。未知能力明确停止，先接通共享实现。
v2在上述字段上增加：

- 每个事件的`speech_ids`引用`content_gate.final_retained_speech`的`id`、其次`speech_id`、再其次原列表下标字符串。全部保留语音必须有设计承担；一句跨多个事件时，所引事件的时间并集须完整覆盖该句，不强制一语句一效果。无口述标题或纯画面节点可显式写`[]`，不能省略字段。
- 每个事件的`handbook_refs`为实际适用的手册章节/案例定位列表，`review_focus`为待观察的具体结果。比如“头肩和讲解手势在整段内自然显示”，不能用“mask通过”代替。这是设计目标，不能当作已取证。
- 每个事件新增`supporting_visual: {status, purpose, operation_ids}`，状态为`ready/needs_asset/not_needed`。`purpose`说明本段补充画面的信息或构图职责；`not_needed`只适用于该段确实由人物/文字承担的内容，不能与资料/关系主视觉或已选素材/蒙版矛盾。整片完整包装须有实际辅助画面及主次构图，不能全部省略。
- `ready`绑定本事件全部`add_broll/apply_video_mask`操作；图片、图表、背景等也经现有`add_broll`作为实际媒体入画。素材文件、蒙版原生参考须以存在的绝对路径绑定。装配及Writer回读逐项核对真实轨道/片段、资源路径、可见开关、蒙版引用。当前意图计划按每条`visual_requirement`所需观察区间检查交付，允许辅助画面只在该段需要时出现；旧格式仍按整个事件覆盖检查。空操作或隐藏画面不能证明已完成。
- 缺素材用`needs_asset`，在同一对象的`asset_search: {queries: [...], next_action: "..."}`保留实际检索词及后续动作。Agent主动查找本地/外部素材，或制作准确的图表、卡片、背景与示意；入选前查看实际画面及其内嵌文字，避免素材标题与字幕重复。缺少必要资源时继续处理独立工作，但该视觉段与完整包装不能标为已完成。该检查不自动代替检索、来源判断或画面阅读。
- 新选辅助媒体按[共享素材系统](MATERIAL_LIBRARY.md)执行：`material-search`查询本地库及`sucai`来源；Agent实际检索并查看，不合适再用`material-generation-brief`准备内置生图；`material-register`冻结实际文件，`material-use`输出同一CandidatePlan的`operation`和`broll`声明。操作内`media_asset`绑定`asset_id/ledger_path/request_path/visual_review`；需求的`visual_event_id`必须引用当前事件并连接`supporting_visual.operation_ids`。原始口播的分屏副本仍走原有素材路径，不必重复入库。新选素材不能省掉该绑定；旧计划兼容读取，不自动改写。
- 新需求先在同一事件的`supporting_visual.request_paths`引用当前需求文件，已取得素材后继续保留路径。`material-requests PLAN`读取待办，`material-use REQUEST USAGE --plan PLAN --output 同目录新计划.json`同时连接实际操作、B-roll声明、media technique及辅助画面引用；多需求只完成其中一个时保持`needs_asset`。候选及Writer沿现有`validate_plan_materials`从需求和操作两边核对，已声明需求无合格素材、错绑其他事件或改为`not_needed`均会报错。旧计划未声明该字段继续兼容；新计划按手册先提出需求，不能等操作完成才补理由。
- `media_asset`从候选预检、`add_broll`装配到Writer回读复用同一检查：来源/授权、实际文件哈希、需求尺寸/透明度、当前查看范围、源区间和实际素材路径。首次登记与后续复用的画面判断分开记录；缩略图或抽帧只能记录为对应检查范围，不自动升级为连续播放。此处不新增首版原生代播门禁或用户审批。
- `design_decisions`五领域use/omit表仅保留旧计划兼容检查；新意图计划用下节的整片策略与观察需求表达具体决定，不再额外填写一套使用效果的勾选表。若保留旧表，仍检查声明和实际操作是否矛盾。运动从最终有效变化关键帧、原生动画及对应时段读取，恒定变换不算运动；音轨须引用存在的资源。静态特效不自动算运动，未解析的原生效果留给实际验收。
- 语音复用计划内或现有`semantic_gate`内的`content_gate`，不维护第二份语音事实。版本最低要求由当前状态读取；预览、封版及Writer复用同一候选校验入口。

### 观察需求到实际交付

当前所有v2候选构建入口要求`creative_brief`和逐段`visual_requirements`。旧文件可只读复核，已写草稿不自动迁移；对旧片作定向调整后需要重建时，按原有设计补齐意图和绑定，只改反馈涉及的表达，不重新选整片方向。版本仍为2，不另建Writer资格或审美通过字段。

`creative_brief`含四个具体字符串：`viewer_takeaway`（观众最后理解什么）、`progression`（各段如何推进）、`visual_strategy`（当前人物/产品/资料如何交接，实际字体配色和参考特征）、`sound_strategy`（原声、预设原配声与必要声音的职责）。这些仍是当前代码的数据要求：从第二步已确认内容、5.2已选表现及后续实际设计简要归纳，复用已有事实，不另开核心目标提炼、稳定/变化分布或重复策略规划；本轮流程精简不声称已删除这些程序字段。具体事件记录实际设计，不以“丰富、自然、好看”代替方案。

新视频须在`creative_brief.hook`记录`event_id/strategy/reason`，绑定从0开始的开场事件，按手册5.2从少量固定开场模板中选用强视觉冲击表现，`strategy/reason`简要记录实际所选表现及与开场原话的适配，不另写抽象Hook策略；第二步只做准确转写与有效内容筛选，保留片段沿原片讲述顺序，不前置句段或跨段重排；Hook在第五步围绕保留的开场原话设计画面与包装。共享多素材入口技术上支持`timeline_segments`指定目标顺序，但该能力不改变当前流程保持原片顺序的要求。第三步完成粗剪与最小必要复核，发现切错、漏剪或错误衔接时立即修正并重剪受影响部分，重新核对修正处及相邻接缝，具体见[完整管线](PIPELINE.md#第三步内的最小必要复核)。当前程序仍要求与实际粗剪文件绑定的两路语义证据；有效结果复用，切点改变导致粗剪文件变化时更新映射与证据绑定，再确定`final_retained_speech`及字幕目标时点，不能只改旧证据时间戳。后文“原句/原顺序”以保留原片讲述顺序的已核对粗剪台词为基准，包装阶段继续沿用。收尾行动在既有整片策略及事件中按当前业务目标安排，不统一要求私信或引流。

每个事件的`expression_role`说明整片职责，`visual_requirements`列出观众必须看清的内容。需求包含全片唯一`id`、具体`subject`、`observable`及`delivery: aroll/media/text`；可选`start_us/end_us`限定观察区间，默认整个事件。`time_behavior`为`still/continuous_action/graphic_motion`，默认still；连续动作须真实视频，静图平移不能替代动作过程。

| delivery | 装配前的真实绑定 | 最终草稿核对 |
| --- | --- | --- |
| aroll | `track_name`与`source_observation: {source_path, start_us, end_us, scope: video_frames/video_segment, observation}`；主画面或原始口播同源小窗 | 实际源文件、已查看源区间、目标可见区间；已有动作由观察记录说明，不自动识别动作含义 |
| media | 当前`request_path`及对应`add_broll`的`operation_ids`，请求带相同`visual_requirement_id/subject/observable/time_behavior` | 来源绑定、文件、源区间、裁切/布局和需求观察区间完整交付；不能用其他图片、文字或人物蒙版代交 |
| text | 普通字幕的`track_name`，或选定预设的`operation_ids` | 真实文字轨道覆盖观察区间，准确文案、去重与限定词仍由现有内容和字形检查负责 |

`delivery=aroll`的`source_observation`可按实际取景需要补充`content_region: [left, top, right, bottom]`和`source_sha256`。坐标沿用源归一化坐标，表示已观察源区间内必须保留内容的静态包围范围，不是最终画布位置；`subject`继续由所在需求提供，原有`observation/scope`保留真实观察说明。声明内容区时必须绑定实际源文件摘要；仅有路径相同不能复用被替换文件的观察。同一验证调用内每个源文件只核对一次摘要，旧计划未声明这两个字段时保持原有检查。

`creative-review`及装配报告的对应观察需求返回`source_content_region/source_sha256`，可将该区域交给现有`framing.content_region`取景求解。装配回读把声明区域与真实原生素材crop比较，切掉必要内容时报错。更改输出裁切不抹掉源观察，只重新计算、检查当前取景；更换源内容或使用未观察区间时须补充观察。此处不自动识别对象、清晰度或遮挡，不证明缩放后的有效尺寸、蒙版可见性或全程跟踪；这些仍由当前构图和原有检查处理。源观察、输出构图和本片适配结论分别复用，不复制维护另一份crop/scale，也不新增首版Writer资格。

声明`graphic_motion`还会检查对应最终轨道是否存在实际变化；静态首尾关键帧不能交付运动需求。该判断确认数据中存在变化，不评估幅度是否自然或肉眼是否容易感知。

```json
{
  "id": "show_texture", "subject": "当前产品的实际质地",
  "observable": "看清表面纹理与指尖接触的位置",
  "delivery": "media", "time_behavior": "still",
  "start_us": 4000000, "end_us": 6500000,
  "request_path": "materials/texture-request.json",
  "operation_ids": ["texture_closeup"]
}
```

这是结构示例，源文件、观察与操作必须来自当前视频；规划阶段允许留未绑定需求，构建时须解决。段落作用与表现选择在5.2保存，5.3只对时，源观察与取景在5.1/5.4复用或补齐，5.5实际填字与字幕分工，5.8直接引用这些数据、补齐执行参数并装配；不在写下需求时就要求装配已完成。

`creative-review PLAN`按需列出规划缺口：成功读取并完成检查时退出0，未解决项仍以各子报告的`errors`及`ok=false`如实呈现；文件或解析错误仍报错。`creative-review PLAN --draft CANDIDATE.json`核对实际交付，检查失败退出1。二者复用现有验证器，不赋予写入资格；CandidateAssembler及Writer继续拒绝必需绑定或实际交付缺失。已有装配报告能回答当前问题时无需固定再跑一遍；内容、来源、资源和Writer检查继续执行。

按真实依赖安排工作：预设选择落盘后再作为后段的已选上下文，素材检索、源观察及无依赖的构图可独立推进。`material-use --plan`已自动连接的操作、B-roll及事件引用无需另填一套交接；仍须由主代理确认取舍、实际文案和关键构图。这里不增加逐段审批、第二套计划或审美评分。

### 教程特效扩展的执行边界

2026-09-10执行对齐：全部28项及其变体的开发依赖与状态见[特效清单](../../reference_videos/analysis/教程学习与管线迭代_20260910/特效教程对应清单.md#执行对齐)。以下是扩展现有共享操作时的要求，尚未实现的新能力不因此获得可调用资格；清单中的开发字段不写进CandidatePlan当作运行接口。具体新增字段与命令随相应实现、验证一起维护在本合同及素材文档，继续使用现有v2、OperationRegistry、CandidateAssembler和Writer。

首批已接入共享`material-process`及`source_kind=derived`，具体参数和格式见[素材文档](MATERIAL_LIBRARY.md#局部合成与派生素材的开发边界)。它先生成可查看的定格图或经过倒放/恒速/抽帧/饱和度处理的视频，实际查看后沿原register/use绑定`delivery=media`，没有新增CandidatePlan枚举或Writer。加工输出有自己的0起点时间；目标保持、取景及后续关键帧仍在当前计划内安排。v2候选集成测试覆盖末帧保持和倒放后原生缩小，回读保持原来源与派生文件绑定；这是合成输入的结构/解码验证，未证明人物抠像、透明视频8.8渲染或整片观感。

| 边界 | 已有基础与执行要求 |
| --- | --- |
| 观察对象与交付方式 | 沿用`visual_requirements`和当前绑定。原片呈现要核对真实源区间中的对象、部位和动作阶段；新增原生运动要对应准确轨道、片段、参数及落定构图；派生素材要核对源到输出关系。`delivery`仍为现有aroll/media/text，三种实现说明不新增同名枚举 |
| 时间基准 | 明确原始源时间、裁切片段局部时间、派生媒体时间及最终时间轴时间。现有时间字段以微秒为单位，区间按起点含、终点不含处理；关键帧偏移沿目标操作定义，不能把源时点直接当时间轴时点。恒定正速时按源起点、目标起点和speed换算；倒放、冻结、抽帧或变速段分别保留映射，按实际帧率和帧时间检查边界 |
| 坐标基准 | 现有source_crop/content_region为源归一化坐标，target_rect为画布像素，原生clip.transform为中心0、右/上为正的归一化坐标。蒙版窗口与框内内容分别控制。扩展成员局部坐标、锚点、旋转或透视时明确转换顺序，不能把源像素或裁切前轨迹直接写成原生位置 |
| 组运动与原生动画 | 复用`preset_motion.py`已有共同anchor和时间映射；当前预设组能力主要限于稳定期。跨媒体、文字、前景或蒙版的联动需明确成员、层级、共同锚点、各层起止及与原生动画的组合顺序。新能力不能默认覆盖进退场或循环；校验失败不得留下部分已修改成员 |
| 源轨迹与标注 | 现有源观察证明实际查看范围，不自动证明跟踪正确。跟随、抠像、变形和遮挡只对有关源区间加密观察；检查起止、转折、遮挡和中途丢失。标注位置来自真实部位，经过裁切和时间映射后仍须对应；仅首尾对齐不足以证明全程跟随 |
| 配方与声音 | 复用`animation_recipes.py`和已有预设/操作，不把当前配方数据结构当完整执行器。组合落成可追踪的实际operations；`action_audio.py`与`preset_audio.py`继续保留原配源区间、提前/延后和声画关系。新增锚点按实际动作或语义安排，音量峰不等于语义重音 |
| 实际回读 | 现有`graphic_motion`已检查非恒定变化，未验证对象含义与审美。扩展时对照目标片段、变换端值、必要中途状态、派生文件及实际绑定；`scale=1`可由原片动作交付，微小非零变化也不能证明观察任务已完成。复用`creative_delivery/sequence_summary/final_layout`，不另起平行报告或评分 |

每个共享能力按实际变化补相关测试；同一实现和相同条件的验证可复用，具体效果的资源、变体、对象和时序仍需对应装配实例。原生结构或资源的兼容证据、离线媒体检查、实际8.8结果和用户观感分别记录，不由文档或样例数量推导通过。缺新的原生参考时先查已有8.8证据，确需GUI则依当前授权处理；独立工作继续，Agent完整代播不成为已授权TEST的前提。派生媒体的来源、透明度和输出约束见[素材文档](MATERIAL_LIBRARY.md#局部合成与派生素材的开发边界)。

### Hook、节奏与分镜视图

`creative-review`及装配报告`sequence_summary.attention_review`复用同一诊断：检查Hook声明，按最终可见轨道列出新的特殊文字、视频来源/构图变化及运动区间，定位超过3.5秒未检测到推进的`long_holds`。普通字幕换句、同源同构图连续切段不计推进；隐藏片段不计入。`review_focus`与真实连续动作的源观察一并列出，供定位既定计划中的动作或阅读用途，不新增装配后的截图、切片审核。默认每2—3.5秒一次有效视觉推进是创作要求，不规定每个事件时长或效果数量。

该报告是诊断：Hook声明不证明吸引力，数据有运动不证明肉眼可感知或有信息价值，原片动作不能仅凭切段自动判断。缺少Hook或存在长停留时返回`warnings/review_required`，不新增审美分数或Writer资格，不改变既有构建错误的退出码；规划仍须依据实际内容，不能把普通字幕或无信息抖动算作完成；报告警告本身不触发独立Agent画面审核，实际观感交用户播放判断。旧计划未声明Hook仍可只读复核，原生/用户验收状态不变。

按需执行`creative-review PLAN --storyboard E:/当前项目/分镜.md`，从同一计划生成时间、段落职责、观看任务、构图与观察需求的Markdown视图。输出须为未存在的非C文件；它不生成另一套计划，也不证明实际字体、构图或新增前置审批。

### 批量执行已选交接

`creative-batch PLAN MATRIX.json --output 同目录当前计划.json`复用现有选择器与素材系统，保存一个可继续交给CandidateAssembler的CandidatePlan。矩阵只组织已查看、比较并作出的决策；`preset`执行时在已保存前段上下文中重新查询，再通过原`preset-use`验证保存，不自动挑选预设或编造查看记录。

```json
{
  "items": [
    {
      "id": "opening_choice", "action": "preset",
      "decision": {
        "event_id": "opening", "node_id": "opening_title", "template_id": "实际模板ID",
        "reason": "当前取舍理由", "compared_with": [{"template_id": "实际比较项ID", "reason": "差异与取舍"}],
        "reviewed": {"source_path": "E:/实际源.json", "scope": "source_structure", "observation": "实际查看结果"}
      }
    },
    {
      "id": "detail_media", "action": "material", "request": "materials/detail-request.json",
      "usage": {"asset_id": "已入库ID", "operation_id": "detail_closeup"}
    }
  ]
}
```

这是字段结构示例；素材`usage`仍须提供[素材合同](MATERIAL_LIBRARY.md#交给现有装配器)的时间、取景和真实查看记录。各项ID唯一，可用`depends_on: [item_id]`声明依赖；`request/config/registry`路径相对矩阵。省略config/registry时沿用现有库。三类action：

- `preset`：必需`decision`；可一起提供已有合同的`operations/presets`，自动核对并接回事件和文字需求。省略操作时只保存选择，不能算装配完成。
- `material`：必需`request/usage`；可带现有`selection`先入库。入库、取景求解和回接仍用原验证；复用素材只给asset_id，无需重登。
- `search`：按`request`批量返回本地候选及导航信息；外部实搜实看、下载和生成仍由Agent在当前授权内执行，命令不代替它们。

每项成功立即原子保存；失败项保留原因、阻断依赖项，独立项继续。修正失败输入后加`--resume`续跑，成功项不会重做；中断后已完成的同素材入库可核对后复用。原计划、已完成输入或当前输出被改动时拒绝沿用旧进度；需要更改已完成设计时以当前计划另开批次。输出必须与原计划同目录、不同文件且不在C盘，保留相对路径含义。同路径的锁防止批次并发写入，进程退出释放，不需人工删锁。此入口不写真实剪映草稿，也不替代内容、资源与最终装配验证。

新批次把完整预设查询保存到输出旁的`<计划名>.queries/`，`_batch.items[].result`仅保留入选ID、理由、操作数及`query_ref: {path, digest}`。引用路径相对当前计划；续跑核对查询原文及摘要，缺失或变化时拒绝复用完成项，不能只留digest后丢弃原文。旧内嵌`result.query`的进度仍可续跑，不主动迁移历史计划。批次保存逐项`elapsed_seconds`，命令结果返回本次`attempted/reused/elapsed_seconds`；逐项时间覆盖执行与查询保存，总时间另含输入读取和进度保存，不是整片制作耗时或视觉评分。

所有适配器CLI可在子命令前传`--timing-log E:/.../timings.jsonl`，按行追加`command/started_at/elapsed_seconds/exit_code`，不改变原命令JSON输出。仅记录实际执行的命令，不推断用户介入、素材观察或语义阶段耗时；`creative-review`规划模式的退出0也不代表全部设计检查通过。例：`python -m jianying_adapter.cli --timing-log E:/.../timings.jsonl creative-batch PLAN MATRIX -o CURRENT`。真实制片时将其用于相关现有入口，首次能力修复和常规制作分别分析。

### 顺序选择预设

共享选择直接读取上述事件。`transition: {intent: hold/change, reason, axes?}`说明与前段延续或变化，`axes`可用`text/sound/composition`，缺省按text处理；构图变化不自动要求换文字形式。需要预设的事件写`preset_request: {requirement_id, intent, item_count?, needs_sfx?, preferred_visual_features?}`，绑定本事件delivery=text的需求。`intent`为当前文字用途（如`question_hook/quote_conclusion/parallel_list/numbered_steps`）；叙述的收束、产品标签和三项比较各自判断，不能统一写成金句。`preferred_visual_features`按需要选layout和information_order。

`preset-select PLAN --event ID --output QUERY.json`复用`preset_registry.select_template`，默认查询完整使用库，从意图、需求、前后段和已保存的选择取得上下文；不必等装配操作生成后才知道上一段选了什么。先保留匹配表达的候选，排除已知不兼容/视觉退回及未允许的人工槽位，再排列同用途选项。不跨职责自动回退，不把未知特征算作新表现。`top_k`仅为返回候选数；资源、实际文案、字体和画布仍需预检，查询不赋予写入资格。

查看候选后执行`preset-use PLAN QUERY.json DECISION.json --output 同目录新计划.json`，将选择保存在当前事件的`preset_selections`，再使用新计划查询下一段。DECISION含`event_id/node_id/template_id/reason`、实际比较的`compared_with: [{template_id, reason}]`，以及`reviewed: {source_path, scope, observation, preview_path?}`；scope为source_structure/image/video_frames/video_segment，画面查看须引用实际预览文件。结构查看允许用于已授权TEST的选择，但不能声称原生播放。选择不替代现有presets/operations装配参数，构建入口会核对选择、文字需求与实际node/template一致。

查询时前段未选择，或查询后当前需求/前段选择改变，旧查询不能提交；须用当前计划重新比较。后段后来选定产生的邻接变化只进入整片复核，不循环作废前段。修改前段选择会提示重新核对受影响的后段，不强迫全片重新设计。

使用库的`visual_features`读取原生文字位置、显示顺序、入退场、变化关键帧，以及实际`font_sources/text_colors/audio_sources`。`comparison_to_selected`分开报告相同布局、字体、颜色、音频字节和缺失资源引用；相同音频文件不代表已听过或听感相同。`source.resolved_source_path`提供当前可读源，未知项不凭名称补事实。主代理按内容比较并决定使用原样还是当前允许的文字外观变体，所选成套预设仍承接作者原配声音。

选预设时同时判断入场与主要停留阶段：结合已有预览或源结构、当前文案及计划摆位，在现有`reviewed.observation/reason/compared_with`中记录落定后的字形、配色、字级和布局取舍，不只比较名称或入场动画。开场落实既定强视觉冲击，重点文字与普通字幕形成明显层级；同字体、同尺寸、同位置仅作为比较依据，结合配色、背景关系及信息组织判断实际表现。符合当前表达的有意统一可以保留，表现重复且未满足当前表达要求时才定向调整，不因动画ID不同就视为已形成变化。资源导入失败时，修共享能力或选择能保住表达目标、表现强度及用户要求的可用替代；具体构图和出现方式允许重新设计，不能只因能导入就降低交付要求。以上沿用5.2选择和5.4—5.5落位，不新增字段、独立审图或审批；结构判断不冒称实际播放效果。

装配与Writer回读的`visual_execution.creative_delivery`核对每项观察需求。`sequence_summary`列出实际预设、普通/特殊文字字体与颜色、音频来源和复用、发生变化的运动操作区间、辅助媒体并集、蒙版及各视频层的源区间/裁切/clip。相邻相同机制保留为复核事实；静态变换与实际运动分开，素材短暂出现的准确时长可直接定位。报告不能证明上层遮挡、运动感知或实际主次合理。程序具体错误或用户播放反馈用于定位问题，再作定向调整；无辅助媒体时段不等于静止，不以数量配额替代判断。

关键构图在5.4—5.5以真实画布、裁切、字形/媒体/主体保护区包围盒和布局参数落实；5.8直接装配。用户2026-09-13取消独立第八步Agent画面审核：不另开预览、截图或切片审图，实际观感交用户播放审核。候选和Writer各自必要的程序门禁保留，直接复用当前有效的`final_layout`等报告；不取消资源、参数、内容及写后回读校验，也不重复人工复核整套数据。未测原生范围如实列出，`native_visual_verified=false`、`creative_quality_verified=false`不能伪改为通过。程序确切报错或用户具体反馈触发定向修改，只修相关时段、对象及直接影响；工具必须重建候选时复用未受影响的计划，不默认重新设计整片。结果记录在现有项目状态`internal_review`。未进行Agent原生播放、画面抽样或完整试听不阻止已授权的独立TEST写入；生产封版所需现有证据与门禁不因取消独立审核而自动取得或关闭。

具体选择参考[现有教程使用说明书](../../reference_videos/analysis/教程统一整理_v1/教程使用说明书_v1.md)及其五类研读补充。`audience_need`与`composition`应描述当前素材如何承担信息；人物蒙版要分别判断框内裁切与画布位置，不能把“已连到mask操作”解释为人物构图正确。声音可通过共享 `mix_action_audio` 显式编排；教程推荐不等于当前素材适合或已经完成试听，不另造审美评分或效果数量门禁。

```json
{
  "project_format": {"visual_planning_version": 2},
  "creative_brief": {
    "viewer_takeaway": "理解两个选择的实际差别",
    "progression": "先建立问题，再把两个选择放在同一标准下比较",
    "visual_strategy": "比较信息并列为主，人物解释为辅；按当前参考选字体与配色",
    "sound_strategy": "原声说明比较，使用成套预设时保留原配声音"
  },
  "visual_events": [{
    "id": "choice", "start_us": 25850000, "end_us": 29210000,
    "audience_need": "看清两个选择", "primary_visual": "relationship",
    "expression_role": "比较两个选择",
    "transition": {"intent": "change", "reason": "从单一观点转入同时比较，画面须展示两个对象的关系"},
    "composition": "人物为辅助卡片，两个选择并列展示",
    "speech_ids": ["speech-choice"],
    "handbook_refs": ["教程使用说明书_v1.md#第一性原理总规则"],
    "review_focus": "人物窗口与两栏信息都能看清",
    "visual_requirements": [{
      "id": "compare_labels", "subject": "两个选择在同一标准下的说明",
      "observable": "观众同时读清两栏差异", "delivery": "text",
      "track_name": "EXAMPLE_COMPARISON_TEXT"
    }],
    "supporting_visual": {"status": "ready", "purpose": "人物窗口为两栏信息让位", "operation_ids": ["person_card_mask"]},
    "techniques": [
      {"kind": "mask", "operation_ids": ["person_card_mask"]},
      {"kind": "keyframes", "operation_ids": ["person_position"]}
    ]
  }]
}
```

示例仅为字段说明；实际`operations`还必须包含同名完整操作和真实输入，不能把此片段当可写候选。

以下完整放行要求适用于生产封版与生产 Writer。首版供用户检查的路径是：
`planning` → `candidate-preview` 生成禁生产写入原生 JSON → 随装配执行必要程序检查并修复具体错误 → 已授权的测试Writer登记独立草稿 → 用户打开、播放和验收。Agent原生播放及截图不是这条路径的前置条件。
后续需要生产封版时，取得合同要求的当前原生画面证据，再由`candidate-seal`全部核验通过后推进`candidate_ready`，生产 Writer 写后推进`written_pending_visual_qa`。不能在预览前要求最终截图，也不能先手改 ready。
预览 JSON 不是渲染画面；未取得合同要求的原生画面证据时，生产封版保持阻塞，已授权的独立测试草稿可先写入供用户检查。
它不能通过把 FFmpeg 粗剪帧、排版示意图或测试夹具改名来解锁。
预览冻结素材、操作、文案、时点、位置与缩放等构建合同；之后只允许补充
`current_video_evidence`、`current_video_ready` 和 `frontend_proof` 证据字段。
封版再绑定完整最终计划与候选文件哈希，Writer 重新核验。封版失败保留失败产物，
manifest 明确禁写，状态恢复为 `planning`；修订后使用新的输出路径，从预览重新开始。

计划 schema 为 `jianying-adapter.candidate-plan.v1`，至少包含：

- `project_state`、`base_draft`、`rough_cut`、`semantic_gate`、`usage_registry`；
- `semantic_gate` 引用两路实际转写证据文件，并提供 `content_gate.final_retained_speech`、
  `ordinary_subtitles` 与可选的 `preset_replacement_windows`；最终构建与 Writer 还会从实际草稿
  `JY_ZH_SUBTITLES` 重新读取字幕并检查完整文字与时间覆盖。原始证据缺失或未裁决分歧不能靠摘要放行；
- `project_format.candidate_builder: CandidateAssembler`、`project_format.per_video_build_script: false` 与 `project_format.operation_registry: jianying_adapter.shared-operations.v1`；
- `target.width/height/jianying_version/expected_exe`；`expected_exe` 必须指向实际存在的8.8 `JianyingPro.exe`；
- 当前视频 `options`；启用可选功能时必须有与 `project_state.current_authorizations` 有效记录匹配的 `authorization_source`，禁止项优先；旧 confirmed 长文不能自动授予可选能力；
- `subject_clarity_preflight`：状态必须为 `passed`，且主体、头顶空区、背景干扰、透视、字幕避让五项检查全部为 `true`；
- 可选的 `text_style_budget`：只在当前视频状态明确确认风格预算时填写；适配层不设项目统一的“两套文字风格”上限；
- `transition_requires_real_before_after_state: true`；转场操作必须显式标记 `visual_role: transition`，并记录不同的前后状态 ID 与边界类型；
- 当前入选的 `presets` 及其 `current_video_evidence`；
- `frontend_proof` 为可选诊断记录，日常封版不要求 CUA、SKY、Doctor 三套证明。提供时可核对当前项目、粗剪、目标窗口和弹窗，异常只作为诊断提示，不独立决定生产放行；不能冒充真实视觉证据。实际操作时出现的阻塞弹窗仍须处理，用户自行验收不依赖 Agent 的电脑控制工具；
- 每个预设必须有 `node_id`、`start_us`、`end_us`，且 `node_duration_us=end_us-start_us`；每条 `actual_text_tracks` 还要声明自己的 `start_us`、`end_us` 与 `segment_count`；
- 存在 B-roll 时必须提供 `brolls`，逐项声明 `node_id`、唯一 `JY_BROLL_` 轨道名、冻结后的 `source_path`、`start_us`、`end_us`、`segment_count` 与最终 `clip` 布局；
- 有唯一 `id` 的 `operations`；
- `output`、`report`、`manifest`，以及互不覆盖的 `preview_output`、`preview_report`、`preview_manifest`。

完整原生预设承担一句台词时，可在 `content_gate.preset_replacement_windows` 声明：

```json
{
  "id": "quote-caption-handoff",
  "start_us": 1000000,
  "end_us": 3000000,
  "text": "你永远赚不到认知以外的钱",
  "replacement_for": ["speech-02"],
  "ordinary_subtitle_replacement": true,
  "complete": true,
  "template_id": "所选预设ID",
  "node_id": "quote",
  "track_names": ["JY_PRESET_所选预设ID__NODE__quote_0"]
}
```

`replacement_for` 按原顺序引用 `final_retained_speech` 中一个或多个相邻整句的 `id`（无 `id` 时取 `speech_id`，再无则取原列表下标字符串），不得跳句或重复引用。
窗口完整文字须保留原台词；只忽略空白和英文大小写，不因是花字而删否定词或改句。
多个原句按引用顺序拼接后须与预设完整文字相等。窗口须包含全部所引语音跨度；允许在没有其他未引用语音的空隙中延伸，以保留原字幕的停留时间，不得吞掉相邻句。
预设添加完成后运行共享操作：

```json
{"id":"handoff-captions","kind":"replace_ordinary_captions_with_presets","window_ids":["quote-caption-handoff"]}
```

该操作先核实实际原生轨道文字、所属预设与整句时间，再移除窗口内的完整普通字幕段；不截断跨窗口句子、不删除素材。
支持单轨完整原句或相邻句组合，或按 `track_names` 顺序拼成完整原文且同时覆盖整个窗口的多轨文字。
如果需要让同一句的短语按顺序由不同原生预设接替，保留原完整窗口和 `replacement_for`，用下面的 `replacement_parts` 替代窗口顶层的 `template_id/node_id/track_names`；不改冻结语音句和语义裁决：

```json
{
  "id": "opening-handoff", "start_us": 1130000, "end_us": 3690000,
  "text": "如何打破信息差提升认知", "replacement_for": ["speech_02"],
  "ordinary_subtitle_replacement": true, "complete": true,
  "timing_evidence_path": "<当前已有 FunASR evidence envelope 的绝对路径>",
  "replacement_parts": [
    {"template_id": "A", "node_id": "opening-a", "track_names": ["JY_PRESET_A__NODE__opening-a_01"],
     "start_us": 1130000, "end_us": 2490000, "text": "如何打破信息差", "source_char_range": [7, 14]},
    {"template_id": "B", "node_id": "opening-b", "track_names": ["JY_PRESET_B__NODE__opening-b_01"],
     "start_us": 2490000, "end_us": 3690000, "text": "提升认知", "source_char_range": [14, 18]}
  ]
}
```

此处 `source_char_range` 是所绑定 FunASR 原始 `result[].text` 按顺序拼接、去空白与 Unicode 标点后的全局字符左闭右开索引；必须与原语音句已有同名范围连续对应，不能漏字、重字或交换顺序。
`timing_evidence_path` 必须属于当前 `content_gate.semantic_evidence.evidence_paths`，未内嵌时从计划 `semantic_gate` 文件的 `evidence_paths/rough_cut` 回读。程序复核证据哈希、粗剪来源、原生逐字 `timestamp` 数量与时间、每句和每个短语的原文绑定；缺少逐字证据不能按字数估算。
短语声明窗口必须无缝覆盖完整字幕窗，内部切点对齐下一短语首字 ASR 开始（容差 40ms），末短语允许保留原字幕的静音尾留白。每个短语可有多条同时显示的文字轨，按 `track_names` 拼接；实际原生轨的文字和起止边界须再次符合该短语，禁止前一短语越界残留。原生动画内部逐字揭示不被当作已经测得的逐字可读时间。

需要让完整句子逐项出现并有选择地保留（对照、清单标题或关系标签）时，必须显式声明
`replacement_mode: "cumulative_sentences"`，仍引用冻结 `final_retained_speech` 的相邻完整句。
本模式不读取或推算逐字时间，也不允许把冻结整句拆成未经证据支持的短语：

```json
{
  "id": "sentence-comparison", "replacement_mode": "cumulative_sentences",
  "start_us": 1000000, "end_us": 4000000,
  "text": "第一条主线这是说明第二条主线",
  "replacement_for": ["sentence-1", "sentence-2", "sentence-3"],
  "ordinary_subtitle_replacement": true, "complete": true,
  "replacement_parts": [
    {"replacement_for": ["sentence-1"], "text": "第一条主线",
     "start_us": 1000000, "end_us": 2000000, "retained_until_us": 4000000,
     "template_id": "A", "node_id": "title-a", "track_names": ["JY_PRESET_A__NODE__title-a_01"]},
    {"replacement_for": ["sentence-2"], "text": "这是说明",
     "start_us": 2000000, "end_us": 3000000,
     "template_id": "B", "node_id": "explain", "track_names": ["JY_PRESET_B__NODE__explain_01"]},
    {"replacement_for": ["sentence-3"], "text": "第二条主线",
     "start_us": 3000000, "end_us": 4000000,
     "template_id": "A", "node_id": "title-b", "track_names": ["JY_PRESET_A__NODE__title-b_01"]}
  ]
}
```

每句恰好一个 part；`replacement_for` 单项按原列表顺序与窗口引用对应，`text` 为该完整原句。
窗口与首 part 从首句既有起点开始，每 part 的 `start_us` 精确等于冻结句起点，
`end_us` 是字幕责任终点：下一句起点（末句为组窗口终点），并完整覆盖本句既有讲话跨度。
可选 `retained_until_us` 只延长实际显示，取值须在本 part 的 `end_us` 与组 `end_us` 之间；
未声明时实际显示止于本 part 的 `end_us`，绝不默认堆叠或越组保留。
示例首标题保留到组末，说明在第三句出现时退出，最终两条完整标题同时保留。
窗口禁止单预设顶层绑定或逐字证据字段；part 禁止逐字范围字段。
实际轨道仍必须唯一绑定所选预设、拼接完整原文，并从声明起点持续到显示终点（原生时间容差 40ms）；
提前出现、提前消失、越界残留、多段重叠、遗漏与普通字幕重复都拒绝。
语义预检、Candidate 最终验证与 Writer 使用同一整句合同；它只证明结构显示区间，
动画遮挡或最终集合是否真正可读仍须原生画面验收。
省略 `replacement_mode` 的旧 `replacement_parts` 保持严格逐短语交接模式，
不会因新增此能力而允许前短语溢出；未知模式拒绝。

窗口不可重叠。`ordinary_subtitles` 可保留冻结粗剪的原始完整清单用于前置语义检查；最终构建和Writer都重读实际普通字幕及预设。完整预设替代时不得残留普通字幕；下述分工模式只允许准确的剩余字幕。缺失预设、文案改动或时间覆盖不足仍拒绝。
普通字幕轨默认精确使用 `JY_ZH_SUBTITLES`，自定义名字须在 `content_gate.ordinary_caption_track_name` 或计划同名字段显式声明。
这只证明文字与时间的结构责任交接，不证明入场动画期间的实际可读性，原生画面验收仍需完成。

### 预设与普通字幕分工

2026-09-09用户确认同期同词只能显示一次，普通字幕主动让位。复用`preset_replacement_windows`和共享`replace_ordinary_captions_with_presets`，以`replacement_mode: distributed_sentence`支持预设重点与剩余普通字幕共同承载一句话，不另建字幕管线。

窗口仍以原始完整`text`、`replacement_for`及起止时间绑定一条已裁决语音句；原声与语义证据不变。`text_parts`按原顺序无遗漏、不重复地分配原句去空白后的字符偏移`source_range: [start, end]`，`text`须与该范围原文相同：

- `role: preset`：绑定所选预设的`template_id/node_id/track_name`。一条实际文字轨可承接多个原文片段（例如“开店／自媒体”）；其实际文字须与分配内容相符并覆盖整句窗口，不能声明后缺失、隐藏或提前消失。
- `role: ordinary`：所分配内容按原顺序组成普通字幕的剩余文字；为空时整个窗口关闭普通字幕。装配复制原普通字幕的单一样式与片段，生成新的文字材料，保留原材料供恢复及基准引用，不改预设或音轨。之后可用`reflow_ordinary_captions`测宽、断行。
- `role: omit_filler`：须有`reason`。只允许明确语气/引导词`嗯/呃/啊/比如/比如说/就比如说`、纯句读标点，或两项预设之间的并列连接词`和/与/以及`。否定、条件、数字和限定不能通过这一角色删除；不确定就安排到普通字幕或预设。

例如原句`就比如说开店和自媒体哪一条路更适合你`可以分为：

```json
[
  {"source_range":[0,4],"text":"就比如说","role":"omit_filler","reason":"省略引导词"},
  {"source_range":[4,6],"text":"开店","role":"preset","template_id":"所选预设ID","node_id":"choices","track_name":"JY_PRESET_所选预设ID__NODE__choices_01"},
  {"source_range":[6,7],"text":"和","role":"omit_filler","reason":"两个选项并列显示"},
  {"source_range":[7,10],"text":"自媒体","role":"preset","template_id":"所选预设ID","node_id":"choices","track_name":"JY_PRESET_所选预设ID__NODE__choices_02"},
  {"source_range":[10,18],"text":"哪一条路更适合你","role":"ordinary"}
]
```

分工模式默认共用整句窗口；需要同句预设标签先后出现时，仅对应`preset`项可同时声明`reveal_start_us`与`source_char_range: [start, end]`，父窗口同时声明`timing_evidence_path`。这里`source_char_range`是该短语在已有FunASR去标点、去空白原文中的全局范围，区别于分配整句语义的局部`source_range`。证据须属于当前`content_gate.semantic_evidence`（或当前`semantic_gate`）绑定的真实文件及粗剪哈希；短语原文必须严格匹配，首字实际时间须在父窗口内，声明起点不得晚于首字实际时间（40ms帧取整公差），允许提前出现。实际轨须从声明起点持续到父句末（40ms公差且首字时点公差不累加）；同一轨多个片段职责须声明同一起点。普通剩余字幕仍覆盖整句，完整语义、同期唯一文字及父句已裁决边界继续由原检查验证，不用原ASR的重复词重写父句。缺少任一证据字段拒绝；没有`reveal_start_us`仍保持旧的整句覆盖要求。此能力不推算字时点或改动实际预设时间，装配需用已有时间操作实现声明；逐短语交接继续使用已有ASR时间绑定模式。多个窗口先全部验证再修改；不得切掉跨窗口原字幕。Candidate及Writer复用实际分工回读，旧的完整预设、顺序短语及累计整句模式保持各自原合同。

同一短语在原生预设内由整词切换为双轨排版时，`preset`项可用`display_phases`代替`track_name`。每阶段按顺序拼接的文字必须完整等于该项原文；阶段无缝、无重叠地覆盖父窗口，不表示新的口述字时点。每项绑定同一个`template_id/node_id`，阶段内按实际轨及其`segments`数组的零基`segment_index`指向唯一可见片段，文字及起止时间须精确符合实际回读；同一片段不能重复分配。不能混用该项的单轨或`reveal_start_us/source_char_range`绑定。其他`ordinary`项仍承载父窗口完整时长，缺字、缺阶段或剩余字幕缺失均拒绝。例如父窗口为`20000..2680000`、分配原文为“这三款”时：

```json
{"role":"preset","text":"这三款","source_range":[0,3],"template_id":"57","node_id":"hook","display_phases":[
  {"start_us":20000,"end_us":1220000,"tracks":[{"track_name":"JY_PRESET_57__NODE__hook_01","segment_index":0,"text":"这三款"}]},
  {"start_us":1220000,"end_us":2680000,"tracks":[{"track_name":"JY_PRESET_57__NODE__hook_01","segment_index":1,"text":"这"},{"track_name":"JY_PRESET_57__NODE__hook_02","segment_index":0,"text":"三款"}]}
]}
```

轨名、阶段边界和源句偏移须替换为当前装配实际值；此声明不会修改预设的动画、文字槽或声音。

停留时间按文字职责判断：完整句字幕交接须覆盖所引语音及声明字幕窗；独立强调须保留能读懂重点的稳定阅读期；列表或对照按逐项出现、保留和退出关系检查，不能把每项都套成同一种短标题。历史案例的`1.5s`稳定阅读期和相邻预设`0.2s`空隙不是全类别通用Writer门槛；完整句交接不机械插空。现有预设注册表的时长合同、显式终态延长规则和最终轨道区间仍须满足；真实资源、完整语义、容量/碰撞及生产三阶段证据要求不变，具体观感由当前草稿验收。

`actual_text_tracks` 中同轨不同文案使用 `texts` 非空字符串数组，按真实轨内片段顺序声明且长度等于 `segment_count`；不得同时声明 `text`。可编辑轨按声明轨序、再按 `texts` 顺序展开后须与导入 `slots` 一致，`max_chars` 逐段检查；回读逐段比较文字与顺序。原 `text` 仍用于单段或同字多段，身份、时间和资源门禁不变。

预设内部文字起点与当前语音不一致时，可在导入后使用共享 `retime_preset_text_tracks`，
声明 `template_id`、`node_id`、当前 `authorization_source`、`design_reason`，以及
`tracks: [{track_name, start_us, end_us}]`。目标必须与当前 `actual_text_tracks` 一致，
每轨仅一个文字片段，且仍在节点内。操作只改显示区间，保留原生入场动画的相对时间、速度和资源；
不支持循环、退场或已有关键帧，不能截短入场。此时导入操作不启用按原起点延长的 `hold_policy`；
由本操作设置最终停留。原配音频按原始关联同步移动，源片段、速度和音量保持不变：原时点明确重合或整组统一移动时自动沿用；提前音、尾音、同一时点多字拆开等关联不明确时，在操作中补`audio_bindings: [{audio_track, segment_index, text_track, text_segment_index, anchor}]`，不猜新的搭配。`text_segment_index`默认0，`anchor`默认`start`，还支持`end`及实际动画序号的`animation_0_start/animation_0_end`等锚点，保留原来的相对偏移。所有引用须真实存在，移后声音仍须完整装进节点；失败不改原输入。原模板不改写，最终字幕交接与原生验收仍按当前合同执行。

最小视觉规则示例：

```json
{
  "subject_clarity_preflight": {
    "status": "passed",
    "checks": {
      "primary_subject_defined": true,
      "unused_headroom_checked": true,
      "background_distraction_checked": true,
      "perspective_checked": true,
      "caption_subject_clearance_checked": true
    }
  },
  "text_style_budget": {
    "max_display_families": 5,
    "max_palette_roles": 8,
    "full_screen_text": false
  },
  "transition_requires_real_before_after_state": true
}
```

需要转场时，对应 operation 还必须包含：

```json
{
  "visual_role": "transition",
  "before_state_id": "shot-01",
  "after_state_id": "shot-02",
  "boundary_kind": "shot_change"
}
```

`boundary_kind` 只允许 `shot_change` 或 `composition_change`。没有真实前后变化时，不得把 operation 标成转场。

## 生产文字预检（仅进入生产Writer前）

前置 `declared_text_limit` 只比较文案与声明字数，不是字形容量测量；原占位文本的字符数也不自动等于最终可读容量。显式槽位上限仍须满足；当前授权下设计的 `apply_text_style_variant` 变体还须按最终字体、字号层级、位置与缩放重新测量，不能只提高声明字数放行。预览预检的 `ok` 只表示本阶段检查无错误，`current_video_ready` 仍为 false，不能充当生产资格。

历史冻结注册表中的`capacity_policy`文案保留作来源记录；当前执行范围以上述检查和共享实现为准，不为同步说明去改写已冻结的计划、注册表或manifest。后续新注册表由现行生成器输出适配口径。

`visual_planning_version=1/2` 的候选在全部操作后、Writer前及回读时，从最终草稿执行 `final_layout`：以绑定字体、最终字号/位置和线性关键帧测量文字出画及文字、媒体、声明主体保护区之间的碰撞；媒体之间亦检查，保护区之间不互检，隐藏片段不计。缺少可测字体不得用声明字数冒充通过。支持1080×1920和1920×1080、固定旋转文字及无旋转B-roll的线性位置/正缩放。连续计算同时可见区间，能识别首尾分离而中途相交，不以整段扫掠总框代替。

新v2计划生产写入须提供`layout_checks.platform_ui`；显式授权的独立 TEST 预览缺少实测截图时，记录 `pending_platform_ui_reference` 并交用户验收，不阻断 TEST 写入。已有界面数据无效或检测到确切碰撞仍拒绝。字段包括：`platform`、实际界面测量说明`basis`、本地截图`image_path`、截图中完整视频区域`video_rect_px: [left, top, right, bottom]`，以及`regions: [{id, bbox_px}]`。区域坐标来自同一截图，按真实顶部导航、右侧操作和底部信息等遮挡记录；视频区域须与目标画布比例相符。检查读取图片尺寸，将界面区域与视频相交后换算到画布，不内置统一平台像素值。文字、B-roll及声明主体区域在完整可见时段内与这些禁停区连续比较，`allowed_overlaps`不能豁免平台遮挡。

`protected_regions`保留`id/bbox/start_us/end_us/basis`，也支持以`geometry_keyframes: [{time_us, bbox}]`表达已确定的连续主体范围，首末点须覆盖完整声明时段。底框、复杂动效、蒙版后的可见区域可在`visible_bounds`按精确`track_name/segment_id`提供`bbox`或同样的几何点及`basis`；文字测量取字形与可见范围的并集，不能用小框隐藏字形。原生描边和阴影在既有字号换算基础上使用保守外缘估计。原生底框/动效/辅助画面或人物范围仍未测到时，`platform_ui_check`列出`unchecked`并标为`partial_geometry`，不能称为禁停区全覆盖通过；已确定的侵入仍报错。真实原生声画依旧由当前草稿验收。

小窗挡住嘴部等检查依赖实际声明的主体范围，不自动识别人脸。已确认的设计重叠继续用现有`allowed_overlaps`精确列出对象对及理由，不能按整个媒体类别豁免；同一主体的可见边界与其保护区确属同物时也按实际对象对说明。

可继续配置`safe_area_px`及精确轨道对的`allowed_overlaps: [{tracks, reason}]`处理内容本身的有意重叠；它们不替代平台区域和完整外缘检查。

`layout_checks.text_unit_to_px` 可显式指定有限正数，供普通字幕重排与最终几何测量统一换算字号与字距；省略时兼容原系数 `5.0`。静态及连续预览使用同值的 `--text-unit-to-px`，各报告均记录该值。当前草稿可用 `text_unit_basis` 记录截图估计依据；估计值不是通用原生像素公式，也不替代剪映实际画面验收。

`final_layout.text_quality`在v1/v2全部操作完成后、预览及Writer复核中始终执行，与是否调用`fit_template_text_bounds`无关：

- `layout_checks.minimum_readable_text_height_px`沿用56px作为初始工程下限，`minimum_emphasis_to_caption_ratio`按本次确认设为至少1.3；可提高，不能用0、缺省、角色改名或低于下限的值关闭检查。它们是本项目当前工程基线，不是通用原生像素公式或视觉验收结论。
- 用绑定的实际字体逐样式测量信息字形，取最小信息字级，排除多行总高、描边、阴影和旋转外框。尺度取最长稳定保持区间，持续缩放时保守取最小值，不能借入场过冲充大结果；原生动效的实际显示仍如实列为待验收。
- 所有非普通字幕的信息文字均与本片普通字幕的实际最大字级比较，辅助信息不能通过改成`supporting/decorative`绕过。纯装饰引号等不按信息字比较，但仍检查边界。若整片普通字幕都已让位，须在`ordinary_caption_baseline: {material_id, scale_y}`绑定草稿内保留的原普通字幕材料及其缩放；基准也须可读，不能因关闭字幕而省略级差检查。
- 所有同期可见文字按实际时间检查相同/包含的词句，换行、大小写或句读不能隐藏重复；几何位置分开、`allowed_overlaps`理由也不能豁免。不同时间再次出现允许。同一原生预设中实际处于同一阅读位置、字体/字号/时序/运动一致的描边或阴影副本按一个字形处理，不作为两处重复字幕。素材烧录字需在入选时实际查看去重，当前字体轨扫描不宣称已自动OCR全部媒体。

字幕或文字预设进入生产单 Writer 前必须通过以下四项检查；任何一项为未知或不通过时，停止生产写入，
不得把“能导入、能渲染、能改字”当成当前画布视觉可用：

1. 原素材字幕：抽查实际使用范围的开头、中间、结尾与镜头变化点，在 `project_state.json` 明确记录
   `source_caption_mode` 为 `clean` 或 `burned_in`。原片存在烧录字幕时，默认不得再生成普通字幕；
   若当前任务就是验证字幕模板，应换用无烧录字幕的原素材。
2. 画布比例：读取真实预设内层草稿的 `canvas_config`。预设比例必须与目标草稿一致；比例不一致时，
   只能使用已经单独完成目标画布适配并由用户验收通过的变体，禁止直接复制坐标、字号和缩放。
3. 文字槽位：模板注册表必须记录原占位文本、允许的最大字数或已测量边界。替换文本超过容量时，
   不得直接写入；先换短文案或使用已验收的自动缩放/换行变体。
   显式 `slot_copy_table` 缺少必填槽位时必须拒绝，不补入工具自带示例文案。实际固定引号等装饰
   须先按原生材料核实并在注册表锁定，仍列入全部文字轨、边界和碰撞检查。
4. 字幕碰撞：普通字幕安全区视为保留区。预设文字与普通字幕在时间上重叠且空间进入同一安全区时，
   必须调整时点或改用其他预设；Writer 前不得存在未解决碰撞。

## 当前视频预设证据

共享 `animate_aroll_transform` 支持单段粗剪人物景别变化。操作显式指定
`track_name: JY_ROUGH_CUT_VIDEO`、`position_space: normalized`、`interpolation: linear`，
`keyframes` 每项完整提供 `time_offset_us`、`scale_x`、`scale_y`、`position_x`、`position_y`。
位置沿用剪映 clip.transform 归一化坐标（中心为 0，右/上为正），缩放为绝对倍率；均须有限，缩放大于 0。
时点为片段内相对微秒，首项为 0，后续严格递增且不超过段长；相同数值的相邻帧表示保持，
不同数值间只做线性插值，不隐式增加缓动。未提供`segment_id`时只接受唯一视频轨、一个原速正播片段、
源起点为0且源/目标时长一致；显式提供`segment_id`时可选择分段后的单个原速片段，关键帧时间相对该片段。
已有视觉关键帧、原生动画或变速冲突拒绝。
原生关键帧直接由 vendor pyJianYingDraft 的 KeyframeList 生成，分别控制 X/Y 缩放时关闭 uniform_scale。
该操作只改人物变换，不改素材、剪辑时点、速度、声音、帧率或字幕；人物出画、字幕避让与运动观感仍须当前原生/用户验收。

共享 `animate_broll_transform` 对 `brolls` 中显式声明的唯一 `node_id`、`track_name: JY_BROLL_*` 选择一个原速正播视频段，源/目标时长及声明时段、段数必须一致。它使用相同的 `position_space: normalized`、`interpolation: linear` 和完整四轴 `keyframes` 字段，时点相对所选段且首项为0；仅修改 clip 变换、原生四轴关键帧和 uniform_scale，不改素材、时点或声音。已有视觉运动、旋转、原生动画、非默认 uniform_scale 和变速冲突拒绝。所有检查先于写入。此操作只移动所选 B-roll；文字同步须在计划中为其完整预设组声明对应时点和变换，并完成最终字形与连续碰撞检查，不能凭底条移动推断文字已跟随。文字与其容器的有意重叠须列出精确轨道对及依据；原生观感仍待当前视频验收。

局部构图先使用`split_video_track`：显式提供`track_name: JY_ROUGH_CUT_VIDEO`、原`segment_id`、
严格递增的绝对时间轴内部`cut_points_us`及按序输出的唯一`segment_ids`（数量比切点多一）。
在唯一粗剪轨内按`segment_id`精确选择一个干净原速、源目标等长的段，允许同轨其他段及非零源起点；
仅在原位置替换所选段，其他段不变，保留该段完整声音和连续源/目标范围；
已有关键帧、动画、蒙版、复杂变速拒绝。原首尾恒定淡入淡出仅分配到首末片段，中间不新增淡变，
切点穿过原淡变区域时拒绝。顺序为分段→局部变换→局部蒙版，每步绑定明确片段ID。

共享`apply_video_mask`只处理指定`track_name`内精确`segment_id`，作用于该已有片段的完整时段，
不会隐式切段。调用前必须在计划中准备好所需范围的目标片段；若目标是整条A-roll，蒙版也会覆盖整条，不能误当局部事件。
参数显式声明`shape: circle|rectangle`、源像素中心偏移`center_x_px/center_y_px`、
`size_ratio`、`rotation_deg`、`feather_percent`、`invert`；矩形另需`width_ratio/round_corner_percent`。
`native_reference: {path, sha256, segment_id}`绑定冻结且已解码的真实双8.8参考，要求恰好引用一个
对应形状的`common_mask`、资源路径可用及参考开关无冲突。vendor仅计算几何，保留参考资源身份与8.8字段结构。
没有对应字段/形状的合格参考，直接报告缺口。已有蒙版或未经验证的Alpha/蒙版关键帧不得覆盖。
此适配只证明数据接通；蒙版形状是否合适、圈内人物裁切与画面主次仍须当前视频验收。

`add_broll`可用顶层`source_crop: [left, top, right, bottom]`声明原图左上角为原点的归一化矩形；各值须在0..1内且矩形非空，同时在`brolls`声明相同值以供最终回读核对。它写入原生素材`crop`，不改媒体文件；同源不同裁切自动使用不同素材ID，显式复用冲突ID会拒绝。离线布局及静态/连续预览先裁切，再按裁切后宽高等比适配画布，最后乘`clip.scale.x/y`；方形裁切在1080宽竖屏中使用相等的0.4缩放得到432×432。该离线计算不代替原生视觉验收。

`add_broll`需要衔接上述蒙版等按ID操作时，可用顶层`segment_id`声明非空且唯一的片段编号；
省略时仍由SDK生成。不要在`segment.id`里覆盖受管理编号，Writer回读会核对显式声明的ID。

共享`add_video_effect`复用vendored `VideoEffect`/`EffectSegment`。`scope: global`用显式
`start_us/end_us`建立独立`effect`轨；`scope: segment`用`track_name/segment_id`将特效挂在
已有视频片段的完整区间，短区间先用`split_video_track`准备，不能隐式扩展到整片。
`effect_name/effect_id/resource_id`须与SDK条目相符；`parameters`按原参数名填写0–100的有限百分数，
不接受未知参数。`authorization_source`与`options.video_effects`及当前状态授权一致，
`visual_event_id`绑定`techniques.kind: video_effect`及本操作ID。

`resource_manifest_path/resource_manifest_sha256`绑定本地资源清单：schema为
`jianying-adapter.video-effect-resource.v1`，包含相同的三个效果身份字段、绝对`resource_path`、
`files: {相对POSIX路径: SHA256}`。资源包在项目资产目录冻结，清单位于包目录外；
缓存缺失时不凭同名或SDK元数据宣称可用。引擎版本字段只作原值记录，不能直接当作应用版本或播放通过。
效果材料、挂载对象、范围、参数与资源会在候选及Writer回读中核对；独立TEST仍保留原生视觉待验收。

`render_candidate_text_review.py`无法渲染原生视频特效，有特效的候选默认拒绝。
仅排版诊断可显式传`--ignore-video-effects-for-layout`；输出标明`EFFECTS OMITTED`，
报告`native_video_effects_omitted: true`。不得把这类静帧当作特效缺失或生效的证据。

普通字幕可通过共享操作`reflow_ordinary_captions`显式设置可选`font_size`（剪映字号，必须为非布尔的有限正数），再按绑定的本地字体、最终字号、片段横向缩放及横向关键帧最大倍率和`safe_width_ratio`测宽；省略字号则保留原样。可选`preferred_breaks`以无换行逻辑原句映射到字符偏移列表，只有实际超宽时才选择语义断点，所有指定断点都放不下时拒绝。该参数不修改预设文字，也不设全项目默认字号。测量记录中的`conservative_font_fallback`是带余量的替代字体测量，不能当作原生字形或用户视觉验收。

每个入选预设必须记录：

- 当前画布与资源状态；
- `actual_text_tracks`：实际全部剪映文字轨名、当前文字、逐轨容量和视觉职责（`main_emphasis`、`supporting`、`fixed_symbol` 或 `decorative`）；不得用虚构的 `track_01` 漏掉问号、引号等固定轨。旧计划中的 `editable` 和 `fixed_decorative` 只作兼容读取，分别映射到 `main_emphasis` 和 `decorative`；
- 入场、稳定阅读、退场三阶段快照与画布边界结果；
- 三阶段必须分别标记 `evidence_mode: current_video_frontend` 或 `current_video_render`、当前节点内严格递增的 `timeline_time_us`，并显式写入 `current_video_ready: true`；以解码后的归一化像素检查，离线结构图、损坏图片、相同像素重新编码或内容相同的三份复制图一律拒绝；
- 普通字幕关系；
- 人物关系策略：`avoid_key_features`、`intentional_overlap` 或 `not_applicable`。
- 同一预设内同时可见的文字轨必须做两两字形包围盒碰撞检查。检测失败时只拒绝本次使用，不自动挪动单条文字破坏模板内部比例。
- 节点大小允许调整，但只能对整个预设组做统一缩放和统一平移；字号比例、轨道相对位置和运动路径按同一系数变化。计划可用 `template_scale_overrides` 为某个预设声明整体倍率，超过画布时再整体收缩。可读性检查使用最终字形屏幕高度 `minimum_readable_text_height_px`，禁止用剪映裸字号误判；最终字形过小或整体缩放后仍有内部碰撞时必须拒绝。
- 特殊文字与全片普通字幕的级差由上述最终`text_quality`统一检查；`hierarchy_roles`不再是最终检查开关，信息性辅助字同样须大于普通字幕。重点预设仍至少声明一条`main_emphasis`用于表达主次。整体放大导致出画或内部碰撞时，调整共同版式、断行、呈现次序或更合适的预设，不能暗缩某条字轨绕过检查。当前授权下可用`apply_text_style_variant`显式设计字体、字重、颜色及句内字号层级；变体仍须重新测量全部最终文字并完成原生/用户验收，不能私改装饰轨比例或动画路径。
- 替换操作必须显式声明旧、新语义角色，并由统一 `usage_registry` 复核新预设的 `primary_category`。旧、新角色不一致时在 CandidatePlan 预检阶段直接拒绝；不得为了通过尺寸门禁把 `question_hook` 静默降级为 `parallel_list`。
- `current_video_evidence` 必须绑定当前 `project_id`、粗剪 SHA256、`template_id`、全部最终文字、计划中的最终位置与整组缩放；三阶段快照必须使用三个不同路径，且全部位于当前视频项目目录。其他视频的截图、同图重复冒充三阶段或只声明一个未与计划比对的倍率都不能放行。
- 替换预设必须真正装进节点时长。若某条字轨在节点结束之后才开始，或非末尾片段越过节点结束，适配器必须当场拒绝，不得通过负时长或隐式裁切制造“能导入”的假成功。

共享 `add_preset_group` / `replace_preset_group` 支持显式
`hold_policy: extend_final_state_only`：以 `actual_text_tracks` 中逐轨起止和段数为合同，
只延长末尾稳定状态，保留入场和内部时点，将出场动画移至新终点前。
循环、组合动画或无法证明终态可延长的结构会明确拒绝。原配音轨随组导入，终态延长涉及退场锚点时同步关联声音；关联不明确时用本组`audio_bindings`说明，不把声音留在旧的退场位置。

单层纯循环文字可在同一`add_preset_group`中声明`hold_policy: "preserve_loop_period"`，并按实际目标时段填写`actual_text_tracks`。
每条所声明文字轨须只有一个片段、一个`start=0`的`loop`动画、`speed=1`，且没有外部关键帧或源视频时域；目标至少包含一个完整周期。
该策略保留原生`loop.duration`周期、资源和速度，仅改变文字片段可见时长，支持缩短和延长。混合进退场、组合动画、多个循环或关键帧不套用此策略。
目录的`loop_effects/loop_periods_us`及装配的完整循环区间用于检查真实结构；原生视觉、边界观感和音频试听仍分别验收。

原生8.8保存稿会省略值为0的时间字段。共享导入器在复制的预设内核对`platform.app_version`及`last_modified_platform.app_version`均为`8.8.0`后，补回已知的`target_timerange/source_timerange.start`、动画`start`及片段内`common_keyframes`的`time_offset`零值，再执行原来的取样、槽位替换和校验。源预设不改写，显式null、缺失时长、其他版本和非法值不按零修复。这个兼容处理已用真实8.8保存稿复现及回读验证；它不证明动画资源已在原生画面生效，也不放宽循环/退场的时长与视觉边界。

`add_preset_group/replace_preset_group`默认沿用原配音频。源文件定位复用`audio_path_map`和`audio_cache_root`，可写在计划、入选预设或操作，后者更具体；映射目标按计划路径解析。只使用已经存在的真实音频，本入口不自动下载。音频源须经媒体探测，不能把扩展名为mp3的其他资源或非空文件当作音频；原生声明比缓存解码声音更长的静音尾段保持原时序，并在回读中记录`source_tail_padding_us`，不伪称有实际声音。音轨、源区间、音量、淡变与相对时点一起保留，节点整体换时点从干净基础按新起点重建；声音不能为了装进短节点被隐式截掉。同一节点再次导入会拒绝，更换使用显式替换操作。

原配音轨采用唯一`JY_PRESET_<template>__NODE__<node>_AUX_AUDIO_01`等名称，随组替换或移除；空间摆位和组缩放不操作声音。`validate_preset_audio`从本次选中源预设重新构建原配音频及声明改时点，核对候选和Writer回读的轨道集合、源片段、相对时序、音量与依赖；漏声、重复轨、改声和静音漂移均拒绝。没有原配声音的预设不会自动补声。实现见`preset_audio.py`及共享导入入口。

共享 `place_preset_group` 用 `template_id`、`node_id` 定位整组，必须显式提供
`final_position: {x, y}`、`position_space: px|normalized` 和正数 `final_group_scale`。
倍率相对于导入后的原生组；文字、辅助图形及位置/辅助缩放关键帧同步变换。
默认锚点是首个片段的位置，也可显式提供 `source_anchor_px`，不能将首轨位置误当作整组包围盒中心。
该操作只执行计划布局，不能代替最终边界、碰撞、可读性和原生画面验收。

同一节点的分阶段文案需要分别定位时，可改用显式`segment_groups`：每组包含
`segments: [{track_name: "完整原生轨名", segment_index: 0}]`、`source_anchor_px: {x,y}`、
`target_anchor_px: {x,y}`及正数`scale_factor`。索引从0开始，锚点只用画布像素。
该模式不混用顶层整组定位/倍率字段，必须恰好覆盖该`template_id/node_id`的全部文字和可视辅助片段一次；
遗漏、重复、越界、同名轨歧义或跨节点选择拒绝，音频不参与。
每组沿用同一组变换公式，同步变换文字、辅助图形及位置/辅助缩放关键帧，时段不变；
非1倍率要求文字素材未被其他片段共享，避免连带字号变化。全部分组成功后才更新草稿。
例如第一轨前段整句单独一组，第一轨后段前缀与第二轨同期强调词合为另一组；
两组锚点须依据各自实际文字宽度确定，操作不自动测量或声称已居中。无此字段沿用原整组行为。

共享 `animate_preset_group_transform` 在 `add_preset_group`、`place_preset_group` 之后为整个节点增加稳定期二次动作。
参数为 `template_id`、`node_id`、显式 `anchor_px: {x,y}`、`interpolation: linear`，以及
`keyframes: [{timeline_time_us, group_scale, shift_x_px, shift_y_px}, ...]`。时点是绝对时间轴微秒，
至少两项且严格递增；首项必须是倍率1、位移0，以保留原生入场。倍率相对调用前整组，必须为有限正数；
位移为有限像素数（右、下为正）。围绕同一个锚点同比改变所有文字/辅助轨的缩放和位置，不改字号、
内部比例、素材、时点及原生入退场资源。每条片段从自身起点保持原位置，直到显式动作开始。
整段动作必须位于所有实际片段的入场结束之后、退场开始之前；循环/组合动画、已有视觉关键帧、
未知运动引用或非默认uniform_scale冲突时拒绝，检查失败不产生局部修改。末帧状态保持到片段结束，
原生退场仍按原时点执行。`visual_events.techniques.kind=keyframes` 绑定此操作ID。
这不是逐字或单槽位动画入口；不得借此破坏模板内部关系。布局测量不能代替运动中逐阶段边界、
人物关系与可读性验收；测试预览仍保持禁生产写入，实际视觉证据取得后才能封版。

统一格式最小标记：

```json
{
  "project_format": {
    "name": "获客口播自动剪辑管线统一工程格式",
    "separates_creative_plan_from_writer": true,
    "style_normalization": false,
    "writer_adapter": "jianying-8.8",
    "candidate_builder": "CandidateAssembler",
    "per_video_build_script": false,
    "operation_registry": "jianying_adapter.shared-operations.v1"
  }
}
```

`style_normalization` 固定为 `false`：适配层可以拒绝不安全的字幕/预设使用，但不得把不同视频的视觉选择自动归一成同一套。

CandidateAssembler 会在输出候选后中心化比较计划与草稿的预设身份、节点起止、整组覆盖时长、每条文字轨的起止与段数、实际文字、未声明预设轨、B-roll轨道、段数、冻结素材和嵌套布局参数。全部相等才生成 `status=candidate_ready_for_single_writer`、`writer_allowed=true`、`blockers=[]`。单 Writer 写前重新执行同一检查，写后再对真实8.8草稿执行一次；不能只比较轨道数量。若写后任一检查失败，Writer 会恢复写前 `root_meta_info.json`，把本次新建的失败草稿移入备份目录下的隔离区，不永久删除，也不把坏登记留在首页。

保存退出、兼容参考、宿主路径和profile要求见[剪映环境与写入规则](JIAN_YING_ENVIRONMENT_AND_PROFILES.md)。

首次配置或排查错误版本时可运行：

```powershell
jianying-adapter jianying-8-8-launch-contract --install-dir <8.8安装目录>
```

确认后的启动入口可以复用，不要求每次重跑合同或三套诊断。Agent获准控制界面时，从实际工具
返回结果确认是正确的剪映8.8窗口；遇到版本不明确或操作失败，再使用 `--window-app` 等诊断。

人物遮挡不是全局硬拒绝。`intentional_overlap` 必须同时记录 `design_basis` 和 `authorized_by`；例如人物已被用户要求模糊为背景，或遮挡来自已确认参考。没有依据的遮挡仍拒绝。

## 命令

```powershell
jianying-adapter validate-project-state <project_state.json>
jianying-adapter validate-candidate-plan <candidate_plan.json>
```

新视频的阶段入口（先设置 `PYTHONPATH=jianying-adapter/src`）：

```powershell
D:\python\python.exe -B -m jianying_adapter candidate-preview <candidate_plan.json>
D:\python\python.exe -B -m jianying_adapter candidate-seal <candidate_plan.json>
```

第二条命令仅在实际取得并核验真实原生画面证据后执行。整片包装与后续版本统一使用这两个阶段入口。

第五步的共享查询与交接命令（不启动Writer）：

```powershell
D:\python\python.exe -B -m jianying_adapter preset-select <candidate_plan.json> --event <visual_event_id>
D:\python\python.exe -B -m jianying_adapter material-requests <candidate_plan.json>
D:\python\python.exe -B -m jianying_adapter material-use <request.json> <usage.json> --plan <candidate_plan.json> --output <同目录的新计划.json>
```

交接只连接明确需求和资源，不代写创作理由、改权限或升级验收。完整执行顺序见首版工作法及[共享素材系统](MATERIAL_LIBRARY.md)。
`CandidateAssembler.run()` 已停用；共享文字测量、整组位移、布局检查与静态预览支持 `1080×1920` 和 `1920×1080`，按实际画布换算；其他尺寸及计划与草稿画布不一致时明确拒绝。原生预设仍须符合上面的画布比例合同，不能因共享操作支持横屏就直接套用竖屏预设。
内容裁决支持整路 ASR 选用，也支持绑定两路原始证据哈希和原文的 `structured_edits_v1` 混合修正。
每处修正必须提供准确原文跨度、替换文本和可核验的另一 ASR、源文档或当前已确认术语依据；
程序逐项核对并重建 `final_text`，Candidate 的保留语音文字必须与该结果一致。
自由文本说明或仅有用户语音效果验收不能代替逐处文字裁决依据。
Writer 成功必须更新当前状态中的登记、真实草稿位置和备份；没有用户主观验收不能标记 completed。
`completed` 的 `completion_evidence.structure_validation.report_path` 必须指向实际
`ok=true` 的 JSON 报告，报告中的 `project_id` 和 `draft_id` 必须绑定当前状态与 Writer
草稿。`user_visual_qa` 必须记录用户真实确认来源、`accepted=true` 及相同的项目和草稿 ID。
这些字段保存验收记录，不能代替用户实际验收；不存在的报告路径或其他草稿的记录均拒绝。

开发或离线测试可以用 `--skip-snapshot-file-check` 只检查证据结构；生产写入前不得跳过快照文件检查。

## 独立原生测试预览（2026-09-05 用户已授权）

`write_candidate_plan.py --preview-test --profile-kind test` 复用唯一Writer，只读取当前CandidatePlan的preview产物。计划须提供 `test_preview.enabled=true`、独立 `draft_name` 和与 `project_state.test_preview_authorization.source` 完全相同的 `authorization_source`。

测试写入重新核验预览合同、哈希、语义、结构、实际字幕和节点时点；拒绝production profile、缺授权、被改写的预览、同名覆盖及写入冲突。兼容参考、C逻辑登记、恢复副本和失败回滚与生产相同。用户2026-09-09确认由用户打开测试草稿检查实际画面与声音，Agent原生播放、逐帧截图、完整试听及`internal_review.passed`均不是这次写入的前置条件；未检查项如实记录，不能因此标记视觉通过。

独立测试预览只接受`phase=preview`、`writer_allowed=false`及唯一的待封版blocker；其他未解决blocker不得借测试路径绕过。生产Writer必须重新读取`writer_allowed=true`与空blockers，并拒绝已写入状态、离线证据或计划/草稿不等价。

写后保持project_state=planning和原preview manifest不变，仅记录独立test_preview_registration。生产Writer继续只接受完成封版及真实画面证据的候选。用户自行打开测试草稿完成验收；本次扩建授权不等于代用户点击界面。
# 显式文字外观变体

共享操作 `apply_text_style_variant` 按精确文字轨名修改本地已绑定字体、实色填充、斜体、已有阴影强度/距离及去描边。需要计划 `text_style_authorization_source` 与当前项目 `text_style_revision` 授权相符，并写明 `design_reason`。它是当前视频的主动设计，不是 `style_normalization`；该字段仍必须为 false。

`tracks[].segment_id` 可选，用于同一字幕轨内只修改精确的一句；省略时仍按整轨处理。目标段必须唯一，不能在同次操作混用同轨整轨目标和单段目标；若该段与同轨其他段共享文字素材则拒绝，避免连带改字。句内 `spans` 按该句字符范围校验。

字体使用真实本地文件、资源 ID 和 SHA-256，检查所需字形；不能把下载字体称为新增原生动画预设。整轨样式默认不修改字号。可选 `tracks[].spans` 对原句内部建立字阶：每项声明精确字符 `range:[start,end]`（结束不含）、匹配的 `text`，以及可选 `size_multiplier`（0.5—2）、`font`、`color`、`bold`。范围包含换行、不得重叠；当前只支持BMP字符，拒绝不完整的原有样式范围。切分原生样式run，保留完整原句、位置、时间、动画、关键帧及非字体素材引用。修改后重新测量容量与碰撞，从干净基础重建；多字体边界按最大字号逐字体保守测量，不能替代原生排版。测试写入不代表原生视觉通过。不同效果的最终阅读感仍由当前草稿实际验收。


## 现有文字片段的原生入场

`apply_text_animation`只给已存在的一个文字片段添加免费 `TextIntro` 入场。目标由精确
`track_name`和`segment_id`定位；`enum/member/effect_id/resource_id`必须一致地指向本地SDK枚举。
`duration_us`是正整数，不能超过当前片段。需要`options.text_animation.enabled=true`，其中
`authorization_source`、操作同名字段与状态`current_authorizations`中`option:text_animation`同源。
对应`visual_events.techniques`声明`kind:text_animation`并引用该操作ID；操作的`visual_event_id`必须反向匹配。

```json
{
  "id": "capacity_intro",
  "kind": "apply_text_animation",
  "track_name": "JY_ZH_SUBTITLES",
  "segment_id": "current-caption-segment-id",
  "enum": "TextIntro",
  "member": "轻微放大",
  "effect_id": "1644262",
  "resource_id": "6763469998330483213",
  "duration_us": 300000,
  "resource_manifest_path": "E:/project/assets/text_animations/1644262_manifest.json",
  "resource_manifest_sha256": "<64位SHA256>",
  "authorization_source": "<当前用户授权原文>",
  "visual_event_id": "capacity_compare"
}
```

资源manifest的`schema`为`jianying-adapter.text-animation-resource.v1`，必填
`enum/member/effect_id/resource_id/resource_path/files`；前四项与操作相同，`resource_path`是已冻结的绝对目录，
`files`为该目录全部文件的相对POSIX路径到SHA256的映射。manifest放在资源目录外；可选`provenance`仅记录来源，
不能代替SDK身份或原生验收。编译与Writer回读都验证目录清单、实际文件与哈希，不下载资源。

实现通过现有`animation_writer.apply_animation_reference`生成SDK动画容器，写入
`materials.material_animations`并更新目标段引用。已有非空原生动画会拒绝，不隐式替换；空动画容器可解除该段引用，
原容器仍保留供其他段使用。原句、字号样式、布局、关键帧与时间均不改动。回读检查实际容器、唯一挂载、资源、
入场类型和时长、目标可见状态，不能只凭操作声明通过。

预览和明确授权的独立TEST可先保持原生视觉待验。封版/生产仍需操作的`current_video_evidence`：
`current_video_ready/native_visual_verified=true`、匹配的`project_id/operation_id`、
`build_contract_sha256`、实际`preview_output`的`candidate_sha256`，以及现有合同的
`render_phases.entry/stable/exit`真实图片、证据模式和递增时点。静态排版图不能冒充原生入场验收。

## 动作音效与离线混音

原配沿用指保留音源、源片段和声画时序，不要求照搬作者增益。主Agent先检查实际原生音量，再通过现有混音操作按当前人声调整；单片用户通过的衰减值不能直接作为其他素材的固定默认值。存在可听音效与人声时，CandidateAssembler在结构检查通过后自动调用本节已有的同期音量测量，结果写入现有装配报告的`final_validation.audio_measurement`；临时混音在当前计划目录内生成并清理，不新增常驻试听文件或报告。测量失败、无有效人声、同期音量风险或峰值余量不足都会使装配失败，不能生成可写入候选。画面局部修改时，从当前状态`artifacts.structure_report`引用的既有装配报告复用测量；依据实际可听音源的路径、文件大小及修改时间、源/目标区间、音量、速度、淡变、音量关键帧与测量阈值判断依赖是否变化，不扫描历史视频或另建缓存文件。纯画面构图、蒙版或文字样式变化不触发重测。缺少可用报告或声音依赖变化时重新测量。seal继续复用同一候选结果，Writer核对当前声音依赖及候选绑定，不重复解码；缺少通过结果的混音候选须重新装配。改变音源、音量或时序后通过重建更新结果。FFmpeg使用`REVIEW_FFMPEG`环境变量指定的可执行文件，否则使用PATH中的ffmpeg；缺少工具时报错，不自动安装。测量通过不代表台词可懂度或用户听感通过。

共享 `mix_action_audio` 在粗剪上处理对白增益及确有需要的补充动作音效。必须绑定 `audio_authorization_source` 与当前状态的 `action_sfx` 授权；每个候选只允许一个混音操作。原声轨须为原音量1.0且无音量关键帧；已导入的原配音轨可以共同存在；可用 `preset_gain_db`（默认0，范围-60至0）统一衰减原配音效，源片段、速度、时点及淡入淡出保持不变，Writer按声明增益回读核对。可选 `preset_track_gains_db: {"精确原配音频轨名": -6}` 为指定的已有非空原配音轨覆盖默认衰减（不是再叠加）；未指定轨仍用 `preset_gain_db`。每项同为有限 -60 至 0 dB；未知轨、非原配轨、重名轨、非法值及音量自动化/未解析关键帧冲突在全部增益验证完成前拒绝，不部分修改。原声片段上的作者音量乘以所选增益，重建回读复用同一变换。其他已有附加音轨或重复混音仍拒绝。存在原配声音时允许`events: []`只调对白；沿用预设原配声音本身不需要再列一份events。补充事件若在同一时点重复同一原配声音的源片段则拒绝；不同节点复用同一音效不受影响。BGM不在此操作范围。

操作声明 `dialogue_gain_db` 及 `events`。每项包含唯一 `id`、`source_path`、`sha256`、`source_start_us`、`start_us`、`duration_us`、`gain_db`、`fade_in_us`、`fade_out_us`、`visual_event_id`、`action_operation_id`、`action_time_us`、`anchor_offset_us`、`reason`。声音须绑定对应画面事件里的真实视觉操作；`start_us + anchor_offset_us == action_time_us`，锚点在该声音片段内，动作时点在画面事件内。包络测量可帮助定位锚点，不等于已经听核动作同步。

新文字入场可在event中增加`action_anchor: text_intro_start|text_intro_end`，
`action_operation_id`指向对应的`apply_text_animation`，或明确声明单文字轨、单段、单一入场动画的`add_preset_group`。预设资源仍走现有预检；复杂多文字轨或多动画不可猜测锚点。此时`action_time_us/start_us`均可省略：
混音读取**实际编译后**目标文字段`target_timerange.start`和引用容器内动画的`start/duration`，
计算入场开始或结束时刻，再减去音源的`anchor_offset_us`得到声轨起点。若仍显式填写这两个时点，
必须与实际值一致；负声轨起点、未先编译动画、错误资源或动作引用均拒绝。
Writer回读重新解析当前动画并核对实际声轨，文字改时点而声音留在原处会失败。
没有`action_anchor`的旧event保持原合同。本次只接文字入场开始/结束，不把任意手填时刻称为已验证的关键帧落点。

对白静态增益支持 `-60..+12 dB`，用于较轻的原声；音效增益仍为 `-60..0 dB`。
正增益不代表已经检查削波，应从实际候选调用 `render_candidate_audio.py` 核对混音峰值，
并在当前草稿回听。增益不改源文件、语速或音调。

音频通过vendored pyJianYingDraft的AudioMaterial/AudioSegment/AudioFade导出，核对真实资源哈希、时长、片段区间、有限增益和淡变范围。`visual_events.techniques` 可使用 `sound` 连接混音操作；不强制每条视频使用音效或固定数量。候选及Writer时间线回读共同检查音频轨集合、资源、源/目标区间、速度、音量、淡变和对白增益。相关实现见 `action_audio.py`。

`scripts/render_candidate_audio.py --plan … --draft … --ffmpeg … --output … --report …` 核对原配音轨后，依据实际候选的音频片段、静态增益、源区间和淡变生成工作区全长试听WAV，支持原速正播；兼容原配片段1–2微秒的源/目标取整差，解码短于原生声明时按原窗口补静音，不改原生草稿或源文件。已存在的音频特效、复杂变速或音量自动化明确拒绝。用浮点音频保留潜在过载，重采样测峰值，不让整数截幅掩盖问题。它不启动剪映、不写真实草稿，也不构成原生播放、音色适配或用户听觉通过。

同一混音链同时测量`JY_ROUGH_CUT_VIDEO`人声与其他可听音轨的10ms窗口RMS，报告`voice_balance`中的同期相对强弱和风险区间。默认在人声高于-45dBFS时，其他声音须比同期人声低至少6dB；可在计划`audio_checks`设`background_margin_db`（0—30）及`dialogue_floor_dbfs`（-80至0，不含0）。短促音效不豁免，不能以开场冲击或强调效果为由放行风险；更短窗口减少100ms均值对短促重叠的稀释，但仍不是听音能力。这是音量风险基准，不是固定衰减量，也不能证明频谱掩蔽或台词可懂度；缺少人声轨或有效人声区间会明确报告不可判断。临时测量音轨在输出目录下生成并清理。

峰值不足1dB余量或检测到上述同期音量风险时，音频报告`ok=false`且命令退出1，连续视频离线预览同步此结果。主代理用现有`mix_action_audio`的实际原生增益修正并重测；本次不自动调增益或增加侧链，也不使用只让预览听着正常的限幅。测量通过仍返回`intelligibility_verified=false`，实际音色及台词清晰度留给试听与用户验收。


连续离线预览`render_candidate_video_review.py`复用上述原配音频校验和混音规则，并将实际计划路径传入混音入口，支持相对路径引用预设与音频映射。原配音轨经真实媒体与源片段核对后可保留声明的静音尾段，音频源/目标时长允许最多2微秒的原生取整差；普通媒体越界、视频时长不等价仍拒绝。回读比较解析后的本地音频路径，同一文件的相对路径表示不构成换声。原生视觉效果仍仅能在显式`--layout-only`下列为省略，不以离线音视频生成成功声称原生声画验收通过。

## 克制文字变体

`simplify_preset_motion` 在当前 `visual_packaging_revision` 授权下，对 `track_names` 明确选中的真实文字轨按 `plain_text_no_entrance_or_glow` 模式去除装饰。只支持全部为in的原生入场动画及静态text_glow：入场容器复制为空容器，光晕解除选中段引用，未选轨道、共享原资源、文字/时点/几何/声音保持不变。循环、退场、未知或时变光晕拒绝。需要 `authorization_source`、`design_reason`，在visual_events的text_preset中连接此操作。它是显式的克制变体，不是全局关闭动画，也不能被写成所有财经视频均已通过的风格。当前作品仍须原生/用户验收。实现见preset_simplification.py。
