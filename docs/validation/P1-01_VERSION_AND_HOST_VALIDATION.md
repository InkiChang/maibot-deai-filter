# P1-01 MaiBot / SDK 版本与宿主加载验证记录

> 状态：**P1-01 完成（版本、配置解析、生产发现/注册与 Hook 加载）**。已取得当前容器启动批次的持久化日志；真实聊天 Hook 执行效果及部署源码与仓库版本一致性不在本项完成声明内。
>
> 记录日期：2026-11-19

## 1. 版本锁定结论

| 项目 | 锁定目标 | 已核实证据 | 边界 |
|---|---|---|---|
| MaiBot | `v1.4.5` | 上游 release：<https://github.com/Mai-with-u/MaiBot/releases/tag/v1.4.5>；GitHub CLI 查询该 tag 成功；生产容器现场核验见第 4 节 | 上游版本与生产现场证据分别记录，不能仅凭 tag 推断宿主状态 |
| MaiBot SDK | `v2.23.0` 作为本轮验证基线 | SDK release：<https://github.com/Mai-with-u/maibot-plugin-sdk/releases/tag/v2.23.0>；MaiBot `v1.4.5` 的 `pyproject.toml` 与 `requirements.txt` 均声明 `maibot-plugin-sdk>=2.23.0` | 这是上游下限约束，不证明当前宿主实际安装版本；验证环境应固定 `==2.23.0` 后再测 |
| 插件兼容性声明 | `_manifest.json` 的 SDK `min_version` 为 `2.20.0` | 仓库文件已读回 | 这是插件声明的兼容下限，不替代本轮目标 SDK `2.23.0` |

本轮目标组合为：**MaiBot `1.4.5` + `maibot-plugin-sdk==2.23.0`**。若宿主实际使用更高 SDK，需另做兼容性验证，不能由 `>=2.23.0` 自动推断。

## 2. 安装与配置解析步骤

以下是待在受控宿主或隔离容器执行的步骤；规划不等于已完成验收：

1. 记录宿主镜像 digest、MaiBot 版本和实际 SDK 版本：
   `python -c "import importlib.metadata as m; print(m.version('maibot-plugin-sdk'))"`
2. 在独立验证环境安装目标依赖：
   `pip install 'maibot-plugin-sdk==2.23.0'`
   或使用宿主既有锁文件，但必须记录实际解析结果。
3. 将 `plugin.py`、`deai.py`、`_manifest.json` 和 `config.toml` 放在同一插件根目录；不覆盖宿主其他插件或配置。
4. 用 `tomllib` 解析 `config.toml`、用 `json` 解析 `_manifest.json`，确认格式有效，并核对插件元数据的 SDK 范围。
5. 通过 MaiBot 插件管理或启动日志确认：插件被发现、manifest 被接受、配置被解析、插件注册完成；只记录脱敏日志行，不提交配置密钥或完整聊天内容。

## 3. 插件加载与隔离 Hook 验证步骤

仓库已有显式 opt-in 的 `tests/test_hook_runtime_e2e.py` 和 `tests/fixtures/hook_runtime_e2e.py`。运行前必须配置已确认指纹的 SSH 主机和匹配目标 MaiBot 的镜像：

```bash
export MAIBOT_RUN_HOOK_E2E=1
export MAIBOT_E2E_SSH_HOST=<已确认指纹的Docker主机>
export MAIBOT_E2E_SSH_PORT=<SSH端口>
export MAIBOT_E2E_SSH_USER=<用户>
export MAIBOT_E2E_IMAGE=<与目标MaiBot匹配的镜像>
python -m unittest discover -s tests -p 'test_hook_runtime_e2e.py' -v
```

验收点：

