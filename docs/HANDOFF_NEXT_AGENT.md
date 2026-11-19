# 项目交接：MaiBot DeAI Filter

> 面向下一位 Agent 的执行交接文档。先读本文件，再读 `TASKS.md` 和相关验证文档；不要把规划项当成已实现功能。
>
> 交接日期：2026-11-19
> 当前仓库：`InkiChang/maibot-deai-filter`
> 本地项目目录：`[redacted]\managed-ga-state\artifacts\maibot-deai-plugin`
> 远端：`https://github.com/InkiChang/maibot-deai-filter.git`
> 默认分支：`main`

## 1. 接手后的第一步

在任何修改前执行：

```powershell
cd [redacted]\managed-ga-state\artifacts\maibot-deai-plugin
git status --short --branch
git log -5 --oneline
python -m unittest discover -s tests
```

然后依次阅读：

1. `TASKS.md`：唯一任务进度来源；只有有实现和证据才勾选。
2. `README.md`：当前真实功能、配置、安装、回滚和安全边界。
3. `docs/validation/P1-01_VERSION_AND_HOST_VALIDATION.md`：真实宿主版本、插件注册和Hook加载证据。
4. `docs/validation/P1-02_LIFECYCLE_VALIDATION.md`：隔离Runner生命周期证据及其边界。
5. `docs/validation/P1-03_WECHAT_EVENT_SCHEMA.md`：微信私聊字段脱敏核验及未完成原因。
6. `docs/validation/P1-03_WECHAT_METADATA_EVIDENCE.json`：仅含聚合统计和源码哈希，不含正文和真实标识值。

如果工作区不是干净状态，先区分已有用户改动，不要覆盖或重置。

## 2. 当前Git与CI基线

- 当前HEAD：`6ca940d79a5993935a083ebc5cb0b8c55c8df45c`
- 提交标题：`Document sanitized WeChat event metadata verification`
- 本地 `main` 与远端 `origin/main` 已一致。
- 工作区已核实干净。
- 对应 GitHub Actions：<https://github.com/InkiChang/maibot-deai-filter/actions/runs/[redacted]>
- Actions 结论：成功。
- 最近提交链：
  - `6ca940d`：P1-03 脱敏微信事件字段证据
  - `bbcad57`：P1-02 隔离生命周期/异常恢复测试
  - `7871ded`：P1-01 生产插件注册证据
  - `617c5f7`：P1-01 宿主版本与加载验证
  - `f25c16b`：P1-01 版本和宿主验证计划

每次修改后至少执行：

```powershell
python -m unittest discover -s tests
git diff --check
git status --short --branch
```

提交前只暂存本次任务相关文件；推送后核对：

```powershell
git rev-parse HEAD
git ls-remote origin refs/heads/main
gh run list --repo InkiChang/maibot-deai-filter --limit 5
```

## 3. 已完成的工程基线（P0）

P0-01 至 P0-11 已完成。关键事实：

- 仓库已公开创建，默认分支为 `main`。
- Topics：`maibot`、`python`、`text-processing`、`chatbot`、`ai`。
- MIT 许可证、README、manifest、配置和项目元数据已核对。
- setuptools 发行白名单和发行包结构检查已建立。
- GitHub Actions 覆盖发行构建、包结构检查和离线测试。
- 本地静态敏感信息检查未发现常见密钥赋值、私有IP、本机目录或私钥标记。
- `RESPONSE_SECRET_MARKER` 是测试哨兵字符串，不是凭据。
- P0-07 明确没有把本地静态检查夸大成 GitHub Secret Scanning 零告警结论；平台告警状态未在清单中宣称已核实。

## 4. 当前代码真实范围

已实现并有离线测试：

