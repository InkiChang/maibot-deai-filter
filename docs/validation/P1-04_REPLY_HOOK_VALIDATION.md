# P1-04：回复 Hook 的执行与字段采纳

**2026-11-20：按固定版本宿主隔离语义完成，不是生产微信验收。** 此前只有静态源码、插件 Stub 和单处理器分发记录，任务曾恢复未勾选。本次在获准创建的临时禁网容器中执行了真实分发器和回复调用方。生产插件未部署或改动，没有发消息。

## 版本与方法

目标是 MaiBot `v1.4.5`（公开提交 `478f671e64467121fd3e39205c8d3507b5ce0bd6`）与 `maibot-plugin-sdk==2.23.0`。[夹具](../../tests/fixtures/reply_contract_runtime.py) 在运行时校验 SDK 版本和六个相关宿主源码文件的 SHA-256；不是靠镜像标签判断版本，也不宣称整个镜像与上游字节相同。

实际调用宿主 HookSpec 注册函数、ComponentRegistry、HookDispatcher、`BuiltinToolRuntimeContext._transform_reply_segments`、规则断句和段合并。处理器注册元数据与 Supervisor 由夹具提供，跳过真实 Runner IPC；插件使用仓库源码、真实 SDK、无发送能力的合成上下文和测试配置。全部输入为合成数据，不启动适配器或调用模型。

## 实测结果

| 问题 | 观察到的结果 |
| --- | --- |
| 执行顺序 | 将处理器逆序交给分发器，blocking 仍按 early、normal、late 执行；同槽内置先于外部，同来源按插件 ID 排序。observe 随后异步执行；它返回的 abort 和空段不改变主结果。未单测同一插件内按处理器名称打破平局。 |
| 相对断句和合并的位置 | 在规则断句模式下，两行合成文本先形成两段。Hook 实际收到这两段带格式标记的文本，真实插件完成过滤，宿主按最终段数上限合并为一段。此测试未替换断句或合并函数。 |
| 进入和返回的参数 | 回复调用方实际传 `segments`、`session_id`、`enable_chinese_typo`。分发器收到 `modified_kwargs` 时**整字典替换**，不是增量合并。调用方只反序列化 `segments`；测试中修改返回的 `session_id` 没有改变会话，附加的顶层字段也未被作为段消费。 |
| 允许的段字段 | 必须返回非空列表。每段恰有 `text`、`separator`、`quote_previous`、`is_correction`；前两者是字符串，`text.strip()` 非空，后两者必须为布尔值。四个合法值均能进入宿主分段对象。 |
| 错误分层 | 插件过滤函数抛异常时，插件返回 continue，原段保留。blocking 处理器抛错或超时且策略为 SKIP 时，分发器记录错误、保留当前参数、执行后续处理器。该 HookSpec 禁止 abort：显式 abort 请求被忽略并记错。 |
| 非法返回不是 SKIP | 成功返回的空列表、非列表、额外段字段或字段类型不对，由**业务调用方**抛 `ValueError`；缺少 `segments` 抛 `KeyError`。夹具实测到调用方抛错为止。上层回复工具捕获后返回失败，是[源码依据](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/builtin_tool/reply.py#L746-L763)，不是本次完整工具或发送链路实测。 |

宿主依据：[HookSpec](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/chat_loop_service.py#L618-L644)、[分发结果合并与错误策略](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/plugin_runtime/host/hook_dispatcher.py#L594-L683)、[最终段校验](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/maisaka/builtin_tool/context.py#L403-L428)、[断句与合并之间的 Hook](https://github.com/Mai-with-u/MaiBot/blob/478f671e64467121fd3e39205c8d3507b5ce0bd6/src/chat/utils/utils.py#L518-L549)。

## 证据与复跑

实际成功夹具 SHA-256：`12f9dac548e5b68886445e70b9d29ad0298e70f6c1b8bc6e45307ea7c6012746`。最终运行输出 `REPLY_CONTRACT_PASS`，11 组检查通过。用后检查确认临时资源已删除，生产容器前后状态快照一致。仅公开结果，不公开 SSH 资料、镜像或容器标识、现场路径及原始日志。调试时先后修正了临时输入的读权限和测试插件缺少 SDK 上下文的问题；失败尝试不计入通过记录。

隔离限制：无网络、只读根文件系统、丢弃全部 capabilities、no-new-privileges、单一只读测试输入挂载和临时可写文件系统；不挂载生产数据。生产只做容器元数据前后比较。没有读取账号文件或消息记录。

同一夹具也可通过[本机 Docker 入口](../../tests/test_reply_contract_local.py)复跑（显式启用，镜像须已在本地）：

```bash
MAIBOT_RUN_LOCAL_REPLY_CONTRACT=1 python -m unittest discover -s tests -p test_reply_contract_local.py -v
```

本机镜像拉取超时，故本次通过记录来自获授权的远端临时容器，不是本机 Docker 包装器成功记录。普通 CI/离线套件默认跳过此测试。[SDK Stub 回归](../../tests/test_p1_04_reply_hook.py)仍用于快速检测插件行为，不替代上述验证。真实生产配置、多插件组合、微信实际输出与延迟仍属 P2-05/P8；此结论也不适用于 P1-05 的 Replyer 前置 Hook。
