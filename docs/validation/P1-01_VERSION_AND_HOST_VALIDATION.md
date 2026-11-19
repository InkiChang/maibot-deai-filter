# P1-01 MaiBot / SDK 版本与宿主加载验证记录

> 状态：**部分完成，P1-01 保持未勾选**。版本目标、生产实际版本、配置解析和隔离加载已取得可复核证据；生产插件发现/注册和真实 Hook 执行证据仍缺失。
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

仍有残差：

- 当前生产日志检索未找到 `inkichang.deai-filter` 或该插件注册的明确日志标识。因此，虽然文件存在且隔离运行成功，尚不能证明生产 MaiBot 已发现、注册并实际执行该插件。
- 本轮没有执行生产重启、热加载或真实聊天 Hook；没有读取秘密配置内容，也没有改变生产容器。

因此 P1-01 继续保持未勾选：版本、SDK、配置解析和隔离加载验证已完成，但生产插件发现/注册证据仍缺失。

## 5. P1-01 完成条件

将 `TASKS.md` 的 P1-01 改为 `[x]` 前，还需取得以下最后一项证据：

1. 从生产 MaiBot 启动或插件管理日志中取得脱敏的插件发现/注册成功记录，并确认对应 Hook 已加载；
2. 若需要热加载或重启，先取得授权窗口并记录生产容器状态前后无异常；
3. 不把仅有隔离 E2E、文件存在或配置可解析误写成生产接入完成。
