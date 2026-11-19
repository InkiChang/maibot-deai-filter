# Linux容器：待审核候选 Unix socket 只读查看器设计与验收

状态：**只读服务端与本机 CLI 已实现，Linux 隔离集成测试通过；未部署到生产 MaiBot，也未启用抽取。** 候选抽取和查看器均默认关闭。已对真实主机上的同版 MaiBot 镜像做一次性隔离权限演练（第8节）；生产容器和部署配置未修改。以下涉及组、挂载或Compose的变更步骤仍须由管理员审查后执行；不要照抄示例值。

## 1. 目标与明确不做的事

为本机管理员提供安全查看插件进程内存中待审候选的途径，解决真实抽取验收时无法检查模型输出的问题。

首版仅只读：不批准、不拒绝、不删除、不修改、不导出候选，不写入 A-Memorix，不保存聊天正文。候选仍在插件进程内存；插件/容器重启即消失。Unix socket 代码已存在，但下文命令不代表生产环境已部署。

## 2. 威胁边界与拓扑

```text
宿主机管理员 CLI
  └─ AF_UNIX socket（无 TCP 监听）
       └─ 宿主机专用目录 bind mount
            └─ MaiBot 容器内插件服务
                 └─ 进程内存中的待审候选
```

- 仅挂载单独的运行时目录，如宿主机 `/run/maibot-admin` → 容器 `/run/maibot-admin`；不要挂整个 `/run`、Docker socket、插件数据目录或宿主敏感路径。
- 目录须位于本机文件系统，不能放 NFS/SMB 等不保证 Unix socket 和权限语义的共享目录。
- 优先由 MaiBot 容器内插件服务创建 socket；宿主机管理员 CLI 连接。socket 不得暴露 TCP/HTTP 端口。
- 容器使用只读根文件系统时，仅对专用 socket 目录提供可写挂载；不能因此将其余根文件系统改为可写。
- 容器跨宿主机迁移、非 Linux 容器、用户命名空间或 rootless Docker 均须单独验证 UID/GID 映射。

## 3. 身份和权限建议

使用专用管理员组（示例名 `maibot-admin`），不要复用 `docker` 组；拥有 Docker socket 权限通常等同宿主机 root，不应作为本查看器的授权方式。

建议的对象属性：

| 对象 | Owner:Group | Mode | 说明 |
|---|---|---:|---|
| 宿主机运行目录（容器创建socket、宿主管理员连接） | `root:maibot-admin` | `2710`（需按宿主路径策略复核） | 容器root（直通映射）可创建socket；管理员组仅能穿越到已知路径，不能列目录或创建/删除目录项；其他用户无权限 |
| 容器内运行目录（示意） | `root:maibot-admin` | `2710`（容器身份/映射实测后确定） | 容器内root可创建socket；管理员组仅可穿越；其他用户无权限 |
| socket 文件 | 容器服务UID:管理员组 | `0660` | 服务UID与管理员组可连接；其他用户无权连接 |

宿主与容器的目录属主/属组映射可能不同；不要盲目用宿主机数字UID/GID覆盖容器文件。先核对映射，再选定两侧能够实际访问的UID/GID。服务端 bind 后必须显式 `chmod(socket, 0o660)` 并核验属主/属组；依赖 `umask` 单独控制权限不够可靠。

连接时再做第二层授权：Linux `SO_PEERCRED` 取得 peer 的 PID/UID/GID，仅允许明确配置的管理员 UID/GID。无法读取凭据、凭据不符、协议错误时一律拒绝。容器 user namespace 下 peercred 显示值必须在目标部署实测，不能从宿主 UID 直接推断。

> `0710`/`2710` 是建议起点而不是通用保证。ACL、user namespace、rootless模式和挂载实现会改变实际访问结果，最终以宿主机 `stat/getfacl` 与授权/未授权连接测试为准。不要以 `0777` 作为排障手段。