- `plugin.py`、`deai.py`：MaiBot 扩展入口和确定性“去 AI 味”文本过滤。
- `capture_message.py`：fail-closed 的私聊纯文本信封提取。
- `memory_capture.py`、`memory_store.py`：内存型、带 owner/session 隔离的待审候选和记忆基础件；不持久化、不自动批准、不接入 A-Memorix。
- `state_engine.py`：规则状态计算与版本化序列化；尚未接入宿主生命周期。
- `candidate_intake.py`、`candidate_scoring.py`、`candidate_viewer*.py`：候选校验、评分和本机查看器基础件。
- `subq_config.py`、`subq_client.py`、`subq_schema.py`：独立 SubQ 配置、注入式客户端核心和低权限决策schema；没有内置真实HTTP传输、没有真实Provider兼容声明、没有接入MaiBot Hook。

明确未实现/不能宣称：

- 没有完整的个人拟人系统、人设编辑器、生活系统或设置WebUI。
- 没有真实SubQ Provider/API smoke test。
- 没有直接连接 A-Memorix、MaiBot 内部数据库、内部 Python 对象或 WebUI memory endpoint。
- 规划文档不代表功能已经上线。

## 5. P1状态与证据

### P1-01：完成

目标组合：MaiBot `v1.4.5` + `maibot-plugin-sdk==2.23.0`。

已核实：

- NAS实时SSH ED25519指纹与系统 `known_hosts` 匹配：
  `[redacted]`
- 生产 `maibot-core`：`running/healthy`，重启次数为0。
- 容器内实际 SDK：`2.23.0`。
- 插件 manifest、TOML配置、Python语法均可解析。
- 生产持久化JSONL证明插件导入、配置、PluginContext注入、注册和 `maisaka.reply.after_split` 加载成功。
- 隔离Hook E2E通过，且生产容器前后状态不变。

边界：生产启动注册成功不等于真实聊天Hook效果已验收；部署文件与仓库HEAD也不保证完全一致。详见 `docs/validation/P1-01_VERSION_AND_HOST_VALIDATION.md` 和 `docs/validation/P1-01_PRODUCTION_REGISTRATION_EVIDENCE.json`。

### P1-02：完成，但只覆盖隔离Runner语义

使用真实 MaiBot `PluginRunner`/`PluginLoader` 和 `sengokucola/maibot:v1.4.5` 镜像，在临时隔离容器验证：

- create/on_load、配置更新、禁用/启用、卸载/recover幂等；
- on_load异常清理、旧实例回滚、修复后reload；
- shutdown任务清理；
- Hook注册和分发。

完整离线套件当时为 `138 passed, 12 skipped`。RPC使用内存Stub，未覆盖生产IPC断连、生产热启停或真实微信事件竞争。详见 `docs/validation/P1-02_LIFECYCLE_VALIDATION.md`。

### P1-03：部分完成，必须保持未勾选

已验证：

- 生产 MaiBot 数据库中 `platform='weixin_clawbot'` 且有入站时间的记录共 `39` 条。
- 39条均有非空 `platform`、`account_id`、`message_id`、`user_id`、`session_id`。
- 39条均与会话表关联，平台/账号/用户字段一致。
- 本次查询未读取正文、昵称、真实ID值或账号凭据文件。
- 当前微信适配器只接受 `message_type == 1`，要求 `from_user_id`，不填充 `group_info`；本次平台记录无 `group_id`。
- 宿主在会话规范化/Hook序列化阶段补充 `session_id`。

未验证：

- 没有脱敏的完整真实Hook运行时事件快照。
- 没有真实群聊事件样本；当前微信适配器本身是私聊通道。
- 没有验证图片、语音或其他非文本事件在Hook载荷中的完整形状。

因此不要把P1-03改成 `[x]`。若项目范围接受“只支持本人私聊”，应由用户明确确认后再调整任务定义；否则需取得合规的脱敏真实样本。详见：

- `docs/validation/P1-03_WECHAT_EVENT_SCHEMA.md`
- `docs/validation/P1-03_WECHAT_METADATA_EVIDENCE.json`
- `capture_message.py`

## 6. 下一步推荐顺序

### 首要任务：P1-04

验证回复Hook的：

