# P1-02 插件生命周期 / 启停 / 异常恢复验证

日期：2026-11-19

## 环境与安全边界

使用正式 `sengokucola/maibot:v1.4.5` 镜像运行真实 MaiBot `PluginRunner`、`PluginLoader` 和调度/卸载逻辑；仅把项目插件复制到临时 sandbox 副本。

隔离容器使用 `--network none`、只读根文件系统、丢弃全部 capabilities、`no-new-privileges`、资源限额；插件注册RPC由内存Stub模拟，不连接宿主服务。执行器验证挂载只读，并在前后比较生产 `maibot-core` 的ID、镜像、重启数、健康状态、启动时间。生产实例从未停止、重启、热加载或改配置。

## 验证场景

隔离Runner中的10项断言全部通过：

1. `create_plugin()` 产生新实例；`on_load()` 启动capture worker。
2. 配置更新停止旧worker/资源并创建新worker。
3. Runner按禁用配置重载后卸载插件、注销RPC、取消worker。
4. 再启用创建新实例并重新注册。
5. 显式卸载后 `plugin.recover` 恢复插件；对已运行插件重复recover为幂等。
6. 在临时插件副本注入 `on_load` 失败：Runner报告失败、清理部分启动的worker，并回滚到先前实例。
7. 恢复正常副本后再次reload成功。
8. Runner shutdown完成注销、卸载，确认无capture worker遗留。

注册RPC调用6次、注销6次；结束时活动注册数0、运行worker数0。预期注入的异常为 `synthetic_on_load_failure`，不是未处理的测试失败。

另行重跑既有Hook隔离E2E：注册并分发 `maisaka.reply.after_split` 成功，1段文本从“你好。”变为“你好”，无Hook错误。

## 结果与边界

两项隔离E2E均通过。生产状态前后相同：容器ID、镜像摘要和具体启动时间不公开；当时重启数为0，状态为 `running/healthy`。

P1-02按隔离真实Runner生命周期语义完成。RPC对端使用Stub，未测试生产宿主IPC断连、生产插件热启停或真实微信事件期间的竞争/恢复；这些不应由本次测试结果推断。

测试入口：`tests/test_hook_runtime_e2e.py` 中独立 opt-in 的 `MAIBOT_RUN_LIFECYCLE_E2E=1` 用例；完整离线套件 `138` 项通过，`12` 项因opt-in跳过（包含两项隔离宿主E2E）。
