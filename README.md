# MaiBot 去 AI 味过滤器与个人拟人系统规划

当前仓库包含一个 MaiBot 扩展：在回复发出前按配置执行确定性的文本清理（如部分 Markdown 壳、模板套话、括号动作描写和同轮重复段处理）。现有过滤器只做删改，不生成回复，也不负责拟人状态或长期记忆。

> **项目状态：** 过滤器有离线 SDK 桩测试；更完整的个人拟人系统仍在规划阶段。SubQ 决策模型规划为插件独立模块，因为 MaiBot 没有单独的决策模型配置项。规划文档不是已实现功能的说明。

## 目录

- `plugin.py`、`deai.py`：当前扩展入口与纯文本处理逻辑。
- `_manifest.json`、`config.toml`：MaiBot 扩展清单与示例配置。
- `tests/test_plugin.py`：不连接宿主、不发送消息的离线 SDK 桩测试。
- `subq_config.py`、`tests/test_subq_config.py`：独立 SubQ 配置解析及纯离线校验测试。
- `subq_client.py`、`tests/test_subq_client.py`：可注入异步传输/凭据解析器的客户端核心，含有限重试与独立日预算账本；无内置 HTTP 传输、无真实凭据集成，尚未接入消息 Hook。
- `subq_schema.py`、`tests/test_subq_schema.py`：按任务白名单校验短 JSON 决策；拒绝未知字段/动作/引用、重复键及越界值。仅产出结构化低权限决策对象，不生成回复、不执行写入。
- `state_engine.py`、`tests/test_state_engine.py`：纯离线情绪/精力/压力/无聊衰减与证据加权、关系轴有证据限幅更新、版本化状态字典序列化；状态不自动落盘，尚未接入 MaiBot 生命周期。
- `candidate_scoring.py`、`tests/test_candidate_scoring.py`：按固定权重计算候选适配度与风险；触发硬边界风险直接排除，低分弃权，高影响近似候选返回仲裁信号。仅为纯规则基础件，不调用 SubQ、不生成或发送回复。
- `memory_store.py`、`tests/test_memory_store.py`：仅内存的 owner/session 隔离短期记忆、待审候选、显式批准门槛、敏感项阻止批准、仅待审候选可合并的去重与最早过期保留、批准记录优先容量保护、中文/英文检索排序、按类别查询、过期清理、结构化安全导出、原子更正（回到待审）、显式事实键冲突隔离（同 scope/domain/key 不同内容时双方隔离、禁止普通检索/批准）、幂等关闭，以及重要性自适应衰减与显式归档/恢复（恢复回待审，过期记录不归档、不延长保留期）。38 项记忆离线测试通过。
- `candidate_intake.py`、`tests/test_candidate_intake.py`：纯离线候选来源校验钩子；要求调用方提供待审候选和可信证据解析器，解析器对每个引用按精确 owner/session 验证并必须返回布尔 `True`，否则拒绝；不做事实抽取/分类、不自动批准，也不接 MaiBot 消息来源。9 项入口安全测试通过。重要性排序仅用合成标注样例验证，衰减参数尚未用真实标注语料校准；冲突检测不做语义推断，无 conflict_key 的候选不参与冲突判断。无磁盘或 MaiBot/A-Memorix 持久化，重启后数据丢失；宿主生命周期未接入；不是候选自动抽取器。
- `docs/planning/PLUGIN_PLAN.md`：个人拟人系统总体规划。
- `docs/planning/SETTINGS_PLAN.md`：设置页信息架构规划。
- `docs/research/RESEARCH.md`：参考项目调研摘要。
- `TASKS.md`：阶段化、可勾选的唯一项目进度清单。

## 当前功能与验证

MaiBot 记忆接入边界已核验：官方 MaiBot v1.4.5 依赖插件 SDK v2.23.0；SDK 的跨插件调用要求提供方显式声明 `@API(..., public=True)`，但 v1.4.5 的 A_Memorix 插件仅声明 LLM Tools，没有向第三方插件公开记忆读写 API。插件运行于独立 Runner，不能直接导入宿主 `src.*`；宿主记忆服务、数据库和 WebUI 路由均不作为插件稳定契约。因此当前内存适配器不连接 A_Memorix，也不私调内部对象或 endpoint。待上游提供版本化公开 API/SDK capability 后再评估接入。详见 [`TASKS.md`](TASKS.md) 的 P5-06 核验记录。

过滤器在 `maisaka.reply.after_split` Hook 上逐段执行确定性文本处理：清理部分 Markdown 外壳、模型模板标记、常见 AI 客套句和动作描写；按需移除配置的 bot 名称前缀及自定义短语；清理句尾单个句号、多个破折号和重复段。它不生成文本、不扩写，也不设回复长度上限。所有规则都可能误判特定语境；启用前应以实际人设、代码和常用格式构造回归样例。

