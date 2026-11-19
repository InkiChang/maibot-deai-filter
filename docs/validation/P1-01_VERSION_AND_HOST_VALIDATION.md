# P1-01 MaiBot / SDK 版本与宿主加载验证记录

> 状态：**部分完成，P1-01 保持未勾选**。版本目标和验证步骤已锁定；当前生产宿主的已安装版本、配置解析和插件加载尚未取得可复核的现场输出。
>
> 记录日期：2026-11-19

## 1. 版本锁定结论

| 项目 | 锁定目标 | 已核实证据 | 边界 |
|---|---|---|---|
| MaiBot | `v1.4.5` | 上游 release：<https://github.com/Mai-with-u/MaiBot/releases/tag/v1.4.5>；GitHub CLI 查询该 tag 成功 | 已核对公开上游版本，不等于当前 NAS 容器已安装该版本 |
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

## 4. 当前证据与阻塞

已验证：

- MaiBot `v1.4.5` release 存在；其源码依赖明确为 `maibot-plugin-sdk>=2.23.0`。
- SDK `v2.23.0` release 存在。
- 本仓库的 opt-in E2E 入口、fixture、Paramiko 可选依赖和严格 `known_hosts` 策略已存在。
- 历史会话搜索发现过 `E2E_PASS` 相关记录，但当前可回读内容主要是测试代码和执行计划，未取得包含完整 `loaded`、`registered`、`production_unchanged` 值的原始运行输出；因此不把它作为本轮独立验收结论。

尚未验证：

- 当前生产 `maibot-core` 镜像实际对应的 MaiBot 版本；
- 当前宿主实际安装的 `maibot-plugin-sdk` 版本；
- 生产配置解析、插件发现/注册和真实 Hook 加载日志；
- 当前宿主上的隔离 E2E 重新运行结果。

阻塞原因：本机 Paramiko 连接因目标 `[[redacted]]:1988` 不在系统 `known_hosts` 中而按设计拒绝；`ssh-keyscan` 又被本机客户端与服务端的 KEX 兼容性问题阻断。本轮没有可验证的主机指纹，因此没有绕过校验、没有读取或写入生产配置，也没有改变生产容器。

## 5. P1-01 完成条件

只有以下证据齐全后，才将 `TASKS.md` 的 P1-01 改为 `[x]`：

1. 记录宿主镜像 digest、MaiBot `1.4.5` 和实际 SDK 版本；
2. 记录脱敏的配置解析成功与插件发现/注册日志；
3. 运行 opt-in 隔离 E2E，保存 `E2E_PASS`、注册数量、Hook 结果和 `production_unchanged=true` 的脱敏证据；
4. 明确真实生产加载是否完成；若只完成隔离 E2E，P1-01 仍不可宣称生产接入完成。
