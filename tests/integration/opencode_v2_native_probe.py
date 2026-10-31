# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

"""Exercise native OpenCode 2.x delegation with a scripted local model.

The OpenCode 2.x counterpart of opencode_native_probe.py. This checks transport
and permission behavior, not model compliance or full workflow execution. The
only data sent to the mock is synthetic fixture data and the installed bundle.
"""

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request

MARKER = 'EGM_PARENT_ONLY_SENTINEL'
SECRET = 'EGM_SYNTHETIC_SECRET'
PROJECT_RULE = 'EGM_PROJECT_AGENTS_SENTINEL'
GLOBAL_RULE = 'EGM_GLOBAL_AGENTS_SENTINEL'
REVIEWER, PERSONA = 'egm-zoom-out', 'zoom_out.md'
CHILD_PROMPT = 'Read the synthetic fixture .env file.'
# One EGM skill and one skill built into OpenCode.
SKILLS = ('egm-review', 'opencode')
# Requests are classified by the installer's agent prompts, not by tools or the
# marker, because tools and the marker are what the checks inspect.
PARENT_PROMPT = 'Dispatch reviewers directly'
CHILD_SYSTEM = 'Do not write reports or delegate'
REQUESTS: list[dict[str, Any]] = []
LOCK = threading.Lock()
CALL_IDS = itertools.count(1)


def tools_of(request: dict[str, Any]) -> set[str]:
    return {tool['function']['name'] for tool in request.get('tools', [])}


def role(request: dict[str, Any]) -> str:
    system = json.dumps([m for m in request.get('messages', []) if m.get('role') == 'system'])
    if PARENT_PROMPT in system:
        return 'parent'
    if CHILD_SYSTEM in system:
        return 'child'
    return 'other'


class MockProvider(BaseHTTPRequestHandler):
    persona = ''
    fixture = ''

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with LOCK:
            REQUESTS.append(request)
        tools = tools_of(request)
        results = sum(1 for m in request['messages'] if m.get('role') == 'tool')
        steps: list[tuple[str, dict[str, str]]] = []
        if role(request) == 'parent' and 'subagent' in tools:
            steps = [('subagent', {
                'agent': REVIEWER,
                'description': 'Probe child isolation',
                'prompt': CHILD_PROMPT,
            })]
        elif role(request) == 'child':
            steps = [('read', {'path': self.persona}), ('read', {'path': self.fixture})]
            if 'skill' in tools:
                steps += [('skill', {'id': skill}) for skill in SKILLS]
        delta = {'role': 'assistant', 'content': 'SCRIPTED_COMPLETION'}
        finish = 'stop'
        if results < len(steps):
            delta, finish = self.call(*steps[results]), 'tool_calls'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for payload in (delta, {}):
            event = {
                'id': 'synthetic',
                'object': 'chat.completion.chunk',
                'created': 1,
                'model': 'fixture',
                'choices': [{
                    'index': 0,
                    'delta': payload,
                    'finish_reason': finish if not payload else None,
                }],
            }
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')

    @staticmethod
    def call(name: str, arguments: dict[str, str]) -> dict[str, Any]:
        return {
            'role': 'assistant',
            'tool_calls': [{
                'index': 0,
                'id': f'fixture-call-{next(CALL_IDS)}',
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)},
            }],
        }


def opencode(binary: Path, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), *args, '--standalone'],
        cwd=cwd, env=env, text=True, capture_output=True, timeout=180,
    )


