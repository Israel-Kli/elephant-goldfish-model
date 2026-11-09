# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the OpenCode V2 adapter and its guard with a scripted local model.

Each scenario installs the candidate with --interface v2 and runs the pinned
OpenCode release against a mock provider that scripts every parent and reviewer
tool call. This checks host transport, permissions and the guard plugin, not
model compliance or full workflows. Only synthetic fixture data and the
installed bundle reach the mock.
"""

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import re
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

REVIEWER, PERSONA = 'egm-zoom-out', 'zoom_out.md'
STANDARD = 'resources/document_standards/zoom-out-standard.md'
# Requests are classified by the installer's agent prompts, not by tools or markers,
# because tools and markers are what the checks inspect.
PARENT_PROMPT = 'Dispatch reviewers directly'
CHILD_SYSTEM = 'Do not write reports or delegate'
FINDINGS = 'SCRIPTED_FINDINGS'
SCENARIOS = ('guarded', 'incomplete', 'undeclared', 'unguarded', 'widened')


class MockProvider(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    parent_steps: list[str] = []
    child_steps: list[tuple[str, dict[str, Any]]] = []
    dispatch: dict[str, Any] = {}
    calls = itertools.count(1)
    lock = threading.Lock()

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.lock:
            self.requests.append(request)
        messages = request.get('messages', [])
        call = None
        if role(request) == 'parent':
            done = sum(1 for message in messages if message.get('role') == 'tool')
            if done < len(self.parent_steps):
                call = self.parent_call(self.parent_steps[done], messages)
        elif role(request) == 'child':
            last = max(i for i, message in enumerate(messages) if message.get('role') == 'user')
            done = sum(1 for message in messages[last:] if message.get('role') == 'tool')
            if done < len(self.child_steps):
                call = self.child_steps[done]
        delta: dict[str, Any] = {'role': 'assistant', 'content': FINDINGS}
        finish = 'stop'
        if call:
            delta, finish = {'role': 'assistant', 'tool_calls': [{
                'index': 0, 'id': f'fixture-call-{next(self.calls)}', 'type': 'function',
                'function': {'name': call[0], 'arguments': json.dumps(call[1])},
            }]}, 'tool_calls'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for payload in (delta, {}):
            event = {
                'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                'choices': [{'index': 0, 'delta': payload, 'finish_reason': finish if not payload else None}],
            }
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')

    def parent_call(self, step: str, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        if step == 'dispatch':
            return 'subagent', self.dispatch
        # A resume attempt: reuse the first child's session in the background.
        previous = re.findall(r'sessionID=\\?"(ses_[A-Za-z0-9]+)', json.dumps(messages))
        return 'subagent', {**self.dispatch, 'sessionID': previous[-1] if previous else 'ses_missing', 'background': True}


def role(request: dict[str, Any]) -> str:
    system = json.dumps([m for m in request.get('messages', []) if m.get('role') == 'system'])
    if PARENT_PROMPT in system:
        return 'parent'
    if CHILD_SYSTEM in system:
        return 'child'
    return 'other'


def opencode(binary: Path, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(binary), *args, '--standalone'], cwd=cwd, env=env, text=True,
                          capture_output=True, timeout=180)


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
            cwd=cwd, env={**env, 'OPENCODE_PASSWORD': password}, stdout=log, stderr=log, start_new_session=True,
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


def tool_records(export: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {'name': part.get('name'), **part.get('state', {})}
        for message in export.get('messages', []) if message.get('type') == 'assistant'
        for part in message.get('content', []) if part.get('type') == 'tool'
    ]


def outcome(record: dict[str, Any] | None) -> str:
    """Separate unavailable tools from denied calls, as #282 requires."""
    if not record:
        return 'not attempted'
    error = record.get('error') or {}
    if record.get('status') == 'completed':
        return 'completed'
    if error.get('type') == 'permission.rejected':
        return 'denied'
    if error.get('type') == 'tool.execution' and 'available' in error.get('message', ''):
        return 'unavailable'
    return f"error: {error.get('type')}"