## 4. 最小协议

实现使用 AF_UNIX `SOCK_STREAM`，UTF-8、每行一个 JSON 请求/响应；服务端限制单请求 8 KiB、响应 64 KiB、读取超时 3 秒、最多 2 个并发连接、每连接最多 10 次请求。超过限制、超时、非法JSON或未知字段会拒绝并关闭连接；超限、超时和身份拒绝可能直接断开而不返回JSON错误。

允许的请求仅有：

```json
{"version":1,"op":"list_pending","limit":20,"cursor":null}
{"version":1,"op":"get_pending","candidate_id":"不透明候选ID"}
```

- `list_pending`：最多返回20个待审项的 `candidate_id`、类别、创建时间、置信度、脱敏来源摘要；不得返回批准/隔离项。
- `get_pending`：返回指定且仍为待审项的事实、单条原文证据引句、消息ID的脱敏形式；必须重新检查待审状态，禁止按任意用户输入查询其它scope。
- 响应不包含 scope 字段、原始消息ID、内部候选ID或任意对象序列化；列表也不含事实/引句。详情有意返回候选事实和单条原文引句，短消息的引句可能等于完整测试消息；这不是全文聊天接口，也不能保证任意文本中绝无秘密或路径。标记为 sensitive 的候选被隐藏，但上游敏感检测并非完备DLP，验收只用非敏感合成文本。来源和候选ID使用带随机密钥的HMAC，查看器重建后失效，不把指纹当授权凭据。
- 不提供 `approve`、`reject`、`delete`、`export`、任意SQL/文件读取等操作。未知操作/字段使用统一错误响应，不泄露堆栈。
- 只允许本机 peer UID/GID；socket 目录之外不接受连接。协议中的 `candidate_id` 不是身份验证机制。

## 5. 生命周期与实现要求

- 目录必须是预期的真实目录，不能是符号链接；创建/检查每个父路径，避免路径被替换或 symlink 攻击。
- 启动时同名路径只要存在就拒绝，包括失效socket；不探测后自动删除。管理员必须先确认没有运行实例、核对类型/属主后才能手工清理。普通文件、符号链接一律不删除。
- bind后设置并核验 mode、UID/GID；若任一步骤失败则关闭socket并停止服务，不能回退成宽松权限。
- 卸载、配置关闭、进程退出时停止accept、关闭连接并仅清理本进程创建的socket；不影响 MaiBot 主消息处理。查看器服务不可用时，消息Hook不应等待IPC。
- 候选读取要加锁/快照，避免与后台抽取并发修改造成半条记录；服务端只访问待审内存存储的窄接口，不向外暴露Store对象。
- 首版不生成逐请求访问审计日志，不记录原始请求、事实或引句；仅插件启动失败有通用运维警告。若未来增加审计，只可记时间、授权peer UID、操作和不透明候选ID。

## 6. 部署前只读检查（真实主机执行）

先从宿主确认上下文；这些命令只读，不会重启或修改容器：

```bash
# 列出现有容器；根据实际名称选择 MaiBot 容器
sudo docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'

# 记录容器安全相关配置。替换 <container>；输出可能含环境变量秘密，勿公开粘贴完整 inspect。
sudo docker inspect --format 'user={{json .Config.User}} readonly={{.HostConfig.ReadonlyRootfs}} userns={{.HostConfig.UsernsMode}} privileged={{.HostConfig.Privileged}}' <container>
sudo docker inspect --format '{{json .Mounts}}' <container>

# 确认宿主Linux、Docker客户端/服务端和组信息
uname -a
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
id
getent group maibot-admin || true
```

检查挂载中没有 `/var/run/docker.sock` 暴露给 MaiBot；若容器为 privileged、挂载宿主敏感目录、以 root 运行或使用不明 user namespace，先记录并完成风险评估，不要直接创建查看socket。

## 7. 容器权限实测步骤（代码已实现，仍须在目标容器单独验收）

