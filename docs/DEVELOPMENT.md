# 开发与验证

模块保持直接：`main.py` 处理钩子、后台与命令；`routine.py` 处理作息和时间建议；`store.py` 保存小型 JSON 状态；`interop.py` 使用 companion v1；`runtime.py` 读取当前人格与情绪并发送可选主动问候。

前台只读缓存并排队，不在 `on_llm_request` 中进行模型调用、Provider 查询或存储操作。动态背景通过 `TextPart.mark_as_temp()` 放在 `extra_user_content_parts`，不改写人格提示词。[AstrBot 官方消息事件说明](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)。

## 本地测试

从仓库父目录执行（Python 3.12+）：

```powershell
python -m pip install "astrbot==4.29.4" ruff -r astrbot_plugin_daily_routine/requirements.txt
python -m ruff check astrbot_plugin_daily_routine
python -m unittest discover -s astrbot_plugin_daily_routine/tests -v
```

`test_installation.py` 会在临时 AstrBot 数据目录中实际安装 ZIP、初始化插件、保存配置并热重载，不连接真实聊天平台。日常提取与问候模型使用可控响应验证，不调用付费模型。

同目录存在配套仓库时，`test_interop.py` 会加载其真实 companion v1 实现，验证发现、关闭联动、睡眠抑制及共享注意力。独立检出本仓库时，这部分跨仓库测试会显示跳过，其余测试仍运行。

## 接口

- `companion_snapshot(origin)`：固定返回空对象，避免 v1 的自动前台注入绕过本插件控制。
- `companion_memory(origin, persona=..., actor=...)`：版本 1 的本人直接形成记忆，不包含导入内容。actor 与自主认知的账号哈希规则一致。两边各自后台读取、缓存；不走前台钩子。
- `companion_timing(origin, role=..., sender=..., persona=...)`：读取当前时机及 `initiative_factor`。群聊必须明确 sender 才提供个人触发系数。
- `companion_allow_initiative(origin, role)`：仅供后台主动候选使用。不存在已知安排或双方未开启联动则允许，工作时减少、睡眠时暂停。
- `companion_observe(packet)`：只接收其他插件已经发出的主动消息时间，不把它当成人的作息观察。
- 自主认知增加可选 `companion_snapshot(origin, persona=None)` 参数，旧调用保持兼容。

配套插件原有 NAMES 名单可在运行中临时登记新成员。只有当前识别的 v1 名单会扩展；关闭联动或卸载后撤销本实例拥有的登记，不替换配套方法。

## 验证边界

0.2.0 的专用场景验证在 `test_daily_memory.py`：无关任务不注入日常、相关召回、临时安排日期、事实补成区间、真实问答中的省略回答、旧语境不能成为新事实证据、可选好奇及间隔、WAIT、旧版数据读取。使用原有后台整理调用同时处理记忆与好奇，不另增前台模型调用。

0.3.0 的七项反向审查回归在 `test_review_fixes.py`，双插件实际类验证在 `test_mutual_memory.py`。包括限期例外后恢复原班次、新答案不复提旧问、前台开关不被自动快照绕过、保存间隙收到消息不发送、独立 Provider 启动、双向学习、时间不刷新、账号与人格隔离、删除传播、重新加载、逐条清除指纹及不循环导出。

自动测试覆盖后台调度、真实 SDK 的安装和钩子、持久化、夜班与日期例外、个体隔离、可选联动、情绪接口、问候 WAIT 与不连发。测试不证明任意模型能正确理解所有自然语言，也不替代真实平台上的主动发送验收。实际识别结果可用命令核对、修正和删除。