可配置项位于 `config.toml` 的 `[plugin]` / `[deai]`：

| 选项 | 作用 |
|---|---|
| `plugin.enabled`、`deai.enabled` | 插件级及过滤器级总开关 |
| `strip_markdown` | 移除标题、强调、列表/表格、行内代码等格式壳；代码块中的内容会保留但围栏去掉 |
| `strip_actions` | 移除符合动作特征的括号或星号描写；有意保留短语气括号，但自然语言也可能被误判 |
| `strip_ai_phrases` | 移除规则表中匹配的 AI 自称、套话、总结/客服腔和起手铺垫；匹配范围有限，可能误删符合模式的正常句子 |
| `strip_model_tags` | 删除常见模型模板标签；谨慎用于讨论这些字面标签的文本 |
| `strip_self_name`、`bot_names` | 删除行首配置名称加冒号的前缀；名称为空时不执行 |
| `dash_to_comma` | 同一段出现两个或更多破折号时改为逗号，可能改变作者刻意的修辞 |
| `strip_ordinals` | 至少匹配两个顺序连接词时删除“首先/其次”等词，不改写段落本身 |
| `drop_trailing_period` | 删除每行末尾单个中文句号；不处理问号、感叹号等 |
| `dedupe_segments`、`dedupe_threshold`、`dedupe_min_len` | 按相似度阈值和最小长度合并同一回复内的近似重复段；可能误删有意重复强调 |
| `extra_phrases` | 按字面值删除额外短语；用 `/…/` 写正则时须确认表达式安全且范围精确 |

### 后台用户事实候选（实验性，默认关闭）

`[memory_capture]` 使用 MaiBot `chat.receive.before_process` OBSERVE Hook，在白名单 `allowed_targets` 中精确匹配 `session_id`，只接受私聊纯文本；群聊、媒体、过长消息及未列入白名单的会话会被丢弃。消息进入有界、仅驻留内存的后台队列后，调用配置的 MaiBot 模型任务抽取候选；要求输出严格 JSON，并以消息原文精确引句验证。候选仅保存为本插件 `MemoryStore` 中的待审记录，不会自动批准、写盘或接入 A-Memorix；卸载或配置热更新时清空队列。启用前请自行核对 MaiBot 会话ID并配置 `model_task_name`、`allowed_targets`，默认 `enabled=false`。

此能力仅经过离线 SDK 桩验证，尚未在真实 MaiBot 主机/微信私聊上验收；抽取质量、来源定位稳定性、模型费用与长期运行行为仍需实测。模型输出可能漏检或误分类，待审不可作为可信事实直接使用。

离线验证命令：

```bash
python tests/test_plugin.py
python -m unittest discover -s tests -p test_subq_config.py -v
python -m unittest discover -s tests -p test_subq_client.py -v
python -m unittest discover -s tests -p test_subq_schema.py -v
python -m unittest discover -s tests -p test_candidate_intake.py -v
python -m unittest discover -s tests -v
```

真实 Runner/Registry/HookDispatcher 隔离 E2E 也包含在 unittest 发现中，但默认跳过；GitHub Actions 不配置远端凭据，因此不会连接部署环境。若要显式运行，先安装 Paramiko、配置目标 SSH 主机的系统 `known_hosts`，再在受控本机 shell 中设置：

```bash
export MAIBOT_RUN_HOOK_E2E=1
export MAIBOT_E2E_SSH_HOST=your-docker-host
export MAIBOT_E2E_SSH_PORT=22
export MAIBOT_E2E_SSH_USER=your-user
export MAIBOT_E2E_SSH_KEY_FILE=/path/to/private-key  # 或改用 MAIBOT_E2E_SSH_PASSWORD
export MAIBOT_E2E_IMAGE=your-maibot-image
python -m unittest discover -s tests -p 'test_hook_runtime_e2e.py' -v
```

仅在明确启用时会SSH连接，并要求系统 `known_hosts` 已有匹配主机密钥；未知密钥直接拒绝。远端仅短暂创建只读源码挂载的 Docker 测试容器（`--network none`、只读根文件系统、丢弃全部 capabilities），结束时删除容器和临时目录，并断言生产 `maibot-core` 容器状态未变。此测试需要远端 Docker 权限及与部署实例匹配的镜像；不要把密码、私钥或部署凭据放入仓库或CI日志。

常规测试使用最小 SDK 桩，不连接 MaiBot、微信或外部模型。通过不代表真实实例的 Hook 顺序、安装过程或微信端效果已验证；这些仍列在 [`TASKS.md`](TASKS.md)。SubQ 配置单测仅验证本地输入校验，不证明任何模型服务兼容。

