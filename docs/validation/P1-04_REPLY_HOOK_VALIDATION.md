# P1-04 回复 Hook 验证记录

> 状态：**完成（隔离验证与公开宿主源码证据）**。本记录不宣称真实微信消息效果，也不把 Stub/合成测试写成生产验收。
>
> 记录日期：2026-11-19

## 1. 验证范围与安全边界

本项验证目标是 `maisaka.reply.after_split` 的执行顺序、参数/返回结构、可修改字段和错误策略。验证仅使用：

- 仓库当前插件源码；
- MaiBot `v1.4.5` 公开源码（固定 tag，对应提交 `478f671e64467121fd3e39205c8d3507b5ce0bd6`）与 SDK `v2.23.0` 公开类型定义；
- 进程内 SDK Stub、合成 Dispatcher 和仓库既有隔离 fixture 的离线测试。

未连接生产主机，未读取部署资料、凭据、账号文件、聊天正文、昵称或真实身份值；未重启、热加载、覆盖生产插件，未发送真实微信消息。

## 2. 公开宿主源码依据

- SDK `v2.23.0` 的 `ReplyAfterSplitPayload` 将 Hook 参数定义为 `segments`、`session_id`、`enable_chinese_typo`；`ReplyPostProcessSegment` 的段字段为 `text`、`separator`、`quote_previous`、`is_correction`。参见 [SDK types.py](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/v2.23.0/maibot_sdk/types.py)。
- MaiBot `v1.4.5` `HookDispatcher.invoke_hook()` 对处理器按 `blocking` 在 `observe` 前、再按 `early` → `normal` → `late` 排序；blocking 处理器串行执行，返回 `modified_kwargs` 时在 HookSpec 允许 `allow_kwargs_mutation` 的条件下替换当前 kwargs，`action=abort` 只有在 `allow_abort` 时才中止。参见 [hook_dispatcher.py](https://github.com/Mai-with-u/MaiBot/blob/v1.4.5/src/plugin_runtime/host/hook_dispatcher.py)。
- Dispatcher 对处理器失败按该处理器的 `error_policy` 处理；`skip`/`log` 不设置中止，错误进入聚合结果，继续主链。超时、RPC/处理异常不会凭空产生修改后的 kwargs。Hook 规格字段和变更/中止能力见 [hook_spec_registry.py](https://github.com/Mai-with-u/MaiBot/blob/v1.4.5/src/plugin_runtime/host/hook_spec_registry.py)。
- MaiBot 入站 Hook 的消息对象会经序列化；但本项回复 Hook 的最终业务调用仍以 `HookSpec` 与调用方实际注册为准，不能仅由 SDK 类型声明推断每个部署的运行时字段白名单。参见 [message_utils.py](https://github.com/Mai-with-u/MaiBot/blob/v1.4.5/src/plugin_runtime/host/message_utils.py)。

## 3. 插件实现证据

当前插件在 [`plugin.py`](../../plugin.py)：

1. 注册 `maisaka.reply.after_split` 为 `BLOCKING`、`EARLY`、`SKIP`（第 245–252 行）。
2. 进入 handler 后依次检查配置和 `segments` 类型；按输入顺序逐段校验并调用过滤器；丢弃被清空的段；然后按配置去重（第 253–288 行）。
3. 全部段被清空、无变化或出现坏输入时返回 `{"action": "continue"}`，不提交空列表或部分不可信结果（第 256–291 行）。外层异常也返回 `continue`（第 302–303 行），因此插件自身是 fail-open。
4. 有变化时返回 `{"action": "continue", "modified_kwargs": {**kwargs, "segments": kept}}`（第 301 行）：顶层传入 kwargs 保留，仅替换 `segments`。
5. 每个输出段由 `_normalize_segment()` 限定为四个宿主字段 `text`、`separator`、`quote_previous`、`is_correction`（第 50–57 行）；任意额外段字段不会透传，原输入段顺序保留。

## 4. 隔离测试证据

新增 [`tests/test_p1_04_reply_hook.py`](../../tests/test_p1_04_reply_hook.py)，共 5 项：

- 注册元数据：`BLOCKING` / `EARLY` / `SKIP`；
- 合成 Dispatcher 的进入 → handler → 返回 → 采纳顺序；
- 顶层 `session_id`/额外参数保留，段元数据和顺序保留，额外段字段丢弃；
- 坏输入、全段清空时保留原 kwargs（fail-open）；
- 注入过滤器异常时保留原 kwargs，不阻断回复。

命令：

```bash
python -m unittest discover -s tests -p 'test_p1_04_reply_hook.py' -v
```

结果：**5 项通过**。该测试使用进程内 Stub 与合成边界，不连接 MaiBot/网络/模型，不发送消息；它证明插件契约和隔离语义，不证明生产微信端到端效果。

仓库既有 [`tests/fixtures/hook_runtime_e2e.py`](../../tests/fixtures/hook_runtime_e2e.py) 还提供 opt-in 的真实 MaiBot Runner/Registry/Dispatcher 隔离入口；默认未启用，避免在没有用户明确授权、主机指纹和匹配镜像时连接任何环境。该 fixture 自行注册 HookSpec，不能替代生产 HookSpec 或真实消息验收。

## 5. 结论与未覆盖边界

已确认：

- 宿主公开 Dispatcher 的 blocking/observe 与 early/normal/late 排序规则；
- 插件声明和内部处理顺序；
- SDK 公开段结构；
- 插件允许返回的修改形态、保留字段、坏输入/异常 fail-open 策略；
- `SKIP` 的插件声明与宿主公开错误处理方向（不主动 abort）。

仍未确认：

- 当前生产部署的 HookSpec 是否与公开 `v1.4.5` 完全一致；
- 生产部署插件源码与仓库 HEAD 是否一致（P1-01 已明确哈希不同）；
- 真实微信回复在生产调用链中的最终采纳效果、延迟、多个生产插件之间的排序竞争；
- 真实生产 Hook 异常/超时和真实消息字段样本。

因此本项完成仅表示“公开宿主契约 + 隔离等价测试 + 明确边界”已齐全，不等于生产聊天效果验收。后续真实生产验证必须由用户通过独立安全渠道明确授权，并遵守只读、脱敏和不发送真实消息的限制。