### A. 准备专用目录和组

由管理员按本机策略创建专用组和目录。先确认组名未被占用，再操作；不要在未确认UID/GID映射前递归 `chown` 容器数据目录。

```bash
# 示例，需管理员审查后按实际账号/GID替换
sudo groupadd --system maibot-admin
sudo install -d -o root -g maibot-admin -m 0710 /run/maibot-admin
```

创建容器侧挂载目录需在 Compose/部署配置中单独增加该路径，宿主 source 固定为 `/run/maibot-admin`，container target 固定为同路径；只给该目录写权限。修改前保存部署文件并确认重建/更新影响，**不要在本次验收中盲目重建生产容器**。

授权某管理员的建议做法是加入专用组并重新登录：

```bash
sudo usermod -aG maibot-admin <admin-user>
# 退出并重新登录后核对
id <admin-user>
```

### B. 检查实际挂载与权限

```bash
# 宿主机：检查目录类型/属主/权限/ACL
stat -c '%F %a %U:%G %n' /run/maibot-admin
getfacl -cp /run/maibot-admin

# 容器内：检查映射视图和运行账号（替换实际容器名）
sudo docker exec <container> sh -lc 'id; stat -c "%F %a %u:%g %n" /run/maibot-admin; test -d /run/maibot-admin && test -w /run/maibot-admin'
```

预期：容器插件账号能在专用目录创建socket；未授权账号不能。宿主和容器显示的数字UID/GID可不同，须结合 userns 配置解释。目录不应有其他不必要ACL。

### C. 验证 socket 自身权限

服务实现后由插件启动socket，然后：

```bash
# 宿主机检查；预期 Type=socket、mode=660、group=maibot-admin
stat -c '%F %a %U:%G %n' /run/maibot-admin/candidates.sock
getfacl -cp /run/maibot-admin/candidates.sock

# 确认不是 TCP 监听；无 ss 输出应视为没有TCP监听
sudo ss -ltnp
```

对照插件日志中只含启动成功/权限信息，不应输出候选正文。若路径不是socket、权限不匹配或出现TCP监听，立即停止服务并回滚。

### D. 授权与拒绝测试（关键）

实现CLI后，用同一只读 `list_pending` 请求分别测试：

```bash
# 授权管理员：应成功（客户端命令取决于最终实现）
sudo -u <admin-user> <candidate-cli> list

# 未授权普通用户：应以权限拒绝失败，不能返回任何候选
sudo -u <ordinary-user> <candidate-cli> list

# 容器内非授权UID：应连接失败/认证拒绝
sudo docker exec --user <unauthorized-uid>:<unauthorized-gid> <container> <container-test-client> list
```

必须同时验证文件权限拒绝与服务端 `SO_PEERCRED` 拒绝；只测某一层不足。记录每种身份的实际UID/GID和结果，不记录候选内容。若容器中没有合适的非授权工具/账号，应在临时测试镜像或独立测试容器完成，不能为此给生产容器增加调试工具或放宽权限。

### E. 协议与生命周期测试

- [ ] 授权管理员只能列出待审项并查看单项；试送 `approve`、`delete`、未知字段，均被拒绝且数据未改变。
- [ ] 以非授权用户连接、伪造 candidate_id、请求其它scope，均拒绝。
- [ ] 超长/非法JSON、未知版本、未知操作、半包、超时连接不使服务卡死；连接并发和请求次数达到上限时被拒绝。
- [ ] 放入合成敏感文本，确认日志、list响应和错误响应不泄露原文；detail只向授权peer返回，测试完成清除合成候选。
- [ ] 用普通文件或符号链接占用socket路径，启动必须安全失败且不能删除占位文件。
- [ ] 已有活服务占socket时第二实例启动失败；停止服务后，只有经确认的陈旧socket才清理。
- [ ] 插件卸载/容器停止后socket关闭；重启后没有遗留候选；socket目录中没有残留普通文件。
- [ ] 模拟socket服务不可用时，MaiBot微信消息收发仍正常，不因查看器故障阻塞Hook。