- SSH 使用系统 `known_hosts` 严格拒绝未知主机，不接受自动信任；
- 测试只创建临时隔离容器：`--network none`、只读根文件系统、`--cap-drop ALL`、`no-new-privileges`，并使用只读源码挂载；
- fixture 完成 Runner/Registry/HookDispatcher 注册与一次 Hook 分发，标准输出包含 `E2E_PASS`；
- 临时容器和远端目录在 `finally` 中清理；生产 `maibot-core` 状态快照前后一致；
- 真实生产加载仍需另外核对宿主日志和启停行为，隔离 E2E 不能替代生产验收。

README 已记录同一套环境变量和安全边界；普通 unittest/CI 默认不会发起 SSH 连接。

## 4. 当前证据与残差

已验证（2026-11-19，生产只读探测）：

- `[[redacted]]:1988` 的实时 ED25519 指纹为 `[redacted]`，与本机 `known_hosts` 完全匹配；Paramiko 使用 `RejectPolicy` 严格认证成功。
- 生产容器 `maibot-core` 当前为 `running/healthy`，重启次数为 `0`；镜像 ID 为 `sha256:7d1474653b86216655194c34d8ac880493a73e2160e1cab0c2afd7a7975b2a5b`。
- 容器内 `/MaiMBot/.venv` 的 Python 为 `3.13.16`，实际安装 `maibot-plugin-sdk==2.23.0`；生产镜像/配置目标为 MaiBot `v1.4.5`。
- 已部署插件根目录包含 `plugin.py`、`deai.py`、`_manifest.json`、`config.toml`；manifest 和 TOML 配置解析成功，Python 语法检查成功。
- 针对该 NAS 和 `sengokucola/maibot:v1.4.5` 的 opt-in 隔离 E2E 已重新运行：`1` 个测试通过，耗时约 `22.034s`；测试断言包含 Runner/Registry/HookDispatcher 注册、Hook 分发、隔离限制、清理和生产容器状态前后一致。运行后未残留 `mai-hook-e2e` 容器。

补充取得生产注册证据（2026-11-19，只读）：

- 此前只检索 Docker 最近 stdout，遗漏实例历史 JSONL。使用实例日志目录 `/MaiMBot/bots/<bot_id>/logs/`，逐行解析 `event` 并精确匹配插件 ID 后，确认并非当前注册失败。
- 当前容器启动于 `2026-11-17T05:58:06.17470796Z`。同批次 `[redacted]` 第 343、350、360、361 行分别记录加载成功、PluginContext 注入、注册完成和 `DeAI Filter 0.1.0 已加载（maisaka.reply.after_split）`，发生于 `05:58:31Z`。
- Runner 日志 `[redacted]` 显示 import、create、config、bootstrap、register、on_load 阶段均 `outcome=completed`。第 156 行为注册完成，第 159 行为 Hook 加载提示。
- 脱敏原始事件与文件行号保存在 [`P1-01_PRODUCTION_REGISTRATION_EVIDENCE.json`](P1-01_PRODUCTION_REGISTRATION_EVIDENCE.json)。日志中不带时区的 `11-17 13:58:31` 为原始时间显示，不与带 `Z` 的 UTC 时间混淆。
- 更早的 13:51/13:52 启动曾因 manifest 缺少 `author.url` 和 `capabilities` 失败；13:53 起已加载成功，不能把早期失败当作当前故障。本轮没有修改生产文件。

## 5. 验收结论与边界

P1-01 的版本、SDK、配置解析、隔离 E2E 以及生产发现/注册和 Hook 加载证据已齐全，清单标记为 `[x]`。

- 生产启动日志证明部署的 `0.1.0` 插件加载与注册成功，不证明真实聊天 Hook 已被调用或输出效果正确；后续 Hook 行为验证仍属于 P1-04 等任务。
- 已部署 `plugin.py` 与仓库文件哈希不同；本次不覆盖或升级生产插件，不能将生产加载记录视为仓库 HEAD 原样部署的证明。
- 本轮只读排查，没有执行生产重启、热加载或真实聊天 Hook，没有读取秘密配置内容。生产容器仍为 `running/healthy`、重启次数 `0`，启动时间及镜像未变。
