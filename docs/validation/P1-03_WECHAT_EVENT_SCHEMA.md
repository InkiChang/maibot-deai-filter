# P1-03 微信事件字段核验（脱敏）

日期：2026-11-19

## 结论

已在生产 MaiBot 实例上以只读方式核对适配器源码、宿主 Hook 序列化逻辑，并对持久化消息元数据做聚合查询。真实入站私聊记录支持确认 `platform`、`account_id`、`message_id`、`user_id` 与 `session_id` 均存在，且宿主会将会话标识与平台/账号/用户元数据关联。没有读取或导出聊天正文、昵称或任何实际标识值。

**P1-03 仅部分完成，任务保持未勾选。** 当前微信适配器明确是私聊通道：它没有群聊入站字段，宿主库中本次检出的该平台记录也没有 `group_id`。因此无法据此声称已核实真实群聊事件结构；此外，持久化数据库不是Hook原始事件快照，完整Hook对象的运行时字段仍未通过一份经脱敏的真实事件样本直接确认。

## 字段与来源

适配器源码 `/MaiMBot/src/plugins/built_in/weixin_clawbot_adapter/codec.py` 与 `connection.py`：

- 适配器只接收 `message_type == 1`，要求 `from_user_id` 后路由；当前实现构造平台、账号、消息ID、时间戳、用户信息、文本消息段及图片标记。
- 适配器构造体包含 `platform`、`account_id`、`message_id`、`timestamp`、`message_info.user_info.user_id`、`raw_message`、`is_picture`；没有填充 `group_info`。群聊不是该适配器当前支持的入站类型。
- MaiBot 在会话规范化/Hook序列化阶段加入 `session_id`。宿主会话ID按平台、账号/作用域及用户或群组身份生成；当前私聊记录关联到用户身份。
- 插件侧 `capture_message.private_text_envelope()` 采用 fail-closed 规则：要求 `platform`、`message_info.user_info.user_id`、`session_id`、`message_id` 和纯文本 `raw_message`；若顶层或消息信息含群组字段则拒绝。此函数契约不等于生产Hook运行时对象的完整快照。

## 生产元数据聚合

查询限定为 `platform='weixin_clawbot'` 且有 `received_at` 的入站行，仅检查字段的非空性、去重计数和会话表关联，不选取正文、昵称或标识值：

- 平台记录共111条，其中带入站接收时间39条。
- 39条入站记录的 `message_id`、`platform`、`account_id`、`user_id`、`session_id` 均非空。
- 39条均关联到会话表，平台、账号、用户字段一致；`scope` 为空。
- 入站记录均无 `group_id`；平台记录中也无群组ID。
- 去重后的用户数和会话数仅作为本地聚合核对，不在此披露具体数量或标识，以降低跨会话关联风险。

结构化聚合及源码SHA-256见 [`P1-03_WECHAT_METADATA_EVIDENCE.json`](P1-03_WECHAT_METADATA_EVIDENCE.json)。证据由生产端只读查询生成；未读取账号凭据文件。

## 隐私与边界

- 查询不包含 `raw_content`、处理后文本、昵称、消息ID值、用户ID值、账号ID值或session ID值。
- 不从日志采集聊天正文，也不保存真实事件对象。
- 适配器的 `raw_message` 按接口契约是文本消息段列表；本次没有验证图片、语音或群消息在Hook载荷中的真实形状。
- 群聊字段是否由其他平台/适配器提供、目标插件对群聊应如何拒绝，仍需用各自的合成夹具或用户授权的脱敏样本验证；不应推断本微信私聊适配器支持群聊。

## 后续验收条件

要关闭P1-03，至少需要：

1. 在允许的测试账号/测试会话中捕获一次真实私聊Hook对象的字段名和类型；在源端移除正文、昵称、账号/用户/会话/消息ID值后再保存。
2. 若项目确需支持群聊，另从明确支持群聊的适配器取得一份同样脱敏的真实样本，确认群组字段和身份边界；若不支持，应把需求范围明确限定为私聊并由用户确认是否接受。
3. 把真实样本与 `private_text_envelope()` 的拒绝规则逐字段对照，覆盖缺失字段、群聊标记及非文本消息。

本次未改动生产容器、适配器或用户数据；生产查询为只读聚合。
