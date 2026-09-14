# 共享素材系统

用于首版工作法第五步与后续定向调整中的辅助画面。主管线仍是现有 CandidatePlan → CandidateAssembler → 唯一 Writer。用户 2026-09-09 确认：先找合适实际素材，找不到或效果不合适再生成，入库供本项目复用。支持图片、视频及其透明度检查；2026-09-10接入已选视频的帧提取与局部加工，具体格式和原生兼容边界见下文。

本入口服务手册5.4的素材与稳定构图，并在5.8交接实际资源；先有画面职责和构图判断，再选择文件及参数。规划时可以保留未解决需求，检索与无依赖的工作独立推进，装配前落实。`material-use --plan`已接回的操作和声明不重复手填；复用现有asset_id无需再次register。完整创作顺序见[完整管线](PIPELINE.md)，以下仅执行当前素材需求涉及的分支。

## Agent 执行顺序

1. 根据当前台词和 `visual_event` 写素材需求：观众需要看懂什么、真实证据还是示意、主体、画幅/尺寸、展示时长、人物与素材的空间分工、风格及文字避让。先判断对象、过程、情境、举例、比较和证据分别需要什么画面，再选择资源。需求文件立即接入当前事件的 `supporting_visual.request_paths`，状态为 `needs_asset`，不能等下载成功才补需求。运行 `material-requests PLAN`查看尚未绑定的请求；该命令不会搜索或生成。
2. 运行 `material-search`。先看本地结果的实际图片或可用视频段。没有符合当前用途的素材时，使用结果中的 `source_candidates.web_queries` 调用网页搜索/浏览工具，进入具体素材页查看。`sucai` 只提供站点和关键词，命令不会把导航记录冒充实际搜索结果；站点上的免费、商用、直连标记也不会进入授权结论。
3. 看主体、内容准确性、清晰度、可用时长、裁切余量、内嵌文字和与本片的风格关系。图片读取原图；视频查看目标源区间或足够的代表帧，如实区分抽帧与连续观看。记录已查看内容及弃用原因；不能仅凭文件名、搜索摘要或下载成功认定合适。
4. 找不到、网络不可用或候选效果不合适：在需求内记录实际搜索结果，然后运行 `material-generation-brief`。对适合生成的图片调用当前 `imagegen` 技能与内置 `image_gen`，无须另建 API 脚本。生成前先查看引用的本地参考图，生成后看实际结果；主体、构图或风格不对就针对具体问题修改再看。透明素材须保留真实 alpha。图表适合准确表达关系时可由共享工具制作，但不能用空框或通用图标缩减本来需要的精细画面。不要自动改用付费外部 API。
5. 真实节目封面、真实人物/产品、具体事件的证据仍找真实来源。图库情境和生成场景只能承担其实际能说明的内容，不能冒充当事画面。`truth_role=evidence` 的需求不能进入生成分支；缺证据保留未完成并继续独立工作。内置生图输出图片；若原设计需要运动，可明确选择图片加现有关键帧，或在获得相应授权后接视频服务，不能把静图记成已生成的视频。
6. 下载到当前项目 E 盘目录，例如 `planning/materials/incoming/`。所有文件禁止下载到 C 盘。内置生图默认文件可能位于 `D:/CodexData/generated_images/`，选定结果通过 `material-register` 复制入本项目 E 盘库。保留原始文件、具体素材页/许可记录或真实生成记录，禁止凭站点名补写许可。
7. 运行 `material-register` 冻结选定文件；登记会实际解码/探测、核对需求和查看对象，保存预览、需求及原有 `jianying_media_asset_ledger_v1` 台账。后续复用也须看当前需要的实际画面，不把上次的画面判断当本次通过。
8. 运行 `material-use REQUEST USAGE --plan PLAN --output 同目录新计划.json`，同时把 `operation`、`broll`、辅助画面及 media technique 引用连接到同一 CandidatePlan，并保留需求路径。多个请求只完成其中一个时继续 `needs_asset`；全部绑定后才转 `ready`，随后仍须实际装配检查。原文件不覆盖，同名冲突不替换，创作理由和权限不由命令补写。若事件仍是 `not_needed`，先根据实际创作判断修正计划，再交接。需要的层级、蒙版、关键帧继续使用现有操作。CandidateAssembler 装配及 Writer 回读检查同一素材；原生实际声画由用户打开 TEST 验收。

## 位置与命令

从 `E:/AutoCUT/jianying-adapter` 使用现有 Python 环境运行；如未安装包，设置 `PYTHONPATH=src`。