## 安装、配置与回滚

将本项目作为 MaiBot 扩展安装时，保持 `plugin.py`、`deai.py`、`_manifest.json` 和 `config.toml` 位于同一插件根目录，并按目标 MaiBot 版本的插件管理方式加载。SDK 与 Hook 的真实兼容性须在目标实例验证，参见任务清单 P1。

变更前备份当前插件配置和文件。发现误删时，先在 MaiBot 插件配置中关闭对应 `[deai]` 规则；要立即完全停止过滤，则设置 `plugin.enabled = false` 或 `deai.enabled = false` 并按宿主要求重载插件。若需整体回滚，恢复备份的 `config.toml` 与插件文件，然后重载并用一条普通回复及一条边界样例确认恢复。不要覆盖宿主程序或其他插件配置。

## SubQ 边界与当前实现状态

SubQ 是插件自己的低频决策模型客户端与配置模块，不占用 MaiBot 普通模型配置。首版协议候选为非流式 OpenAI Chat Completions wire format（自定义 Base URL、Bearer 凭据引用、模型名；读取 `choices[0].message.content`），但这是设计目标，不代表用户实际的 SubQ 服务已核验或兼容。上线前须按所选服务资料核对端点、认证和响应格式并做 smoke test；当前没有任何 Provider 的实测兼容声明。

目前实现独立配置解析模块 `subq_config.py`、`[subq]` 默认关闭配置示例、不带内置网络传输的客户端核心 `subq_client.py`，以及仅校验不执行的决策 schema `subq_schema.py`。客户端仅通过显式注入的异步传输和凭据解析器构造/解析候选 Chat Completions 请求；本地 fake 测试覆盖请求格式、超时/取消、HTTP 错误分类、Retry-After 解析、重试与预算拒绝。客户端对瞬态传输错误/超时/5xx 最多额外重试 1 次；429 仅在有效 `Retry-After` 且等待不超过总时限时重试，永久 4xx 不重试；总历时受每次超时与最大尝试数的组合截止时间限制。`SubQBudget` 在进程内按 UTC 日原子预留请求数、Token 估值与费用估值（Decimal 累计）；每次重试单独预留，预算不足时客户端拒绝请求。账本未持久化，进程重启后计数归零；Token/费用为调用方估算而非服务商实账单，尚未接入调用方的确定性降级。以上均未挂入 MaiBot 消息流程，不读取真实凭据、不发真实网络请求。配置有 10 项、客户端有 20 项、schema 有 8 项离线单测；它们均不验证目标宿主的凭据注入机制或任何 Provider 兼容性。

凭据安全规划：普通 TOML 仅存环境变量名或受支持凭据仓库的引用，永不存密钥明文；未来运行时只在发请求时读取并构造 Authorization 头，不回显、不写日志/异常/诊断导出。引用缺失、为空或读取失败时拒绝请求并保持模块关闭，不切换 MaiBot 模型、不尝试别的凭据。轮换时在凭据源更新值并重启/刷新插件；撤销时先在 Provider 侧吊销旧密钥，再清除本地引用或关闭模块。凭据来源与读取机制尚未按目标 MaiBot 环境实测。

MaiBot Replyer 仍负责主聊天回复；记忆整理、生活生成等普通后台任务只有在宿主公开接口验证可用时才复用 MaiBot 已配置模型。以上均为规划，未实现前不可按可用功能配置。

## 待审候选本机查看器

只读Unix socket服务端与CLI已实现，默认关闭；不开放网页/TCP端口，不审批、不持久化，也不写入A-Memorix。
Linux隔离全134项测试通过，但尚未部署至真实MaiBot/微信。配置条件、主GID陷阱及命令见
[Unix socket查看器说明](docs/unix_socket_candidate_viewer.md#9-当前实现与本机命令2026-11-18)。

## 开发约定

1. 从 [`TASKS.md`](TASKS.md) 按 ID 推进；只有实现并有验证证据后才勾选。
2. 先验证 MaiBot/SDK 的公开接口与真实运行态，不私读宿主数据库或猜测 Hook 能力。
3. 模型调用按确定性优先、最小上下文、独立预算和安全降级设计；不得因重试绕过限流。
4. 不提交密钥、个人部署笔记、真实聊天记录或本机/服务器私有路径。
5. 参考项目仅作设计启发；遵守其许可证，不复制未授权代码、文本或数据。

## 许可证

本项目按 MIT License 发布，详见 [`LICENSE`](LICENSE)。参考项目的名称、许可证和设计摘要不代表其代码包含在本仓库中。