## 8. 已完成的实机权限演练（2026-11-18）

### 生产容器只读检查

在飞牛NAS宿主机经SSH只读检查 `maibot-core`：镜像 `sengokucola/maibot:v1.4.5`；容器以root运行、未启用userns、根文件系统非只读、非privileged；没有挂载Docker socket。容器UID/GID映射显示为直通映射（0→0）；宿主测试账号 `inkicheng` 为UID 1000、主GID 1001，另有补充组。本机项目 `config.toml` 为 `enabled=false`、模型任务空、白名单空；本次未能从容器内确认生产实例实际加载的插件配置路径，因此不据此断言生产实例配置值。没有改动或重启生产容器。

### 隔离一次性容器测试

使用同版MaiBot镜像启动临时容器；网络关闭、根文件系统只读、移除Linux capabilities、启用 `no-new-privileges`，只挂载临时目录。按宿主实际组映射将临时目录设为 `root:1001`、mode `2710`；容器内服务创建的socket为 `root:1001`、mode `0660`。

- 宿主授权测试用户UID 1000 / GID 1001：成功连接。
- 隔离容器内UID/GID 65534：连接因 `PermissionError: [Errno 13] Permission denied` 被拒绝。
- 容器侧 `SO_PEERCRED` 读取到UID 1000 / GID 1001；PID显示0，因为测试容器处在独立PID namespace，不可把该PID值当宿主PID使用。
- 临时容器和临时目录均已清理；未留下测试容器。生产MaiBot持续运行，抽取仍关闭。

**边界：** 这是同版镜像中的底层Unix socket文件权限和凭据读取演练，不是候选查看器实现，也没有运行实际候选协议、真实SO_PEERCRED白名单授权、插件卸载/重启生命周期或真实MaiBot挂载目录测试。容器内65534被拒绝证明文件系统权限拒绝；不能替代对服务端身份授权逻辑的测试。Host组权限结论适用于这次的UID/GID配置，部署改变后必须重新测试。

## 9. 回滚

1. 在MaiBot配置中保持 `[memory_capture].enabled = false`，并将 `allowed_targets = []`；热更新或按既有流程停止插件。
2. 停止只读查看器，确认socket不再监听。
3. 仅删除由本次服务创建、路径正确且 `lstat` 确认为socket的 `/run/maibot-admin/candidates.sock`；不得对未知路径做递归删除。
4. 若添加过专用挂载，先停止/按维护流程更新容器，再移除该单独挂载；不能影响其它数据卷。移除临时管理员组成员/测试账号。
5. 确认 `enabled=false`、白名单为空、MaiBot正常收发、其它挂载与插件未改变。记录脱敏错误和版本。

## 10. 验收结果模板

- [ ] 真实MaiBot镜像/版本和容器名已记录（不公开秘密）
- [ ] Docker/rootless/userns/只读根文件系统情况已确认
- [ ] 宿主管理员授权连接成功
- [ ] 普通宿主用户拒绝
- [ ] 容器内未授权UID/GID拒绝
- [ ] `SO_PEERCRED` 返回身份与预期一致
- [ ] 权限精确、无TCP监听、协议只读且日志不泄露正文
- [ ] 停止/卸载/重启/回滚测试完成
- [ ] 候选抽取仍关闭；待实际查看功能和单微信验收清单完成后另行决定是否启用

结论：通过 / 部分通过 / 未通过：________

未测项及原因：____________________________________________________


## 9. 当前实现与本机命令（2026-11-18）

### 启动条件和配置

