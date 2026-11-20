# 2026-11-20 小范围审核修复

本轮不连接生产、不运行远端 E2E、不部署插件、不发送真实消息。只使用合成输入和离线测试。

## 行为变更

- 代码保护采用保守整轮旁路：任一段含反引号或波浪线代码围栏时，回复 Hook 原样继续，不清理、裁剪或去重；纯文本函数也保留含这些标记的输入。这样保护跨段围栏和缩进，代价是同一回复中的自然语言也不清理。不保证识别未标记代码。此行为替代 README 中原有的“代码围栏去掉”描述；README 本轮未重写。
- 带引句候选重复合并时保留全部证据引用；同一引用的引句保留，多引用合并清除无映射关系的引句，不扩大原文保留量。冲突隔离清除引句但保留证据引用，避免违反只允许待审项保留引句的约束。
- SubQ 异步凭据解析受剩余总时限限制，超时取消并返回脱敏错误；外部取消继续传播。同步 resolver 必须非阻塞，异步 resolver 必须配合取消；这不是可强杀任意阻塞 Python 代码的机制。
- 任一日预算限额为零即拒绝，即使调用方提供零估值；拒绝发生在凭据解析和传输之前。
- 远端 E2E 公开证据只记录 fixture 是否通过及生产状态是否一致，不导出状态原值或远端 stdout/stderr。公开测试边界截断异常链，避免第三方 SSH 异常把私密信息写进 unittest 输出。本轮仅用 mock 验证输出边界，不代表远端 E2E 已运行。

## 任务证据纠正

P1-04 恢复未勾选。公开源码和自写合成 Dispatcher 的断言不等于真实宿主运行验证；需要真实 Dispatcher、多处理器及回复调用方字段校验的隔离测试。P1-03 和真实微信验收状态不变。

P5-04v 的旧报告属于仓库外私密材料，移除公开任务清单中的失效链接；不把历史现场结论写成当前重新验收。

## 回归入口

`tests/test_audit_regressions.py` 覆盖代码整轮保护、带引句合并与冲突隔离、捕获队列重复事实接线、独立零额度、凭据解析超时和取消、E2E 失败输出脱敏。原过滤器脚本保留原回归组，仅把代码围栏的预期改为原样保留。

新克隆完整测试需要先构建发行包，不能直接将缺少发行包导致的两个失败当作业务回归：

```bash
python -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python -m build --outdir dist
.venv/bin/python tests/test_plugin.py
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

上述是 Linux 命令；远端 E2E opt-in 环境变量应保持未设置。发行目录已有多版本包时，先人工检查并移出旧包，不能盲目删除。

## 本轮实际结果

本机 Python 3.12.13。默认 PyPI 构建依赖下载发生 TLS EOF，未关闭证书校验；随后通过 HTTPS 镜像将 setuptools 84.2.1 和 wheel 0.48.0 安装到本地虚拟环境，执行 `.venv/bin/python -m build --no-isolation --outdir dist`，sdist/wheel 构建成功。

新增 10 项合成回归通过；完整 unittest 153 项，其中 151 项通过、2 项远端 opt-in E2E 跳过。过滤器脚本 CASE1–9 通过，`git diff --check` 通过。成功证据导出与失败 traceback 的隐私测试均使用内存 fake SSH 客户端，不连接 NAS。