def run(scenario: str, source: Path, binary: Path, output: Path, port: int) -> dict[str, Any]:
    output.mkdir()
    MockProvider.requests = []
    with tempfile.TemporaryDirectory(prefix=f'egm-v2-probe-{scenario}-') as root:
        root = Path(root).resolve()
        project, home, install, outside = root / 'project', root / 'home', root / 'install', root / 'outside'
        token = secrets.token_hex(8)
        markers = {key: f'EGM_{key.upper()}_{token}' for key in
                   ('parent', 'global', 'project', 'nested', 'configured', 'secret', 'outside')}
        (project / 'docs').mkdir(parents=True)
        (project / 'sub').mkdir()
        outside.mkdir()
        (project / 'AGENTS.md').write_text(f'Project rule: {markers["project"]}\n')
        (project / 'sub' / 'AGENTS.md').write_text(f'Nested rule: {markers["nested"]}\n')
        (project / 'sub' / 'notes.md').write_text('Nested notes.\n')
        (project / 'docs' / 'target.md').write_text('# Target\n\nA design to review.\n')
        (project / '.env').write_text(markers['secret'] + '\n')
        (project / 'alias.md').symlink_to(project / '.env')
        (outside / 'private.md').write_text(markers['outside'] + '\n')
        (project / 'reference.md').symlink_to(outside / 'private.md')
        config_dir = home / 'config' / 'opencode'
        config_dir.mkdir(parents=True)
        (config_dir / 'AGENTS.md').write_text(f'Global rule: {markers["global"]}\n')
        (root / 'configured.md').write_text(f'Configured instruction: {markers["configured"]}\n')
        if scenario == 'widened':
            (project / 'opencode.json').write_text(json.dumps({'agents': {REVIEWER: {'permissions': [
                {'action': '*', 'resource': '*', 'effect': 'allow'}]}}}))
        subprocess.run(['python3', '-I', str(source / 'adapters/opencode/install.py'), str(install),
                        '--interface', 'v2'], check=True, stdout=subprocess.DEVNULL, timeout=60)
        identity = json.loads((install / 'installation.json').read_text())
        version = subprocess.check_output([str(binary), '--version'], text=True).strip().split()[-1].removeprefix('v')
        assert version == identity['host_version'], f'OpenCode {version} is not the pinned {identity["host_version"]}'
        if scenario == 'unguarded':
            candidate = json.loads((install / 'opencode.json').read_text())
            candidate.pop('plugins')
            (install / 'opencode.json').write_text(json.dumps(candidate, indent=2) + '\n')
        persona = install / 'bundle' / 'agents' / PERSONA
        standard = install / 'bundle' / STANDARD
        target = project / 'docs' / 'target.md'
        reads = f'EGM required reads:\n- {target}\n- {standard}'
        MockProvider.dispatch = {
            'agent': REVIEWER, 'description': 'Probe reviewer',
            'prompt': f'Mode: REVIEW. Review {target}.' + ('' if scenario == 'undeclared' else f'\n\n{reads}'),
        }
        MockProvider.parent_steps = ['dispatch', 'resume'] if scenario == 'guarded' else ['dispatch']
        attacks = [
            ('read', {'path': str(project / '.env')}),
            ('read', {'path': str(project / 'alias.md')}),
            ('read', {'path': str(project / 'reference.md')}),
            ('read', {'path': str(outside / 'private.md')}),
            ('read', {'path': str(project / 'sub' / 'notes.md')}),
            ('write', {'path': str(project / 'written.md'), 'content': 'write probe'}),
            ('shell', {'command': f'touch {project / "shell-ran"}'}),
            ('skill', {'id': 'egm-review'}),
        ]
        MockProvider.child_steps = {
            # The target is read by a relative path to check identity matching.
            'guarded': [('read', {'path': str(persona)}), ('read', {'path': 'docs/target.md'}),
                        ('read', {'path': str(standard)}), *attacks],
            'incomplete': [('read', {'path': str(persona), 'limit': 1}), ('read', {'path': str(target)})],
            'widened': [('read', {'path': str(persona)}), ('read', {'path': str(target)}),
                        ('read', {'path': str(standard)}), *attacks],
        }.get(scenario, [('read', {'path': str(persona)})])
        (config_dir / 'opencode.json').write_text(json.dumps({
            '$schema': 'https://opencode.ai/config.json',
            'model': 'fixture/fixture',
            'instructions': [str(root / 'configured.md')],
            'providers': {'fixture': {
                'name': 'fixture', 'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': f'http://127.0.0.1:{port}/v1', 'apiKey': 'fixture'},
                'models': {'fixture': {'name': 'fixture'}},
            }},
        }, indent=2) + '\n')
        for name in ('opencode.json', 'installation.json'):
            shutil.copy2(install / name, output / f'candidate-{name}')
        env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
            'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'),
            'XDG_CACHE_HOME': str(home / 'cache'), 'XDG_STATE_HOME': str(home / 'state'),
            'OPENCODE_CONFIG': str(install / 'opencode.json'),
        }
        command = ['run', '--format', 'json', '--model', 'fixture/fixture', '--agent', 'egm',
                   f'Parent-only marker: {markers["parent"]}. Delegate without it.']
        if scenario == 'unguarded':
            command.append('--auto')
        result = opencode(binary, *command, cwd=project, env=env)
        (output / 'trace.jsonl').write_text(result.stdout)
        (output / 'stderr.txt').write_text(result.stderr)
        (output / 'provider-requests.json').write_text(json.dumps(MockProvider.requests, indent=2) + '\n')
        assert result.returncode == 0, f'{scenario}: OpenCode exited {result.returncode}; inspect stderr.txt'
        # Literal LF only: splitlines() would also split Unicode separators inside JSON strings.
        trace = [json.loads(line) for line in result.stdout.split('\n') if line.strip()]
        calls = [event['part']['state'] for event in trace
                 if event.get('type') == 'tool_use' and event['part'].get('tool') == 'subagent']
        children = [((call.get('metadata') or {}).get('metadata') or {}).get('sessionID') for call in calls]
        exports = {}
        for label, session in [('parent', trace[0]['sessionID'] if trace else None),
                               *((f'child{i}', child) for i, child in enumerate(children))]:
            if session:
                exported = opencode(binary, 'session', 'export', session, cwd=project, env=env)
                assert exported.returncode == 0, f'{scenario}: could not export {label}'
                (output / f'session-{label}.json').write_text(exported.stdout)
                exports[label] = json.loads(exported.stdout)
        if scenario == 'guarded':
            agents = effective_agents(binary, project, env, output)
            (output / 'effective-agents.json').write_text(json.dumps(agents, indent=2) + '\n')

        child_requests = [r for r in MockProvider.requests if role(r) == 'child']
        parent_requests = [r for r in MockProvider.requests if role(r) == 'parent']
        records = tool_records(exports.get('child0', {}))
        by_path = {str(r.get('input', {}).get('path')): r for r in records if r['name'] == 'read'}
        by_name = {r['name']: r for r in records if r['name'] != 'read'}
        first_result = json.dumps([m for m in parent_requests[1]['messages'] if m.get('role') == 'tool']) \
            if len(parent_requests) > 1 else ''
        leaked = sorted(key for key in ('parent', 'global', 'project', 'nested', 'configured')
                        if any(markers[key] in json.dumps(r) for r in child_requests))
        secrets_seen = sorted(key for key in ('secret', 'outside')
                              if any(markers[key] in json.dumps(r) for r in MockProvider.requests))
        child_tools = sorted({t['function']['name'] for r in child_requests for t in r.get('tools', [])})
        checks: dict[str, bool] = {}
        if scenario in ('guarded', 'widened'):
            checks.update({
                'review_completed': bool(calls) and calls[0].get('status') == 'completed' and FINDINGS in first_result,
                'reviewer_tools_read_glob_only': child_tools == ['glob', 'read'],
                'no_parent_or_instruction_context': not leaked,
                'no_secret_or_outside_content': not secrets_seen,
                'required_reads_completed': all(outcome(by_path.get(p)) == 'completed'
                                                for p in (str(persona), 'docs/target.md' if scenario == 'guarded'
                                                          else str(target), str(standard))),
                'env_read_denied': outcome(by_path.get(str(project / '.env'))) == 'denied',
                'env_symlink_denied': outcome(by_path.get(str(project / 'alias.md'))) == 'denied',
                'outside_symlink_denied': outcome(by_path.get(str(project / 'reference.md'))) == 'denied',
                'outside_read_denied': outcome(by_path.get(str(outside / 'private.md'))) == 'denied',
                'write_shell_skill_unavailable': all(outcome(by_name.get(n)) == 'unavailable'
                                                     for n in ('write', 'shell', 'skill')),
                'no_mutation': not (project / 'written.md').exists() and not (project / 'shell-ran').exists(),
            })
        if scenario == 'guarded':
            checks['resume_starts_fresh_child'] = len(children) == 2 and None not in children and children[0] != children[1]
            checks['nested_agents_md_read_allowed'] = outcome(by_path.get(str(project / 'sub' / 'notes.md'))) == 'completed'
        if scenario == 'incomplete':
            checks.update({
                'incomplete_notice': 'EGM: incomplete review' in first_result
                                     and str(persona) in first_result and str(standard) in first_result,
                'findings_withheld': FINDINGS not in first_result,
            })
        if scenario in ('undeclared', 'unguarded'):
            expected = 'EGM required reads' if scenario == 'undeclared' else 'Permission denied'
            checks.update({
                'dispatch_rejected': bool(calls) and calls[0].get('status') == 'error'
                                     and expected in json.dumps(calls[0].get('error')),
                'no_reviewer_started': not child_requests,
            })
        return {
            'scenario': scenario,
            'checks': checks,
            'observations': {
                'subagent_calls': [{'input': c.get('input'), 'status': c.get('status'), 'error': c.get('error')}
                                   for c in calls],
                'child_sessions': children,
                'child_tools': child_tools,
                'leaked_markers': leaked,
                'secrets_seen': secrets_seen,
                'reads': {path: outcome(record) for path, record in by_path.items()},
                'other_tools': {name: outcome(record) for name, record in by_name.items()},
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--opencode', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scenario', choices=SCENARIOS, action='append', help='default: all scenarios')
    args = parser.parse_args()
    source, binary = args.source.resolve(), args.opencode.resolve()
    head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    status = subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], text=True,
    )
    assert not status, 'Source contains staged, unstaged, or untracked changes'
    args.output.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        results = [run(scenario, source, binary, args.output / scenario, server.server_port)
                   for scenario in args.scenario or SCENARIOS]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    summary = {
        'head': head,
        'opencode': subprocess.check_output([str(binary), '--version'], text=True).strip(),
        'model': 'scripted local mock',
        'scenarios': results,
        'scope': ('Scripted delegations and tool calls; not model behavior, full workflows, '
                  'interactive approvals, or general isolation'),
    }
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    if not all(all(r['checks'].values()) and r['checks'] for r in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
