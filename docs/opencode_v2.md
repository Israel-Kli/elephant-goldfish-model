<!-- Copyright 2026 Google LLC. SPDX-License-Identifier: Apache-2.0 -->

# OpenCode V2 integration (experimental)

This adapter targets OpenCode CLI **2.2.6** (`@opencode/cli`) with native V2 `agents`, `permissions`, `commands`, `skills`, `plugins`, and the `subagent` tool.
For OpenCode 1.18.29, see [the V1 guide](opencode.md).
Installation checks and scripted probes do not establish workflow or isolation support.
Maintainer approval, live workflows, and isolation verification remain pending under [#282](https://github.com/google/elephant-goldfish-model/issues/282).

## Install

Use Python 3.10+ and a POSIX filesystem with symbolic links.
Select a fixed candidate commit before installation and keep the installation at its original path.
The installer copies the complete canonical resource trees and the guard plugin. It does not change global or project configuration and refuses an existing destination.

```sh
git rev-parse HEAD
python3 adapters/opencode/install.py --interface v2 /absolute/egm-opencode-v2-candidate
npm install --prefix /absolute/opencode-v2 @opencode/cli@2.2.6
/absolute/opencode-v2/node_modules/.bin/opencode --version
```

Require version `2.2.6` before launch.
Inspect the generated `opencode.json` and `installation.json` before use.
`installation.json` records package identity, the host version, the SHA-256 of `opencode.json`, and hashes of every bundled and plugin file.
The configuration contains seven commands, eight reviewers, the `egm` primary agent, and the `egm-guard` plugin.

Start OpenCode from the project directory with the candidate configuration:

```sh
OPENCODE_CONFIG=/absolute/egm-opencode-v2-candidate/opencode.json opencode --agent egm
```

OpenCode loads configuration and plugins from the reviewed project, and project configuration outranks `OPENCODE_CONFIG`.
For an untrusted repository or a verification run, also set `OPENCODE_DISABLE_PROJECT_CONFIG=1`, which skips project configuration, plugins, skills, and the project `AGENTS.md`.

Model calls require separate user authorization.
Select `egm` before native skill use. The guard denies reviewer dispatch from other agents.
For review text, use an ordinary message rather than a slash command, for example `Use the egm-review skill to review /absolute/path/design.md.`
OpenCode evaluates shell blocks (an exclamation mark followed by a command in backticks) in slash-command arguments before agent tool permissions apply, whether or not the template contains `$ARGUMENTS`.
Do not paste untrusted review material into slash-command arguments.

To uninstall, stop sessions and remove only this candidate directory and its environment override.

## Paths and dispatch

Use the absolute bundle root in the generated prompt.
Resolve `agents/`, `resources/`, and `docs/` against that root, and `references/` against the canonical skill directory after resolving its alias.
Resolve user files and outputs against the project directory. Never substitute project files for bundled personas.

Dispatch reviewers directly from `egm` with the `subagent` tool. Set `agent` to the persona's canonical frontmatter name and supply `description` and `prompt`.
Never pass `sessionID` or `background`; every review is a fresh foreground child session.
The primary agent performs synthesis and writes reports; no nested synthesizer adapter is installed.
Bootstrap worker and scout delegation is outside this adapter's scope. Stop if bootstrap requires them; do not substitute a reviewer or simulate the missing worker.

Pass absolute paths for the target, standards, and selected sources.
Do not pass parent history, private reasoning, or memory.
Supply prior report paths only when the canonical stage requires them, such as `zoom_out_report.md` for Step 8 (Intermediate Zoom Out).
Read prior reports only when explicitly supplied and required by the canonical stage.
Do not retrieve unsolicited history, memory, or earlier reviewer findings.

End every reviewer prompt with an `EGM required reads:` line followed by one `- <absolute path>` line for each file the canonical stage requires that reviewer to read: the target, standards, and any supplied report.
Do not list the persona; the guard always requires it.
Keep the line with an empty list when there is no such file, as in a brainstorm whose concept is in the prompt.
Reviewers read the persona and every listed file from line 1 to the end, even when their review scope is a line range.

Stop if delegation is unavailable, a dispatch is rejected, or a reviewer result starts with `EGM: incomplete review`. Never simulate Goldfish reviewers in the parent conversation.
When brainstorm precedes draft, complete all seven brainstorm reviewers and synthesize their findings into `concepts_brief.md` before drafting.
For review, write each completed stage report before starting the next stage.
Before Step 8, verify `zoom_out_report.md` exists in the artifact directory and pass its absolute path to the reviewer; stop if it is missing.

Honor `EGM artifact directory: <path>` in the user's prompt or loaded instructions, resolving a relative directory against the project directory.
Preserve each readiness, section, and stage approval gate. Ask one plain-text question when the question tool is unavailable; cancelled questions, missing tools, and elapsed time are not approval.

## Guard plugin

The generated configuration denies `subagent` to `egm` for every reviewer. The `egm-guard` plugin grants each validated dispatch as a session rule on the parent session and revokes it afterwards, so without the plugin no reviewer starts, even under `opencode run --auto`.

For each dispatch from `egm`, the guard:

- rejects a prompt without the `EGM required reads:` line, or with a listed file that is missing, empty, outside the project and the bundle, or a `.env` file;
- removes `sessionID` and `background`;
- replaces the result with an `EGM: incomplete review` notice unless the persona and every listed file were read in full, judged by the line ranges OpenCode reports for each read.

For each reviewer request, the guard keeps the reviewer's own system prompt and the date and environment sources, drops every other instruction source (global, project, and nested `AGENTS.md`, configured instructions, MCP and skill guidance) and every synthetic message, and limits tools to `read` and `glob`.
It checks reviewer reads on the resolved file, so symlinks to `.env` files or out of the project and the bundle are denied, even when project configuration or session rules allow more.
A `glob` path outside the project and the bundle is rejected.

Reviewers deny `grep`: its permission resource is the search pattern, so read rules cannot apply to it.
The primary agent asks before writes and shell commands, allows `question`, and allows only `egm-*` skills.

## Verification limits

Tool rules and plugin hooks are not process or filesystem isolation.

- Coverage is per line. OpenCode shortens lines longer than 2,000 characters.
- The orchestrator chooses the required-read list; live runs must check it against the canonical stage.
- Hard links, races between a permission check and the read, and plugins that run after the guard are out of its reach.
- The guard relies on OpenCode 2.2.6 details such as `metadata.experimental.instructions` and the read output header. Keep the version pinned and rerun the probe before changing it.

Run the offline checks and the scripted native probe, which uses a local mock provider instead of a model:

```sh
pytest tests/unit/test_opencode_adapter.py
python tests/integration/opencode_v2_native_probe.py \
  --source . --opencode /absolute/opencode-v2/node_modules/.bin/opencode --output /tmp/egm-opencode-v2-probe
```

Choose an output path that does not exist. A passing probe does not establish live model behavior, interactive approval gates, or full workflows.

## Interface sources

Reviewed on 2026-11-09 against OpenCode 2.2.6:

- [Configuration and precedence](https://opencode.ai/docs/config/)
- [Agents](https://opencode.ai/docs/agents/)
- [Permissions](https://opencode.ai/docs/permissions/)
- [Tools and `subagent`](https://opencode.ai/docs/tools/)
- [Skills](https://opencode.ai/docs/skills/)
- [Commands](https://opencode.ai/docs/commands/)
- [Instructions and `AGENTS.md` discovery](https://opencode.ai/docs/instructions/)
- [Server plugins and hooks](https://opencode.ai/docs/build/plugins/server/)
- [Migration from V1](https://opencode.ai/docs/migrate-v1/)
