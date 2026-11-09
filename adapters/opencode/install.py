# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

"""Install an experimental OpenCode bundle without changing user config."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[2]
HOST_VERSION = "1.18.29"
V2_HOST_VERSION = "2.2.6"
REVIEWERS = (
    "codebase_details", "comprehension", "design_critic",
    "implementation_readiness", "inventive_catalyst", "structural", "style", "zoom_out",
)


def name(path: Path) -> str:
    """Read the canonical name without a runtime YAML dependency."""
    header = path.read_text(encoding="utf-8").split("---", 2)[1]
    match = re.search(r"^name: (egm-[a-z-]+)$", header, re.MULTILINE)
    if not match:
        raise ValueError(f"Missing canonical name: {path}")
    return match[1]


def rules(*entries: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"action": action, "resource": resource, "effect": effect} for action, resource, effect in entries]


def install(destination: Path, interface: str = "v1") -> Path:
    """Create a new installation; refuse to overwrite any existing path."""
    if interface not in ("v1", "v2"):
        raise ValueError(f"Unknown OpenCode interface: {interface}")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination = destination.resolve()
    # Check resolved parents too: paths enter glob rules and prompt templates.
    if "\\" in destination.as_posix() or any(
        c in str(destination) for c in '*?[]{}!@`$\n\r'
    ):
        raise ValueError("Installation path contains template or glob characters")
    # Read rules match worktree-relative paths and "*" spans "/", so "*.env.*"
    # would deny every bundle read below such a path.
    if ".env." in str(destination).lower():
        raise ValueError("Installation path matches the .env read rules")
    if destination.is_relative_to(ROOT):
        raise ValueError("Install outside the source tree")
    destination.mkdir(parents=True)
    bundle = destination / "bundle"
    bundle.mkdir()
    for directory in ("skills", "agents", "resources", "docs"):
        shutil.copytree(ROOT / directory, bundle / directory)
    for filename in ("plugin.json", "LICENSE", "README.md", "TESTING.md"):
        shutil.copy2(ROOT / filename, bundle / filename)

    guide = bundle / ("docs/opencode.md" if interface == "v1" else "docs/opencode_v2.md")
    location = (
        f"EGM bundle root: {bundle}. "
        "Resolve agents/, resources/, and docs/ against this root. "
        "Resolve references/ against the canonical skill directory. "
        "Resolve user files and outputs against the project working directory. "
    )
    context = f"Read {guide} first. " + location
    personas = {name(bundle / "agents" / f"{r}.md"): (r, bundle / "agents" / f"{r}.md") for r in REVIEWERS}
    skills = destination / "skills"
    skills.mkdir()
    commands = {}
    for skill in sorted((bundle / "skills").glob("*/SKILL.md")):
        skill_name = name(skill)
        (skills / skill_name).symlink_to(skill.parent, target_is_directory=True)
        commands[skill_name] = {
            "description": f"Run {skill_name} (experimental)",
            "agent": "egm",
            "template": context + f"Read and execute {skill}.\nUser request: $ARGUMENTS",
        }
    reviewer_prompt = (
        "Read {persona} before the target and supplied standards. "
        "Return findings to the caller. Do not write reports or delegate. "
        "Read prior reports only when explicitly supplied and required by the canonical stage. "
        "Do not retrieve unsolicited history, memory, or earlier reviewer findings. "
        "Report inaccessible required files as an incomplete review."
    )
    stages = (
        "When running brainstorm before draft, complete all seven brainstorm "
        "reviewers and synthesize their findings into concepts_brief.md before drafting. "
        "In review, write each completed stage report before starting the next stage. "
        "Before Step 8, verify zoom_out_report.md exists in the artifact directory "
        "and pass its absolute path to the reviewer; stop if it is missing. "
        "Write reports yourself. Preserve every human approval gate."
    )
    if interface == "v1":
        config = v1_config(bundle, skills, commands, personas, context, reviewer_prompt, stages)
        host_version, extra, owned = HOST_VERSION, {}, [bundle]
    else:
        plugin = destination / "plugin"
        shutil.copytree(ROOT / "adapters/opencode/plugin", plugin)
        config = v2_config(bundle, skills, plugin, commands, personas, location, reviewer_prompt, stages)
        host_version, extra, owned = V2_HOST_VERSION, {"interface": "v2"}, [bundle, plugin]
    config_path = destination / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if interface == "v2":
        # The plugin entry and reviewer rules live in the config, so evidence must identify it too.
        extra["config_sha256"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    metadata = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))
    (destination / "installation.json").write_text(json.dumps({
        "name": metadata["name"], "version": metadata["version"],
        "host_version": host_version, **extra, "status": "experimental",
        "sha256": {
            path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in owned for path in sorted(root.rglob("*")) if path.is_file()
        },
    }, indent=2) + "\n", encoding="utf-8")
    return config_path


def v1_config(bundle, skills, commands, personas, context, reviewer_prompt, stages) -> dict:
    agents = {}
    for key, (reviewer, persona) in personas.items():
        agents[key] = {
            "description": f"EGM read-only {reviewer} reviewer (experimental)",
            "mode": "subagent",
            "color": "#E86E00",
            "permission": {
                "*": "deny",
                # Later rules win, so "*" alone would override OpenCode's default .env rules.
                "read": {
                    "*": "allow", "*.env": "deny", "*.env.*": "deny",
                    "*.env.example": "allow", "mcp:*": "deny",
                },
                # grep does not enforce read rules, so it could expose .env files.
                "glob": "allow", "grep": "deny",
                "external_directory": {"*": "deny", f"{bundle}/**": "allow"},
            },
            "prompt": context + reviewer_prompt.format(persona=persona),
        }
    agents["egm"] = {
        "description": "EGM orchestrator (experimental OpenCode V1)",
        "mode": "primary",
        "color": "#1B6EF3",
        "permission": {
            "*": "ask",
            "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"},
            "glob": "allow", "grep": "allow",
            "skill": {"*": "deny", "egm-*": "allow"},
            "task": {"*": "deny", **{key: "allow" for key in agents}},
            "external_directory": {"*": "ask", f"{bundle}/**": "allow"},
        },
        "prompt": context + "Follow the canonical skill. Dispatch reviewers directly "
        "with task and the registered persona name as subagent_type. "
        "Omit task_id for every independent review; never resume a reviewer. "
        "Wait for foreground completion. Pass only the task, absolute resource paths, "
        "target, standards, and selected reading list, not conversation history. "
        "Stop if delegation or a required completion is missing. " + stages,
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "default_agent": "egm",
        "skills": {"paths": [str(skills)]},
        "agent": agents,
        "command": commands,
    }


def v2_config(bundle, skills, plugin, commands, personas, location, reviewer_prompt, stages) -> dict:
    agents = {}
    for key, (reviewer, persona) in personas.items():
        agents[key] = {
            "description": f"EGM read-only {reviewer} reviewer (experimental)",
            "mode": "subagent",
            "color": "#E86E00",
            # The last matching rule wins. grep stays denied: its resource is the
            # pattern, not the path, so the .env read rules cannot apply to it.
            "permissions": rules(
                ("*", "*", "deny"),
                ("read", "*", "allow"), ("read", "*.env", "deny"), ("read", "*.env.*", "deny"),
                ("read", "*.env.example", "allow"),
                ("glob", "*", "allow"),
                ("external_directory", "*", "deny"), ("external_directory", f"{bundle}/*", "allow"),
            ),
            # The guard checks that each required file was read in full, so a line-range scope
            # must not turn into a partial read.
            "system": location + reviewer_prompt.format(persona=persona)
            + " Read the persona and every file listed under \"EGM required reads\" from line 1 to the end, "
            "even when your review scope is a line range; pass offset only to continue after a truncated page.",
        }
    agents["egm"] = {
        "description": "EGM orchestrator (experimental OpenCode V2)",
        "mode": "primary",
        "color": "#1B6EF3",
        "permissions": rules(
            ("*", "*", "ask"),
            ("read", "*", "allow"), ("read", "*.env", "ask"), ("read", "*.env.*", "ask"),
            ("read", "*.env.example", "allow"),
            ("glob", "*", "allow"), ("grep", "*", "allow"), ("question", "*", "allow"),
            ("skill", "*", "deny"), ("skill", "egm-*", "allow"),
            # The guard plugin grants each reviewer dispatch as a session rule; without it, none starts.
            # The per-reviewer denies keep OpenCode 2.2.6 from dropping the subagent tool altogether.
            ("subagent", "*", "deny"), *(("subagent", key, "deny") for key in personas),
            ("external_directory", "*", "ask"), ("external_directory", f"{bundle}/*", "allow"),
        ),
        "system": f"Read {bundle / 'docs/opencode_v2.md'} first. " + location
        + "Follow the canonical skill. Dispatch reviewers directly with the subagent tool: "
        "set agent to the registered persona name and supply description and prompt. "
        "Never pass sessionID or background; every review starts a fresh foreground child session. "
        "Pass only the task, absolute resource paths, target, standards, and selected reading list, "
        "not conversation history. End every reviewer prompt with a line \"EGM required reads:\" "
        "followed by one \"- <absolute path>\" line for each file the canonical stage requires that "
        "reviewer to read: the target, standards, and any supplied report. Do not list the persona. "
        "Keep the line with an empty list when there is no such file, as in a brainstorm whose concept "
        "is in the prompt. Stop if delegation or a required completion is missing, "
        "or if a reviewer result starts with \"EGM: incomplete review\". " + stages,
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "update": "disable",
        "default_agent": "egm",
        "skills": [str(skills)],
        "plugins": [{
            "package": str(plugin),
            "options": {
                "primary": "egm",
                "bundle": str(bundle),
                "reviewers": {key: str(persona) for key, (_, persona) in personas.items()},
            },
        }],
        "agents": agents,
        "commands": commands,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New, fixed installation directory")
    parser.add_argument("--interface", choices=("v1", "v2"), default="v1", help="OpenCode configuration interface")
    arguments = parser.parse_args()
    print(install(arguments.destination, arguments.interface))