服务端随插件生命周期运行；不在消息Hook中等待IPC。需要同时满足：
`memory_capture.enabled=true`、有效非空模型任务名/会话白名单、抽取worker已启动、
`memory_viewer.enabled=true`、至少一个明确允许的peer UID/主GID。任何一项不满足都不启动。
查看范围直接继承服务端的 `memory_capture.allowed_targets`，客户端不能传 owner/session。
查看器启动失败保持关闭，不让该错误中断消息插件加载。

仓库示例保持关闭：

```toml
[memory_viewer]
enabled = false
socket_path = "/run/maibot-admin/candidates.sock"
allowed_uids = []
allowed_gids = []
socket_gid = 0
```

启用时将 `socket_gid` 换为实测管理员组GID，UID/GID白名单换为实际数值。
服务进程必须拥有socket父目录；父目录禁止其他用户访问、禁止组/其他用户写入，
所有祖先必须是真实目录且由root或服务UID控制（只允许root sticky目录作非直接祖先）。
目录必须预先创建，不会自动建目录或修改挂载。`socket_gid`只设socket属组，不授予peer访问资格。

**GID陷阱：** `SO_PEERCRED` 的GID是peer主GID，不是补充组列表。
仅执行 `usermod -aG` 并不一定通过第二层认证：可以显式允许管理员UID，或用
`sg maibot-admin -c 'python3 ...'` 使本次命令使用该主组。UID与主GID白名单是OR关系。
不隐含允许root；即便能穿透文件权限，peer仍要匹配白名单。

### 本机CLI

在包含这三个文件的可信目录运行：`candidate_viewer_cli.py`、`candidate_viewer.py`、`memory_store.py`。
宿主需要Python 3.11+；无需模型SDK或网络请求。

```bash
python3 candidate_viewer_cli.py --socket /run/maibot-admin/candidates.sock list --limit 20
python3 candidate_viewer_cli.py --socket /run/maibot-admin/candidates.sock get <上一步candidate_id>
```

输出UTF-8 JSON：列表只含元数据；`get`才含事实和一条证据引句。不要重定向到公共日志。
退出码：0成功；1服务端拒绝/未找到；2连接、超时或协议失败（命令参数错误也为2）。
首版只列最近最多20项，无分页，`cursor`仅允许null；不会保存候选或执行审批。
列表后候选若被批准、隔离、删除或过期，详情返回 `not_found`。

### 验证记录和未验证边界

- Windows / Python 3.11：134项发现，124项通过、10项Linux专测按平台跳过。
- Linux / Python 3.14.4：隔离临时目录运行全134项，全部通过；测试副本随后已删除。
- 本次新增20项测试：Store/协议/CLI错误/生命周期10项，以及真实AF_UNIX集成10项。
- 真实socket测试覆盖0660、SO_PEERCRED拒绝、CLI子进程list/get、同名路径拒绝、
  符号链接目录拒绝、socket替换不误删、bind后权限失败清理、2连接上限、10请求上限、
  请求大小/超时/非法JSON、响应上限和卸载关闭连接。
- 凭据单测验证UID及主GID授权；本次socket集成使用同一测试账号，未增加系统用户/组。
  跨用户文件权限和容器映射已有第8节独立演练，但不能等同于新服务已在生产容器验收。
- 未部署/重启生产MaiBot，未改容器挂载、用户/组或生产配置；未发送真实微信消息，
  未调用真实抽取模型、未验证抽取质量；未接入A-Memorix。


## 10. NAS同版镜像隔离验收（2026-11-18）

服务端已在NAS上使用生产同一镜像ID单独启动，网络禁用、根文件系统只读，未挂生产数据。全134项测试通过；宿主CLI list/get、跨UID/主GID的文件权限与SO_PEERCRED双层授权、SIGTERM清理及冷启动重绑均实测通过。临时容器/目录已删除，生产前后状态与排序后的配置结构摘要一致。

详见 [NAS验收报告](nas_viewer_acceptance.md)。这是合成数据与实际socket验收，不是生产插件Runner、微信Hook或真实模型抽取验收；不改变P5-04整体未完成状态。