配置仅有一份：[material_library_config.json](../assets/material_library_config.json)。`navigation_data` 与 `keyword_guide` 指向 `E:/AutoCUT/local/sucai` 原项目，读取最新 JSON/词库，不复制 HTML 中的第二份站点列表、不改写原项目或全局技能。`library_root` 为 `E:/AutoCUT/media_library`。目录变化时只修改此配置。

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
python -B -m jianying_adapter material-search E:/当前项目/planning/materials/shop-request.json
python -B -m jianying_adapter material-requests E:/当前项目/planning/candidate_plan.json
python -B -m jianying_adapter material-generation-brief E:/当前项目/planning/materials/shop-request.json
python -B -m jianying_adapter material-register E:/当前项目/planning/materials/shop-request.json E:/当前项目/planning/materials/shop-selection.json
python -B -m jianying_adapter material-process E:/当前项目/planning/materials/rewind-request.json E:/当前项目/planning/materials/rewind-process.json --output E:/当前项目/planning/materials/rewind-result.json
python -B -m jianying_adapter material-frame E:/当前项目/planning/materials/shop-request.json E:/当前项目/planning/materials/shop-use.json --output E:/当前项目/planning/materials/shop-frame.json
python -B -m jianying_adapter material-use E:/当前项目/planning/materials/shop-request.json E:/当前项目/planning/materials/shop-use.json --output E:/当前项目/planning/materials/shop-handoff.json
python -B -m jianying_adapter material-use E:/当前项目/planning/materials/shop-request.json E:/当前项目/planning/materials/shop-use.json --plan E:/当前项目/planning/candidate_plan.json --output E:/当前项目/planning/candidate_plan_with_shop.json
```

`E:/当前项目` 是当前视频实际目录的占位符。`material-search/generation-brief/register/process/frame/use`支持 `--config` 与 `--output`，输出须为新文件；`material-requests`直接读取计划并输出待办。最后两条 `material-use` 是两种交接方式，选择其一：不带 `--plan` 输出供已有调用者使用的交接片段；带 `--plan` 保存连接好的新计划。计划输出须与原计划同目录，保持其他相对引用的含义。`material-frame`计算取景和clip；源帧提取用`material-process`的`mode=frame`，两者不要混淆。`generation-brief`只准备工具参数，Agent实际调用内置工具。命令和`next_action`均不能作为原生或用户验收证据。

## 需求与选材记录

需求文件随当前视频保存；这里只是字段示例，不是可直接冒充已检查的证据。

例如在当前事件填写 `supporting_visual: {status: "needs_asset", purpose: "展示经营店铺的情境", operation_ids: [], request_paths: ["materials/shop-request.json"], asset_search: {queries: ["店主 整理店铺"], next_action: "material-search 后实际查看候选"}}`。相对路径按 CandidatePlan 所在目录解析；完成后路径继续保留，不能把未解决请求改为 `not_needed`。每个请求绑定一个当前事件，不同事件使用各自用途需求，可以复用同一库内文件。

```json
{
  "id": "shop-scene", "node_id": "shop", "visual_event_id": "choice",
  "visual_requirement_id": "shop_context", "subject": "店主和货架",
  "observable": "看清经营者整理店铺的空间关系", "time_behavior": "still",
  "purpose": "让观众看到经营一家店铺的具体情境",
  "truth_role": "illustration", "media_type": "image",
  "query_zh": "店主 整理店铺", "query_en": "shop owner arranging store",
  "composition": "店主与货架可辨，按本片人物窗口和字幕留出空间",
  "style": "填写当前整片设计中的风格",
  "requirements": {"min_width": 1080, "min_height": 1080, "alpha_required": false},
  "search_attempts": []
}
```

`media_type=video` 时可加 `requirements.duration_us`；尺寸按实际版式需要设定，不强制把所有素材预先裁成竖屏。生成可补 `subject/constraints/text/reference_images`；精确文字写在 `text`，参考图使用存在的绝对路径。对视频无声辅助画面，交接默认 `volume=0`，音效另按当前设计编排。

当前意图计划的请求必须绑定同一事件的`visual_requirement_id`，`subject/observable/time_behavior`与该需求保持一致。例如“看清涂抹顺序”不能由产品静物图代交；`continuous_action`要求真实视频。构建时从需求和实际操作两边核对，生成了其他合格图片也不会消除当前缺口。`visual_requirements[].request_path`与现有`supporting_visual.request_paths`共同进入material-requests，后者保留对旧计划的兼容。

记录实际搜索后，`search_attempts` 每项填写 `stage: local/web`、`query`、`source`（本地库或实际网站）、`outcome: no_results/rejected/unavailable`、`reason`。`rejected` 还要有 `reviewed_candidates: [{source, scope, reason}]`，明确实际查看了什么和为何不合适。没有搜索记录不会输出生成回退；不要复制虚构的“无结果”来解锁。

selection 共用字段：

```json
{
  "asset_id": "shop-001", "local_path": "E:/当前项目/planning/materials/incoming/shop.png",
  "source_kind": "local", "provider": "user",
  "authorization_source": "填写当前真实授权来源及可用范围",
  "license_status": "user_authorized", "editorial_only": false, "attribution_required": false,
  "tags": ["店铺", "经营", "shop"],
  "visual_review": {
    "decision": "accept", "scope": "image",
    "reviewed_path": "E:/当前项目/planning/materials/incoming/shop.png",
    "content": "填写实际看到的主体和内容",
    "composition": "填写实际取景、裁切余量、文字与人物空间",
    "quality": "填写实际清晰度、风格与瑕疵检查"
  }
}
```

来源按真实情况填写，不能用 `local` 隐藏网络下载来源：

| 来源 | 额外/替换字段 |
| --- | --- |
| 已有自有/获授权文件 | `source_kind=local`、真实 `provider/authorization_source`、`license_status=user_authorized`。文件在本地不自动证明自有或有权使用。 |
| 网络素材 | `source_kind=download`；`provider=pexels/pixabay/mixkit/videvo`，其他站点用 `external`；`source_page_url/license_url/license_checked_at/license_name/license_status=verified_commercial`。`external` 须补 `license_evidence` 摘述实际单条许可；特殊范围、署名沿用现有台账检查。 |
| 内置或已授权生成工具 | `source_kind=generated`、`provider=image_gen` 或真实服务、`license_status=project_generated`、真实 `authorization_source`；`generation: {method, description, output_path, reference_images}`，description 保存实际完整提示，保留真实输出和参考路径。须有此前搜索记录。 |
| 自行制作的准确图表/背景 | `source_kind=authored`、真实 `provider/authorization_source`、`license_status=project_generated`、`authoring: {method, description, source_path}`，说明制作方法和可编辑来源；只能标为 illustration。 |
| 已入库视频的帧提取或局部加工 | 由`material-process`产生`source_kind=derived`及实际`derivation/processing_record_path`；继承源provider、许可、署名与使用范围，保留当前加工授权。不得把原示意素材升级为evidence。 |

需要署名时填写 `attribution_text`；后续发布物料使用此记录。`project_generated` 和 `user_authorized` 区分来源，不冒充对全部第三方权利的许可认证。网络内容按实际素材许可处理；导航数据库内的混合许可标签不能直接入账。

视频 review 的 `scope` 如实用 `video_segment` 或 `video_frames`，再记录 `source_timerange: {start, duration}`（微秒）；只有检查过的源区间可以使用。抽帧仅用于离线选材，不宣称完整播放。

## 局部合成与派生素材的开发边界

2026-09-10首批已实现：`material-process`从已入库视频提取所选区间的首帧/末帧，或生成恒速、倒放、抽帧保持和饱和度处理的无声视频；输出可保留已有透明度。它经`material-register/use → add_broll`接回同一CandidateAssembler。仓库另有[人像处理脚本](../scripts/render_portrait_composite.py)，已写有BiRefNet/U2Net逐帧alpha、局部修复及静态/视频背景合成；这些能力尚未接入此统一入口和派生合同，不能当作零基础重写，也不能据源码存在认定模型实跑或连续边缘通过。动态蒙版、更广的图层组合及透明视频8.8实际叠加仍待实现/验证。逐项进度见[28项特效清单](../../reference_videos/analysis/教程学习与管线迭代_20260910/特效教程对应清单.md#执行对齐)。

处理当前已选源素材不需要编造外部搜索失败记录，也不使用`generated/authored`掩盖其来源。当前输出需求继续使用上面的REQUEST，填写真实用途和`media_type`；输入先按真实来源入库，源的已查看区间必须覆盖本次处理区间。加工参数作为现有命令的第二个文件，例如：

```json
{
  "asset_id": "rewind-001", "source_asset_id": "selected-source-001",
  "authorization_source": "填写本次真实加工授权及范围",
  "parameters": {
    "mode": "video", "source_timerange": {"start": 2000000, "duration": 2000000},
    "reverse": true, "speed": 2, "frame_step": 1, "saturation": 0,
    "output_fps": "30", "output_format": "h264_mp4"
  }
}
```

| 参数 | 当前实际含义 |
| --- | --- |
| `source_timerange` | 必填微秒`start/duration`，相对源视频起点，起点含、终点不含；按实际解码显示时间选帧，兼容非零PTS与可变帧率，不按平均fps猜帧 |
| `mode=frame` | REQUEST须为image；`frame=first/last`，默认first，提取所选区间内确切帧为RGBA PNG。不接受以下视频参数；保持多久由现有`material-use`目标区间决定 |
| `mode=video` | REQUEST须为video；先裁切、按`frame_step`选帧，再倒序内容、恒速重映射、调饱和度和转换输出帧率。`reverse`默认false；`speed`为有限正数，默认1；不是变速曲线 |
| `frame_step` | 正整数，默认1；从所选第一帧起每N帧保留一帧，中间保持最近帧，整体时长仍按源区间和speed确定 |
| `saturation` | 0—2，默认1；0为去色。按实际表达选择，不能把加工后的色彩当作产品原色证据 |
| `output_fps` | video必填，0以上至120的数字或有理数，如`30`、`30000/1001`。输出帧数为`源区间时长 ÷ speed × fps`四舍五入，至少1帧；实际时长随整帧量化，不伪填任意微秒时长 |
| `output_format` | `h264_mp4`（普通视频默认）、`qtrle_mov`（有透明度时默认）或`prores4444_mov`。现有H.264输出会丢失透明度，含透明像素时拒绝该组合；MOV组合保留透明度但不代表8.8原生兼容已通过 |

输出位于E盘库内`.processing/<asset_id>/`，含真实媒体、原需求、`processing.json`和`result.json`；同名不覆盖，原源文件保留。`result.selection`是待查看的入库记录，不自填`visual_review`。Agent实际查看输出后，取该selection对象补当前review，保存到当前规划目录的selection JSON，再运行原`material-register REQUEST SELECTION`。入库会把媒体和加工记录一起冻结到`library_root/<asset_id>/`，随后用返回的asset_id执行原`material-frame/use`，无需另建剪辑入口。

`derivation`由执行器记录工具实际版本、规范化参数、滤镜、源探测、源台账/ID/文件校验、输出文件校验和逐输出帧的源索引/源时间/目标时间。登记与候选回读核对实际记录、输出和原始来源，不能仅改selection把另一文件、许可或示意角色混入。源、派生输出和最终时间轴分别计时：已加工视频通常从输出0开始以speed=1装配；缩小等后续运动仍用已有`animate_broll_transform`。当前加工统一舍弃音轨，原对白及必要音效按原声音合同单独安排。

| 工作 | 必须保留或核对的内容 |
| --- | --- |
| 输入与可重做性 | 原文件/已入库asset、真实源区间或帧时间、处理方法及参数、工具实际版本、输出路径与已有文件校验信息；源许可和真实性沿用原记录。准确图表仍按authoring记录其可编辑来源 |
| 帧与时间 | 以实际解码帧时间选取，区分源、局部输出和最终时间轴；核对恒速、倒放方向、冻结保持及抽帧后的帧序和目标时长。不得只用文件名或平均fps推定选中了所需动作帧 |
| 视频输出 | 实际容器/编码、像素格式、尺寸、帧率与时间戳、时长、色彩信息及音轨；按目标用途选择合适输出，检查首尾和必要中途内容。普通不透明视频可先独立验证倒放等能力 |
| 透明输出 | 图片保留原像素检查；视频先读实际格式/alpha声明，再解码统计全片alpha最小/最大值，区分存在通道、实际含透明像素与是否有可见内容；全透明会被需求检查拒绝。VP8/VP9的WebM透明附加流使用支持alpha的libvpx解码。旧视频台账缺少该证据时按需重探测，原台账不改写；8.8边缘与叠加仍须实际检查 |
| 前景和层级 | 人物前景与背景对应同一实际源帧和时间映射；发丝、手、转头及遮挡边缘可检查，缩放或移动后层间同步。必要时记录半透明边缘的合成方式，避免黑白边或错位 |
| 声音与回接 | 无声辅助媒体继续默认静音；明确保留、舍弃或单独绑定的音轨。倒放或冻结不得自动重复/倒放主对白；沿原声和动作音效合同处理。最终装配核对实际文件、源区间、目标区间、速度、裁切、位置及媒体绑定 |

普通时间回溯不依赖透明通道。真实像素、帧映射、无音轨和v2候选回接已有共享模块测试；测试使用明确标注的合成输入，不是教程复刻、真实口播ASR或用户草稿验收。人物前景与背景同步、发丝/手部边缘、实际原生渲染仍按具体实现验证；不把保留已有alpha称为人物抠像。加工不补造产品事实，也不把静帧替代必须连续的动作。文件保存在E盘；局部中间素材供装配使用，成片导出、付费服务及GUI等权限按当前任务处理。

视频时长优先取对应视频流；流未给时长时用该流实际包的末端显示时间和帧时长计算，不把WebM容器中的非零起始偏移或更长音轨计入画面时长。没有足够时间证据则报错，不从平均帧率猜末帧。当前阶段不包含人物分割模型、自动目标跟踪、音频时间重映射、任意变速曲线或多源合成。

## 交给现有装配器

先分开决定“框内看哪一部分”和“窗口放在哪里”。当前usage可提供`source_crop: [left, top, right, bottom]`（源归一化坐标），以及`framing: {canvas_size: [1080, 1920], target_rect: [左, 上, 右, 下], content_region: [源左, 源上, 源右, 源下]}`。target_rect单位为画布像素；content_region是依据实际源画面确定、必须保留的内容区。`material-frame REQUEST USAGE`求出原生clip、实际placed_rect和裁切后的分辨率，保持比例；不同宽高比会在目标矩形内留边，不拉伸人物或产品。它计算稳定矩形取景，不实现自动抠像、蒙版边缘动画或跟踪。

查看具体源区间及裁切后，在usage.visual_review中如实记录同一`source_crop`和求得的`clip`；已有预览用于核对构图，不能把源JSON或取景计算称为已播放。随后material-use保留裁切、布局、当前用途及查看范围，复制给operation/broll并连接对应观察需求。裁切切掉content_region、裁切后分辨率不足、使用画布不一致、请求的内容/风格/约束后来改变，或最终草稿的源区间/crop/clip不同，均会拒绝。修改用途后重新查看和交接，不改历史入库许可。

登记返回 `asset_id/ledger_path/local_path/preview_path/probe`。使用文件以该 `local_path` 为准，核对当前版式后填写 usage：

```json
{
  "asset_id": "shop-001", "operation_id": "insert_shop", "start_us": 27000000, "end_us": 30000000,
  "source_start_us": 0,
  "clip": {"alpha": 1.0, "scale": {"x": 0.5, "y": 0.5}, "transform": {"x": 0.0, "y": 0.0}},
  "visual_review": {
    "decision": "accept", "scope": "image",
    "reviewed_path": "E:/AutoCUT/media_library/shop-001/media.png",
    "content": "填写本次查看的实际内容",
    "composition": "填写适合当前事件的取景和图文关系",
    "quality": "填写本次检查结果"
  }
}
```

此处坐标仅说明字段，不是推荐构图。`material-use` 输出静音、1 倍速的原生 `add_broll` 输入；改变速度时必须明确修改 source_timerange 与 segment.speed 并继续通过现有时长检查，不隐式加速。图片的停留时长由目标区间决定。

新选辅助媒体保留输出中的 `media_asset` 引用；原始口播复制出的同源小窗不必重复登记。验证器在候选预检、实际装配、Writer 前及回读检查台账、文件内容、当前请求、查看区间和实际素材绑定，旧计划没有此字段时继续兼容。返回通过只说明这些检查成立，画面表达与实际声画由当前任务继续设计、检查及用户验收。

验证同时从`request_paths`核对每个已提出的需求是否存在合格操作，避免只检查已下载素材而漏掉仍未完成的需求。装配报告的`visual_execution.sequence_summary`按实际可见区间合并辅助媒体时长；同屏两张图不算两段变化，人物蒙版独立统计。对照整片画面职责判断是否充分，不设置B-roll时长配额。

已完成实际选材、查看及构图决策的多项需求，可用[CandidatePlan批量交接](CANDIDATE_PLAN_V1.md#批量执行已选交接)的`creative-batch`一次调用现有入库、取景和material-use，并保存到同一当前计划。失败项可续跑，独立项继续，已完成项不重登；复用asset_id无需selection。`search`批量返回候选/导航，仍须执行实际外部搜索与查看，不能把批处理成功写成选材完成。
