# Jianying Adapter

当前是本项目剪映 8.8 原生草稿适配层。生产合同以 `docs/CANDIDATE_PLAN_V1.md` 为准，
主管线固定为 Whisper/VideoCut → EDL → FFmpeg → pyJianYingDraft/本地适配层 → 剪映 8.8。
端到端执行顺序、交接物与调整范围见[完整管线](docs/PIPELINE.md)。先完成手册第五步现行保留步骤的开场及段落表现、逐段声画与实际关键构图，再装配、执行必要程序检查和已授权写入，交用户播放审核；独立5.6及装配后Agent截图、切片审核已取消。当前v2的意图、需求、选择与取景绑定服务于对应创作步骤；规划时可以保留待办，构建前须落实。旧计划可只读复核，定向调整沿用既有设计。
保留生产真正需要的能力：

- 探测剪映安装、草稿目录、版本和 DLL；
- JSON 编解码、对象/文件/目录哈希；
- 使用真实 Windows 宋体测量中英字幕，中文在上、英文在下，支持关键词黄色和字号放大；
- 从剪映原生中英模板轨道克隆字幕，一次备份并同步现有内容镜像；
- 对外部写入保持显式调用，不在导入包时加载 DLL。
- 使用显式候选计划统一状态、语义、预设证据和操作顺序；不从旧视频脚本继承全局变量。
- `preset-select`查询后通过`preset-use`保存选择，再查询下一段；比较实际字体、颜色、原配音频及前后关系。`material-frame/use`保留源内取景和窗口布局；`creative-review`核对观察需求、Hook、长停留、实际运动和整片分布，可按需输出文本分镜；`creative-batch`把已选交接按依赖保存到一个当前计划并支持失败续跑，详见[合同](docs/CANDIDATE_PLAN_V1.md#批量执行已选交接)。最终布局补查媒体与媒体/主体保护区，离线音频补测同期音量风险。

完整创作从[剪辑手册第五步](../reference_videos/analysis/教程统一整理_v1/教程使用说明书_v1.md#首版工作法)开始：沿用已确认粗剪 → 开场及段落表现 → 对时 → 素材构图 → 文字阅读 → 原配音效 → 参数与装配。具体字段及阶段规则见[CandidatePlan](docs/CANDIDATE_PLAN_V1.md)。设计判断由主代理承担，代码不设审美分数或效果数量配额。

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
D:\python\python.exe -B -m jianying_adapter preset-select VIDEO/planning/candidate_plan.json --event EVENT_ID
D:\python\python.exe -B -m jianying_adapter material-requests VIDEO/planning/candidate_plan.json
```

`preset-select`默认读取完整使用库并从当前计划提取前后段；只返回同表达候选和原生结构特征，当前资源与文案仍须按需预检。使用库由`scripts/build_preset_usage_registry.py`生成；代码、JSON和生成说明书共同维护，未知特征保持unknown。`material-use --plan ... --output 同目录新计划.json`连接操作、B-roll与事件需求，保留原计划；步骤见[共享素材系统](docs/MATERIAL_LIBRARY.md)。装配报告`visual_execution.sequence_summary`列出实际预设及辅助媒体时间并集，用户在TEST中判断实际声画。

## 安全边界

`apply` 只在剪映进程为 0、草稿无 `.locked` 且目标位于显式 `--allowed-root`
之下时运行。它先完整备份，再写入所有已存在的内容镜像；中途失败会自动恢复镜像。
它不启动剪映、不导出、不上传，也不触发付费功能。结构通过后交用户播放判断实际声画，不增加Agent代表帧截图、切片审核。

## 运行测试

```powershell
cd E:\AutoCUT\jianying-adapter
D:\python\python.exe -m pytest
D:\python\python.exe -m compileall -q src tests
$env:PYTHONPATH = "src"
D:\python\python.exe -m jianying_adapter --help
```

Examples against synthetic/plain JSON data:

```powershell
D:\python\python.exe -m jianying_adapter probe fixtures\synthetic_draft
D:\python\python.exe -m jianying_adapter audit fixtures\synthetic_draft
D:\python\python.exe -m jianying_adapter backup fixtures\synthetic_draft .tmp\backup
D:\python\python.exe -m jianying_adapter validate-project-state VIDEO\project_state.json
D:\python\python.exe -m jianying_adapter validate-candidate-plan VIDEO\planning\candidate_plan.json
```

`validate-candidate-plan` 会把当前粗剪 SHA256、语义门禁、单视频授权和实际入选预设的
`current_video_evidence` 绑定在一起。人物遮挡按节点画面职责判断；有意遮挡必须记录设计依据与
授权来源，文字出画和未解决的普通字幕碰撞始终拒绝。候选执行契约见
`docs/CANDIDATE_PLAN_V1.md`。

转写压缩与局部时间线检查（只处理指定区间）：

```powershell
D:\python\python.exe -m jianying_adapter pack-transcript transcript.json -o transcript.md
D:\python\python.exe -m jianying_adapter timeline-view input.mp4 12 24 -o .tmp\timeline.png --transcript transcript.json
```

`pack-transcript` 支持本项目的 `segments`、`files` 和 flat `words` JSON；`timeline-view`
使用本机 ffmpeg、Pillow 生成帧条、波形和词/静音标记，不扫描全片。该工具用于第三步切口疑点或用户明确要求的技术定位，不作为装配后独立画面审核的固定步骤。

## B-roll 来源与授权台账

先按当前画面需求查本地共享库，再通过`sucai`导航及现有来源策略实搜实看；不合适时按当前授权生成、检查并入库。入口统一为[共享素材系统](docs/MATERIAL_LIBRARY.md)。站点顺序只提供检索线索，不能替代
语义匹配、构图检查或用户主观验收。Mixkit 必须记录具体素材许可；Videvo 默认关闭，只有
单条素材明确允许商业项目、不是 Editorial Use Only，且署名要求已经记录时才能使用。

下载进入项目的每一条 B-roll、图片、音乐或音效，都应写入
`jianying_media_asset_ledger_v1` 台账，至少保留语义节点、中英文搜索词、素材页、许可页、
许可检查时间、许可名称、署名要求、本地冻结路径和 SHA-256。写草稿前运行：

```powershell
D:\python\python.exe -m jianying_adapter audit-media-ledger MEDIA_LEDGER.json
```

命令默认读取 `assets/media_source_policy_v1.json`，并检查来源域名、商用许可、Editorial
限制、署名文本、本地文件和哈希。未通过时不得进入生产草稿。

字幕计划只包含两个原生模板轨道和字幕卡片：

```json
{
  "zh_template_track": "ZH_TEMPLATE",
  "en_template_track": "EN_TEMPLATE",
  "cards": [
    {
      "start_us": 0,
      "duration_us": 1000000,
      "zh": "重点内容",
      "en": "Key point",
      "zh_keywords": [{"start": 0, "end": 2, "color": "#FFD600", "size_scale": 1.12}]
    }
  ]
}
```

```powershell
D:\python\python.exe -m jianying_adapter jianying-8-8-launch-contract `
  --install-dir E:\AutoCUT\tools\JianyingPro_8.8.0.13328_portable\JYPacket\8.8.0.13328
```

上述命令只生成精确 `process:` 启动合同，不启动剪映。真实草稿统一经封版后的
`scripts/write_candidate_plan.py` 单 Writer 写入；普通 `apply` 是底层历史接口，
不能替代 CandidatePlan 的生产门禁。完整 profile 与 Writer 参数见
`docs/JIAN_YING_ENVIRONMENT_AND_PROFILES.md`。

codec 源的上游版本见 `vendor/pyJianYingDraft-source/UPSTREAM.md`；生产执行显式绑定
经 PE 版本核验的 8.8 安装目录和双 8.8 兼容参考，不再使用旧 11.3 示例作为生产入口。
