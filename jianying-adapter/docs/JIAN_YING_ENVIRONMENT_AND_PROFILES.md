# 剪映8.8环境、Profile 与测试草稿收尾

首次配置、环境变化或排查故障时，可运行 `scripts/check_jianying_environment.py`；它是只读入口，输出
`jianying-adapter.environment-gate.v1`。调用方必须显式传入 `profiles_root`、profile 类型、
预期8.8 exe、C逻辑root、物理root和阶段。启动前要求进程0、锁0；启动后要求所有
Jianying/CapCut进程的实际exe都精确等于预期8.8路径且至少一个。目标草稿注册只接受唯一的
C逻辑路径；同一草稿的E路径或重复注册都会拒绝。日常写入复用内部必要检查，不额外运行一轮完整诊断。

日常不要求 CUA、SKY、Doctor 三套取证，诊断工具只在出现具体故障时按需使用。
Windows进程检查通过Toolhelp32快照先按名称筛选剪映/CapCut，再只读取目标exe；不能用psutil.process_iter的name字段替代快照，因为该字段在Windows也会调用exe读取。快照失败或目标exe读取失败仍阻止放行；Writer复用同一共享检查。实现见`jianying_environment.collect_jianying_processes`。
首次配置或环境变化时确认版本与目录；写入中的路径、进程、锁与登记检查由程序自动处理，
不让用户逐项确认。Agent操作界面时以实际工具返回的目标窗口及操作结果为准；用户自己打开
草稿验收时，不需要 Agent 电脑控制证明。可选诊断记录不能冒充真实画面证据。

宿主入口与 Codex 私有目录必须区分：Windows 的 AppData 重定向可能使后台读取同名 C 路径时，实际命中 `Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local` 的链接。后台 `samefile` 只能证明该进程所见路径，不能单独证明桌面剪映的目录。仅在后台与桌面结果不一致时，通过实际文件读取或独立宿主进程核对，再修复已授权的宿主入口；不把此故障诊断增加为每次 Writer 的固定步骤。修复后以原桌面快捷方式正常退出、重开验证。

上述简化不取消当前视频原生视觉证据、素材完整性和用户画面验收。
workspace 预览 JSON 默认不登记真实草稿，也不等于已在剪映首页可见。
已获当前用户授权的 `--preview-test` 可以把绑定的预览写入独立 test profile 草稿；
预览 manifest 仍为 `writer_allowed=false`，不得据此写入生产 profile。

`scripts/write_candidate_plan.py` 现在必须传 `--profiles-root` 和 `--profile-kind`：
`production` 精确对应 `8.8-production`，`test` 精确对应 `8.8-tests`。测试 profile 允许从空目录启动，
Writer默认使用 `jianying-adapter/assets/compatibility/8.8.local.json` 独立共享技术参考，也可用
`--compatibility-reference-path` 显式指定。通过现有codec读取后要求正整数`version`、非空字符串
`new_version`，且 `platform.app_version` 与 `last_modified_platform.app_version` 同为8.8.0。
共享参考来自已授权且成功写入验证的四个顶层字段，保留真实值；`new_version`不是应用版本，禁止改成8.8.0。
参考只在本地保存（含设备标识，不提交Git）；缺失或环境变化时重新准备，不从历史TEST自动抓取，也不每片重复申请。
测试Writer禁止直接读取活动生产profile作为兼容参考，也不会自动切换C盘Junction。

封版候选写后所有检查成功才推进项目唯一状态至 `written_pending_visual_qa`，并保存真实草稿路径、
登记结果和备份。写后失败必须可恢复回滚新增草稿/索引和项目状态。用户在剪映完成主观验收前
不推进 `completed`；已有目录和索引登记也不能证明当前首页已展示或用户已看过。
`--preview-test` 写后只登记独立测试预览结果，状态保持 `planning`；实际原生画面验证后仍需封版。