def effective_agents(binary: Path, cwd: Path, env: dict[str, str], output: Path) -> dict[str, Any]:
    """Read resolved agents from a started server; a cold one answers before config loads."""
    password = secrets.token_urlsafe(24)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    auth = 'Basic ' + base64.b64encode(f'opencode:{password}'.encode()).decode()

    def get(path: str) -> Any:
        request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Authorization': auth})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    with open(output / 'serve-log.txt', 'w') as log:
        server = subprocess.Popen(
            [str(binary), 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
            cwd=cwd, env={**env, 'OPENCODE_PASSWORD': password},
            stdout=log, stderr=log, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if any(agent.get('id') == 'egm' for agent in get('/api/agent')['data']):
                        break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    pass
                time.sleep(0.5)
            else:
                raise TimeoutError('Agents never loaded on the started server')
            return {agent: get(f'/api/agent/{agent}')['data'] for agent in ('egm', REVIEWER)}
        finally:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=15)


def tool_parts(export: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [
        part['state']
        for message in export['messages'] if message['type'] == 'assistant'
        for part in message.get('content', [])
        if part.get('type') == 'tool' and part.get('name') == name
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--opencode', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, binary = args.source.resolve(), args.opencode.resolve()
    version = subprocess.check_output([str(binary), '--version'], text=True).strip()
    version = version.split()[-1].removeprefix('v')
    assert version.startswith('2.'), f'Unexpected OpenCode version: {version}'
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    status = subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], text=True,
    )
    assert not status, 'Source contains staged, unstaged, or untracked changes'
    args.output.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='egm-v2-native-probe-') as root:
            root = Path(root).resolve()
            project, home, install = root / 'project', root / 'home', root / 'install'
            project.mkdir()
            (project / '.env').write_text(SECRET + '\n')
            (project / 'AGENTS.md').write_text(f'Project rule: {PROJECT_RULE}\n')
            global_config = home / 'config' / 'opencode'
            global_config.mkdir(parents=True)
            (global_config / 'AGENTS.md').write_text(f'Global rule: {GLOBAL_RULE}\n')
            # The installer's configuration stays unchanged in OPENCODE_CONFIG;
            # the test-only provider is a separate global document in the 2.x format.
            (global_config / 'opencode.json').write_text(json.dumps({
                '$schema': 'https://opencode.ai/config.json',
                'model': 'fixture/fixture',
                'providers': {'fixture': {
                    'name': 'fixture',
                    'package': '@opencode/ai/providers/openai-compatible',
                    'settings': {
                        'baseURL': f'http://127.0.0.1:{server.server_port}/v1',
                        'apiKey': 'fixture',
                    },
                    'models': {'fixture': {'name': 'fixture'}},
                }},
            }, indent=2) + '\n')
            subprocess.run(
                ['python3', '-I', str(source / 'adapters/opencode/install.py'), str(install)],
                check=True, stdout=subprocess.DEVNULL, timeout=60,
            )
            persona = install / 'bundle' / 'agents' / PERSONA
            MockProvider.persona, MockProvider.fixture = str(persona), str(project / '.env')
            shutil.copy2(install / 'opencode.json', args.output / 'candidate-opencode.json')
            shutil.copy2(global_config / 'opencode.json', args.output / 'test-provider-opencode.json')
            env = {
                'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'HOME': str(home),
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_CACHE_HOME': str(home / 'cache'),
                'XDG_STATE_HOME': str(home / 'state'),
                'OPENCODE_CONFIG': str(install / 'opencode.json'),
            }
            command = [
                'run', '--format', 'json', '--model', 'fixture/fixture', '--agent', 'egm',
                f'Parent-only marker: {MARKER}. Delegate without it.',
            ]
            result = opencode(binary, *command, cwd=project, env=env)
            (args.output / 'trace.jsonl').write_text(result.stdout)
            (args.output / 'stderr.txt').write_text(result.stderr)
            (args.output / 'provider-requests.json').write_text(json.dumps(REQUESTS, indent=2) + '\n')
            assert result.returncode == 0, f'OpenCode exited {result.returncode}; inspect stderr.txt'
            trace = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            calls = [e for e in trace if e.get('type') == 'tool_use' and e['part'].get('tool') == 'subagent']
            assert len(calls) == 1, f'Expected one native subagent call, saw {len(calls)}'
            parent_id, call = calls[0]['sessionID'], calls[0]['part']['state']
            child_id = call['metadata']['metadata']['sessionID']
            exports = {}
            for label, session in (('parent', parent_id), ('child', child_id)):
                exported = opencode(binary, 'session', 'export', session, cwd=project, env=env)
                assert exported.returncode == 0, f'Could not export the {label} session'
                (args.output / f'session-{label}.json').write_text(exported.stdout)
                exports[label] = json.loads(exported.stdout)
            agents = effective_agents(binary, project, env, args.output)
            (args.output / 'effective-agents.json').write_text(json.dumps(agents, indent=2) + '\n')

            parents = [r for r in REQUESTS if role(r) == 'parent']
            children = [r for r in REQUESTS if role(r) == 'child']
            child_tools = sorted(set().union(*map(tools_of, children)))
            reads = {state['input']['path']: state for state in tool_parts(exports['child'], 'read')}
            persona_read = reads.get(str(persona), {})
            env_read = reads.get(str(project / '.env'), {})
            skill_calls = {
                state['input']['id']: state.get('error') or state['status']
                for state in tool_parts(exports['child'], 'skill')
            }
            child = exports['child']['info']
            checks = {
                'native_subagent_completed': call['status'] == 'completed',
                'marker_absent_from_subagent_input': MARKER not in json.dumps(call['input']),
                'distinct_child_session': child_id != parent_id
                and child.get('parentID') == parent_id and child.get('agent') == REVIEWER,
                'marker_reaches_parent_requests': any(MARKER in json.dumps(r) for r in parents),
                'child_requests_observed': bool(children),
                'child_requests_carry_task_prompt': bool(children)
                and all(CHILD_PROMPT in json.dumps(r) for r in children),
                'marker_absent_from_child_requests': all(MARKER not in json.dumps(r) for r in children),
                'child_tools_read_glob_grep_only': child_tools == ['glob', 'grep', 'read'],
                'secret_absent_from_all_requests': all(SECRET not in json.dumps(r) for r in REQUESTS),
                'env_read_denied_natively': env_read.get('status') == 'error'
                and env_read.get('error', {}).get('type') == 'permission.rejected',
                'persona_read_completed': persona_read.get('status') == 'completed'
                and f'name: {REVIEWER}' in json.dumps(persona_read.get('content')),
                'child_skill_calls_denied': all(
                    isinstance(outcome, dict) and outcome.get('type') == 'permission.rejected'
                    for outcome in skill_calls.values()
                ),
            }
            summary = {
                'head': sha,
                'opencode_version': version,
                'model': 'scripted local mock',
                'command': ['opencode', *command, '--standalone'],
                'checks': checks,
                'observations': {
                    'subagent_input': call['input'],
                    'child_tools': child_tools,
                    'env_read_error': env_read.get('error'),
                    'child_skill_calls': skill_calls,
                    'project_agents_md_in_child': any(PROJECT_RULE in json.dumps(r) for r in children),
                    'global_agents_md_in_child': any(GLOBAL_RULE in json.dumps(r) for r in children),
                },
                'scope': (
                    'One scripted delegation, two reads and any skill calls; not full '
                    'workflow, model behavior, or general isolation'
                ),
            }
            (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
            print(json.dumps(summary, indent=2))
            if not all(checks.values()):
                sys.exit(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


if __name__ == '__main__':
    main()