1. 执行顺序：进入Hook前、过滤器执行、返回宿主之间的先后关系；
2. 参数结构：段列表、段字段、session_id、Hook参数和额外字段；
3. 允许修改字段：哪些字段会被宿主采纳，哪些必须原样保留；
4. 错误策略：坏输入、过滤器异常时是继续原回复、跳过该段还是阻断；
5. 日志边界：不得保存完整聊天正文、密钥或真实身份值。

推荐方法：先读 MaiBot `v1.4.5` 的公开Hook序列化/反序列化代码和仓库现有 `tests/test_hook_runtime_e2e.py`，再写最小隔离fixture；不要直接在生产实例发送测试消息或修改生产Hook。

完成条件：有源码依据、隔离E2E或等价测试、真实宿主边界说明，并在 `TASKS.md` 追加证据后才勾选P1-04。

### 随后任务

- P1-05：验证 Replyer 前置Hook 能否安全中止/延迟；若不能，禁止设计静默拦截。
- P1-06：本人私聊身份硬校验；未知、非本人和群聊事件最前入口拒绝。
- P1-07：拒绝事件不得读状态、注入上下文或调用模型。
- P1-08：脱敏 Prompt 快照、注入对象/顺序/token增量。
- P1-09：合成/脱敏基线集。
- 真实微信端到端和发布验收属于更后的 P2/P8，不要提前宣称完成。

## 7. 生产宿主访问与安全纪律

生产宿主连接信息不写入本交接文档或公开仓库。新Agent若需要生产验证，必须由用户在新机器上单独提供授权的连接方式、主机指纹和最小权限凭据；不得从Git历史、公开文档或聊天记录猜测凭据。

安全访问规则：

- 使用用户提供的 `known_hosts` 严格校验主机；禁止自动接受未知指纹。
- 凭据只在内存中使用，不打印、不保存、不写入环境变量日志、脚本或仓库。
- 生产探测默认只读；不要读 `account.json`，它可能含token、游标和私聊上下文。
- 日志/数据库分析必须在远端先聚合和脱敏，再只返回字段名、类型、计数或一致性结果；禁止把聊天正文、昵称、真实ID带回本地或写入Git。
- 不要执行生产重启、热加载、插件覆盖、配置写入或发送真实微信测试消息，除非用户明确授权且先写回滚方案。
- 隔离E2E必须使用临时容器、`--network none`、只读根文件系统、`--cap-drop ALL`、`no-new-privileges`，并在 finally 清理。

## 8. 代码和测试入口速查

```text
plugin.py                         插件入口/Hook注册
_deai.py?                         实际文件为 deai.py，确定性过滤
capture_message.py                私聊纯文本 fail-closed 提取
memory_capture.py                 有界、易失候选捕获队列
memory_store.py                   内存记忆/候选存储
subq_config.py                    SubQ配置解析
subq_client.py                    注入式SubQ客户端核心
subq_schema.py                    低权限决策schema
state_engine.py                   规则状态引擎
tests/test_plugin.py              过滤器和Hook桩测试
tests/test_hook_runtime_e2e.py   opt-in隔离Hook/生命周期入口
tests/fixtures/hook_runtime_e2e.py Hook隔离fixture
tests/fixtures/lifecycle_runtime_e2e.py 生命周期fixture
```

常用测试：

```powershell
python -m unittest discover -s tests
python -m unittest discover -s tests -p test_plugin.py -v
python -m unittest discover -s tests -p test_hook_runtime_e2e.py -v
```

最后一个默认只会跳过远端E2E；不要在没有指纹、镜像和明确授权时强行开启。

## 9. 新Agent的交付协议

每个任务结束时必须交付：

1. 实际修改的文件完整路径；
2. 测试命令及通过/跳过数量；
3. 生产探测是否只读、是否改变容器状态；
4. 仍未验证的边界和残差；
5. `git diff --check`、提交哈希、远端main哈希和Actions结论。

严禁：

- 把规划、源码静态分析或Stub测试写成真实生产效果；
- 为了勾选任务而隐藏缺失证据；
- 读取、打印、提交密钥和完整聊天内容；
- 擅自把未确认的群聊/本人身份规则当成已实现；
- 修改生产环境后不做前后状态对照和回滚说明。