`scripts/finalize_test_draft.py` 取代历史硬编码24草稿脚本，且只处理一个精确测试草稿。前台证据
必须声明完整播放、客观审计和正常退出均完成，客观结果通过且媒体丢失数为0，并至少提供一个
存在的证据文件。`--apply` 前要求进程0、锁0、C/test-profile samefile和唯一C路径注册；执行时
先备份root_meta与草稿，再移动到workspace quarantine，最后原子移除注册。任何失败会恢复
root_meta并把已移动草稿放回；不会永久删除，也不会修改Junction。

## 保存、退出与写入冲突

用户明确要求整理旧测试草稿时，同一收尾工具可使用 `--cleanup-request`，填写精确草稿名、ID及用户授权来源；这是可恢复清理，不要求被淘汰版本先获得视觉通过，也不会生成视觉验收结论。执行前检查保留草稿未引用待移出目录内的媒体，保留必要资源。仍检查进程、锁、正确test入口与唯一登记，备份、移出与移除索引失败则恢复；恢复时按报告中的单条登记合并回当前索引，不能用旧整份索引覆盖后来新增的草稿。

真实草稿只允许一个Writer。写前确认没有保存/写入冲突，备份涉及的草稿和索引，再一次合并写入并检查；失败恢复。离线Writer优先在剪映正常退出且无`.locked`后写入，不擅自清锁，不覆盖客户唯一原稿。
用户2026-09-08明确长期授权“下次这种情况允许你自己关闭”：当已获准的草稿写入/登记仅因剪映仍运行而受阻时，Agent可自行正常退出已确认版本，不重复请求关闭许可。先核对目标窗口与真实exe，确认无正在保存、导出或写入；可以执行退出所需的返回首页、窗口关闭或应用退出操作。出现未保存编辑、保存冲突、恢复或云同步等阻塞弹窗时暂停处理，不选择丢弃修改，也不以强制结束绕过。关闭到托盘不算退出；正常退出后仍重查进程0、锁0及Writer原有门禁。当前视频状态记录本条长期授权来源和实际退出结果，不再把历史“无GUI授权”误作正常退出的阻塞。本授权限于关闭，不扩大编辑、代播验收、导出或发布权限。
仅当对应`project_state.json`中有当前任务结束卡住、无法正常退出的剪映进程的明确授权及来源时，才可执行：执行前核对PID、启动时间、真实exe及版本，确认无保存、导出或草稿写入，仅结束目标进程。之后重查进程、锁和草稿状态，不能以进程消失代替可写。历史任务授权不自动沿用；用户最新指令先记录到当前状态再执行。未保存编辑、保存冲突、登录、恢复或云同步弹窗不能用强制结束绕过。

8.8草稿物理存储在E盘真实目录，桌面剪映实际访问的C盘`com.lveditor.draft`是Junction兼容入口。文件系统写入与登记地址必须分开：唯一Writer在校验C/E为同一目录且匹配当前profile后，通过E盘物理路径创建草稿、写内容镜像和`root_meta_info.json`，失败恢复也使用同一物理路径；登记字段仍保留宿主C逻辑地址，C/E按同一物理草稿去重，不新增E路径登记。显示C路径不等于草稿存储在C盘，也不能把C入口可读推断为经该入口创建目录一定成功。

`write_candidate_plan.py --draft-root`继续传宿主C逻辑入口，`--physical-root`传经核实的E盘profile，不能互换或只把所有C字符串替换成E。Writer向本地适配层同时传两者；DraftFolder的文件IO使用物理root，`registration_root_path`只控制登记字段且必须与物理root为同一目录。首次配置、映射变化或宿主/私有AppData不一致时仍按本文核实实际宿主；不改Junction来绕过profile检查，不改素材、字体、音效的真实来源路径。所有8.8写入仍解密核验兼容参考的`platform.app_version`与`last_modified_platform.app_version`同为`8.8.0`，任一被11.x改写即拒绝，不以名称或历史验收代替。

## 原生个人预设包导入

