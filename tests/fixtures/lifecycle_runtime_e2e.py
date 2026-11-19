"""Opt-in real Runner lifecycle tests; only modifies the sandbox copy."""
import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ['MAIBOT_INSTANCE_DIR'])
root.mkdir(parents=True, exist_ok=True)
os.chdir(root)
plugin_dir = root / 'plugins' / 'deai'
plugin_dir.mkdir(parents=True, exist_ok=True)
for source in Path('/test').iterdir():
    if source.suffix in {'.py', '.toml', '.json'}:
        shutil.copy2(source, plugin_dir / source.name)

from src.common.paths import resolve_plugin_config_path
from src.plugin_runtime.runner.runner_main import PluginRunner
from src.plugin_runtime.runner.plugin_loader import PluginLoader
from src.plugin_runtime.protocol.envelope import Envelope, MessageType

PID = 'inkichang.deai-filter'


class RpcStub:
    """No real Host IPC: track registrations and capability revocation locally."""
    def __init__(self):
        self.active = set()
        self.register_calls = 0
        self.unregister_calls = 0

    async def send_request(self, method, **kwargs):
        if method == 'plugin.register_components':
            assert kwargs['plugin_id'] not in self.active, 'duplicate registration'
            self.active.add(kwargs['plugin_id'])
            self.register_calls += 1
            return SimpleNamespace(error=None, payload={'accepted': True})
        if method == 'plugin.unregister':
            self.active.discard(kwargs['plugin_id'])
            self.unregister_calls += 1
        return SimpleNamespace(error=None, payload={})

    async def send_event(self, method, **kwargs):
        pass


runner = PluginRunner('in-memory', 'synthetic', [str(plugin_dir.parent)])
runner._loader = PluginLoader(host_version='1.4.5')
rpc = RpcStub()
runner._rpc_client = rpc
checks = []


def configure(enabled=True):
    path = resolve_plugin_config_path(plugin_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[plugin]\nenabled = ' + str(enabled).lower() + '\nconfig_version = "1.0.0"\n'
        '[deai]\nenabled = true\n'
        '[memory_capture]\nenabled = true\nmodel_task_name = "synthetic-no-call"\n'
        'allowed_targets = ["synthetic-session"]\nmax_queue = 4\n'
        '[memory_viewer]\nenabled = false\n', encoding='utf-8')


def current():
    meta = runner._loader.get_plugin(PID)
    assert meta is not None and PID in rpc.active
    assert meta.instance._capture_worker is not None
    assert not meta.instance._capture_worker.done()
    return meta


def stopped(instance, worker):
    assert worker.done(), 'worker leaked'
    assert instance._capture_worker is None and instance._capture_queue is None
    assert instance._candidate_viewer is None


async def reload(recover=False):
    return await runner._reload_plugins_by_ids(
        [PID], 'isolated_lifecycle_test', recover_missing_only=recover)


async def main():
    configure()
    metas = runner._loader.discover_and_load([str(plugin_dir.parent)])
    ready = await runner._initialize_plugins(metas)
    assert ready.loaded_plugins == [PID], ready.model_dump()
    meta = current()
    module = __import__('sys').modules[type(meta.instance).__module__]
    fresh = module.create_plugin()
    assert fresh is not meta.instance and fresh._capture_worker is None
    checks.append('create_plugin_fresh_instance')
    checks.append('on_load_starts_worker')

    first = meta.instance
    worker = first._capture_worker
    await first.on_config_update("self", runner._load_plugin_config(meta.plugin_dir), meta.version)
    stopped_worker = worker.done()
    assert stopped_worker and first._capture_worker is not worker
    assert not first._capture_worker.done()
    checks.append('config_update_replaces_worker')

    meta = current()
    old, worker = meta.instance, meta.instance._capture_worker
    configure(False)
    result = await reload()
    assert result.success and PID in result.inactive_plugins, result.model_dump()
    assert runner._loader.get_plugin(PID) is None and not rpc.active
    stopped(old, worker)
    checks.append('disable_unloads_unregisters_and_cancels')

    configure(True)
    result = await reload()
    assert result.success and PID in result.reloaded_plugins, result.model_dump()
    meta = current()
    assert meta.instance is not old
    checks.append('enable_creates_new_active_instance')

    old, worker = meta.instance, meta.instance._capture_worker
    await runner._unload_plugin(meta, reason='isolated_stop')
    stopped(old, worker)
    assert not rpc.active and runner._loader.get_plugin(PID) is None
    result = await reload(recover=True)
    assert result.success and PID in result.reloaded_plugins, result.model_dump()
    meta = current()
    calls = rpc.register_calls
    result = await reload(recover=True)
    assert result.success and not result.reloaded_plugins
    assert current() is meta and rpc.register_calls == calls
    checks.append('unload_then_recover')
    checks.append('recover_is_idempotent_for_active_plugin')

    # Raise after the actual on_load starts its worker; real Runner must clean
    # the partially initialized new instance and reactivate the previous one.
    old, worker = meta.instance, meta.instance._capture_worker
    source = plugin_dir / 'plugin.py'
    original = source.read_text(encoding='utf-8')
    source.write_text(original + '\n_real_on_load = DeAiFilterPlugin.on_load\n'
        'async def _injected_failure(self):\n'
        '    await _real_on_load(self)\n'
        '    raise RuntimeError("synthetic_on_load_failure")\n'
        'DeAiFilterPlugin.on_load = _injected_failure\n', encoding='utf-8')
    result = await reload()
    assert not result.success and PID in result.failed_plugins, result.model_dump()
    assert worker.done()
    restored = current()
    assert restored.instance is old
    live = [t for t in asyncio.all_tasks() if getattr(t.get_coro(), '__name__', '') == '_run_capture_worker' and not t.done()]
    assert live == [restored.instance._capture_worker], 'failed activation leaked worker'
    checks.append('on_load_failure_cleans_new_instance_and_rolls_back')

    source.write_text(original, encoding='utf-8')
    result = await reload()
    assert result.success and PID in result.reloaded_plugins, result.model_dump()
    meta = current()
    assert meta.instance is not old
    checks.append('fixed_source_reloads_after_failure')

    worker, old = meta.instance._capture_worker, meta.instance
    request = Envelope(request_id=1, message_type=MessageType.REQUEST,
                       method='runner.shutdown', payload={})
    response = await runner._handle_shutdown(request)
    assert response.payload['acknowledged'] and runner._shutting_down
    stopped(old, worker)
    assert not rpc.active and not runner._loader.list_plugins()
    assert not [t for t in asyncio.all_tasks() if getattr(t.get_coro(), '__name__', '') == '_run_capture_worker' and not t.done()]
    checks.append('shutdown_unregisters_and_releases_all_workers')
    print('LIFECYCLE_PASS', json.dumps({
        'checks': checks, 'register_calls': rpc.register_calls,
        'unregister_calls': rpc.unregister_calls, 'active_registrations': len(rpc.active),
        'live_workers': 0, 'host_rpc': 'stub',
        'runtime': 'real MaiBot Runner/PluginLoader',
    }, ensure_ascii=False))


asyncio.run(main())
