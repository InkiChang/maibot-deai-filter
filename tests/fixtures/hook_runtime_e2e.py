import asyncio, json, os, shutil
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ['MAIBOT_INSTANCE_DIR'])
root.mkdir(parents=True, exist_ok=True)
os.chdir(root)
plugin_dir = root / 'plugins' / 'deai'
plugin_dir.mkdir(parents=True, exist_ok=True)
for f in Path('/test').iterdir():
    if f.suffix in {'.py', '.toml', '.json'} and f.name != 'hook_runtime_e2e.py':
        shutil.copy2(f, plugin_dir / f.name)

from src.plugin_runtime.runner.runner_main import PluginRunner
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_dispatcher import HookDispatcher
from src.plugin_runtime.host.hook_spec_registry import HookSpec, HookSpecRegistry
import src.plugin_runtime.host.hook_dispatcher as dispatcher_module
from src.plugin_runtime.protocol.envelope import Envelope, MessageType

dispatcher_module.is_shutdown_requested = lambda: False

class RpcStub:
    def __init__(self):
        self.calls = []

    async def send_request(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == 'plugin.register_components':
            return SimpleNamespace(error=None, payload={'accepted': True})
        return SimpleNamespace(error=None, payload={})

    async def send_event(self, method, **kwargs):
        self.calls.append((method, kwargs))

runner = PluginRunner('in-memory', 'synthetic', [str(plugin_dir.parent)])
from src.plugin_runtime.runner.plugin_loader import PluginLoader
runner._loader = PluginLoader(host_version='1.4.5')
rpc = RpcStub()
runner._rpc_client = rpc

async def main():
    metas = runner._loader.discover_and_load([str(plugin_dir.parent)])
    ready = await runner._initialize_plugins(metas)
    assert ready.loaded_plugins == ['inkichang.deai-filter'], (
        ready.model_dump(), [(m.plugin_id, m.load_error) for m in metas]
    )
    registration = next(
        kwargs['payload'] for method, kwargs in rpc.calls
        if method == 'plugin.register_components'
    )
    specs = HookSpecRegistry()
    specs.register_hook_spec(HookSpec(
        name='maisaka.reply.after_split', allow_blocking=True,
        allow_observe=True, allow_kwargs_mutation=True,
    ))
    specs.register_hook_spec(HookSpec(
        name='chat.receive.before_process', allow_blocking=True,
        allow_observe=True, allow_kwargs_mutation=True,
    ))
    registry = ComponentRegistry(specs)
    count = registry.register_plugin_components(
        'inkichang.deai-filter', registration['components']
    )
    assert count == 2, registration

    class Supervisor:
        component_registry = registry
        group_name = 'external'
        is_builtin = False

        async def invoke_plugin(self, method, plugin_id, component_name, args,
                                timeout_ms=None):
            assert method == 'plugin.invoke_hook'
            envelope = Envelope(
                request_id=1, message_type=MessageType.REQUEST, method=method,
                plugin_id=plugin_id,
                payload={'component_name': component_name, 'args': args},
            )
            response = await runner._handle_hook_invoke(envelope)
            if response.error:
                raise RuntimeError(response.error)
            return response

    dispatcher = HookDispatcher(hook_spec_registry=specs)
    result = await dispatcher.invoke_hook(
        'maisaka.reply.after_split', supervisors=[Supervisor()],
        segments=[{
            'text': '你好。', 'separator': '',
            'quote_previous': False, 'is_correction': False,
        }],
    )
    assert not result.aborted and not result.errors, result
    assert result.kwargs['segments'][0]['text'] == '你好', result.kwargs
    print('E2E_PASS', json.dumps({
        'loaded': ready.loaded_plugins, 'registered': count,
        'errors': result.errors, 'segments': result.kwargs['segments'],
    }, ensure_ascii=False))
    await dispatcher.stop()

asyncio.run(main())