已有 `Combination` 完整预设包用 `scripts/install_existing_presets.py prepare` 准备，传入
`--source-root`、`--target-root`（E盘 `Combination/Presets`）、`--cache-root` 与全新工作区
`--workdir`；现有字体目录可重复传 `--resource-root`，已恢复资源的实际路径记录可传
`--resource-evidence`。准备阶段只读取源库和当前个人预设，在工作区冻结完整本地资源并生成
`import_manifest.json`。保留文字、动画与轨道差异；仅重复副本及实证的保存回写默认字段合并，
同ID的真实变体取得稳定新ID，同名项取得稳定后缀。缺依赖或版本待核验项保留具体原因。

资源修复按原路径、原缓存文件名及同类型资源ID关联；富文本内的字体/花字ID、音效的
`music_id`/`pgc_id`也参与解析。裸占位符必须绑定资源ID，不能据同名占位符套用其他资源。
特效资源保留完整包入口及`lumi_hub_path`等子路径；多个同ID包内容不同则暂缓，不能任选新版。
作者生成的抠像/算法结果必须找回原输出，不能用算法定义包代替。补回字体须记录实际字族、字重
与来源，补回音效须记录原ID与实际文件；这些依赖检查不代表原生效果或完整试听通过。
按原文件或原包哈希补回的特定版本在资源证据中使用 `original_only: true`，只绑定其已验证原路径；
解析器不把这类映射再扩展到同ID、同名字体或另一包哈希的缺失引用。其余版本继续保留待补状态。

缺失的原生音效、字体及效果包可用同一入口的 `recover --manifest <准备清单> --workdir <E盘全新工作区>`
补回。它只查询清单中未解决的资源ID，匿名接口明确标记免费、未过期后才下载，并核对下载文件
与原路径记录的MD5；字体和效果核对原ZIP哈希、解包边界及实际入口。输出的
`resource-recovery.json`作为下一次 `prepare --resource-evidence` 输入，不把恢复目录作为通用资源根。
音效还需完整解码检查；原作者私有素材、未取得资源、不同哈希的版本及会员资源继续保留缺失原因。
此恢复命令只写E盘工作区，不写原生缓存、个人预设或时间线草稿。

用户授权导入后，正常退出剪映，再用 `scripts/install_existing_presets.py apply --manifest <清单>`
新增包及共享资源、追加 `CombinationPresetVirtualStore.json`。已有预设目录不覆盖、不改名；
原索引先备份，写前检查进程与清单是否变化，所有改写后依赖须属于同一清单，并回读文件与索引。
此入口不写时间线草稿、不改变生产选择或视觉验收资格；原生列表显示、实际加载/播放和用户验收分别记录。

安装后通过同一入口`bind-catalog --catalog <all_auto_candidates_v1.json> --manifest <清单，可重复> --output <更新目录> --report <报告>`接回自动调用。
它只把实际安装字节与清单一致、版本兼容且资源存在的项绑定到既有模板编号，保留`original_source_path`，从实际安装表示重建槽位定位，避免同内容副本的本地ID不同造成错字或引用缺失。
未安装、已改动或仍缺资源的包不绑定；不增加视觉或生产资格。更新原目录后运行既有`build_preset_library_index.py`及`build_preset_usage_registry.py`同步检索入口，原目录留可恢复副本。

## 仅在电脑控制或可见性故障时

使用 `@oai/sky.launch_app` 启动已确认的8.8时，传入 `process:` 加完整EXE路径的应用标识；
2026-09-06裸路径调用未锁定8.8，随后观察到11.4窗口，改用上述标识后返回真实8.8窗口。
这不证明桌面快捷方式被改写，也不推断工具内部匹配原因。启动后仍以返回窗口的实际应用路径确认版本，
不固定窗口ID。时间线跳转后须等画面刷新再存证；时间码更新可能早于文字渲染，暂时无字不能直接判为丢字。

按实际错误选择Doctor、CUA或SKY，不固定跑全套。没有直接证据不新建桥、覆盖配置、禁用插件或安装组件；一次改动无效先撤回，不要求用户连续重启。以原先失败的实际操作复测，工具缺失只有在当前任务确实依赖它时才构成阻塞。误开其他版本或无法确认目标窗口时不继续操作。
