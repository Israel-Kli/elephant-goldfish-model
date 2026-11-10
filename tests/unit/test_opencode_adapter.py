# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

"""Offline checks for the experimental OpenCode V1 installer."""

import hashlib
import json
import os
from pathlib import Path
import re
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = runpy.run_path(str(ROOT / "adapters/opencode/install.py"))


def test_install_preserves_resources_and_native_contract(tmp_path: Path) -> None:
    destination = tmp_path / "EGM candidate ü"
    config_path = ADAPTER["install"](destination)
    config = json.loads(config_path.read_text())
    identity = json.loads((destination / "installation.json").read_text())
    canonical = json.loads((ROOT / "plugin.json").read_text())
    assert (identity["name"], identity["version"]) == (canonical["name"], canonical["version"])
    assert identity["host_version"] == "1.18.29"
    for relative, digest in identity["sha256"].items():
        installed = destination / relative
        source = ROOT / installed.relative_to(destination / "bundle")
        assert installed.read_bytes() == source.read_bytes()
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest
    for directory in ("skills", "agents", "resources", "docs"):
        for source in (ROOT / directory).rglob("*"):
            if source.is_file():
                assert (Path("bundle") / source.relative_to(ROOT)).as_posix() in identity["sha256"]

    assert config["default_agent"] == "egm"
    assert config["autoupdate"] is False
    assert "model" not in config
    assert len(config["command"]) == 7
    for skill in (ROOT / "skills").glob("*/SKILL.md"):
        key = ADAPTER["name"](skill)
        alias = Path(config["skills"]["paths"][0]) / key
        assert alias.resolve() == destination / "bundle/skills" / skill.parent.name
        assert (alias / "SKILL.md").read_bytes() == skill.read_bytes()
        command = config["command"][key]
        assert command["agent"] == "egm"
        assert str(alias.resolve() / "SKILL.md") in command["template"]
        assert "$ARGUMENTS" in command["template"]

    assert len(config["agent"]) == 9
    for filename in ADAPTER["REVIEWERS"]:
        persona = destination / "bundle/agents" / f"{filename}.md"
        agent = config["agent"][ADAPTER["name"](persona)]
        assert agent["mode"] == "subagent"
        assert agent["permission"] == {
            "*": "deny",
            "read": {
                "*": "allow", "*.env": "deny", "*.env.*": "deny",
                "*.env.example": "allow", "mcp:*": "deny",
            },
            "glob": "allow", "grep": "deny",
            "external_directory": {"*": "deny", f"{destination / 'bundle'}/**": "allow"},
        }
        # V1 maps resource reads and both list tools to read. Last match wins.
        assert list(agent["permission"]["read"].items()) == [
            ("*", "allow"), ("*.env", "deny"), ("*.env.*", "deny"),
            ("*.env.example", "allow"), ("mcp:*", "deny"),
        ]
        assert str(persona) in agent["prompt"]
        assert "Read-only Protocol" not in agent["prompt"]
    primary = config["agent"]["egm"]
    assert primary["permission"]["*"] == "ask"
    assert list(primary["permission"]["read"].items()) == [
        ("*", "allow"), ("*.env", "ask"), ("*.env.*", "ask"), ("*.env.example", "allow"),
    ]
    assert set(primary["permission"]["task"]) == {"*", *set(config["agent"]) - {"egm"}}
    assert "Omit task_id" in primary["prompt"]
    assert "foreground completion" in primary["prompt"]


def test_stage_required_reports_preserve_context_boundary(tmp_path: Path) -> None:
    destination = tmp_path / "candidate"
    config = json.loads(ADAPTER["install"](destination).read_text())
    bundle = destination / "bundle"
    canonical = (bundle / "skills/egm-review/SKILL.md").read_text()
    assert "include `zoom_out_report.md` path in" in canonical
    guide = " ".join((bundle / "docs/opencode.md").read_text().split())
    prompt = config["agent"]["egm-zoom-out"]["prompt"]
    for instructions in (prompt, guide):
        assert (
            "Read prior reports only when explicitly supplied and required by the canonical stage."
        ) in instructions
        assert "Do not retrieve unsolicited history, memory, or earlier reviewer findings." in instructions
    assert "Do not pass parent history, private reasoning, or memory." in guide


def test_v2_install_uses_native_config_and_guard(tmp_path: Path) -> None:
    destination = tmp_path / "candidate"
    config_path = ADAPTER["install"](destination, "v2")
    config = json.loads(config_path.read_text())
    identity = json.loads((destination / "installation.json").read_text())
    bundle = destination / "bundle"
    assert (identity["host_version"], identity["interface"]) == (ADAPTER["V2_HOST_VERSION"], "v2")
    assert identity["config_sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert "plugin/index.ts" in identity["sha256"]
    for relative, digest in identity["sha256"].items():
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
    # Keys from the V1 schema would mix formats inside one document.
    assert not {"agent", "command", "autoupdate", "permission"} & set(config)
    # V2 takes skill IDs from directory names, so it needs no alias links.
    assert config["skills"] == [str(destination / "bundle/skills")]
    assert not (destination / "skills").exists()

    personas = {
        ADAPTER["name"](bundle / "agents" / f"{filename}.md"): str(bundle / "agents" / f"{filename}.md")
        for filename in ADAPTER["REVIEWERS"]
    }
    [plugin] = config["plugins"]
    assert plugin == {
        "package": str(destination / "plugin"),
        "options": {"primary": "egm", "bundle": str(bundle), "reviewers": personas},
    }
    assert set(config["agents"]) == {"egm", *personas}
    for key, persona in personas.items():
        agent = config["agents"][key]
        assert agent["mode"] == "subagent"
        assert persona in agent["system"]
        assert [(r["action"], r["resource"], r["effect"]) for r in agent["permissions"]] == [
            ("*", "*", "deny"),
            ("read", "*", "allow"), ("read", "*.env", "deny"), ("read", "*.env.*", "deny"),
            ("read", "*.env.example", "allow"),
            ("glob", "*", "allow"),
            ("external_directory", "*", "deny"), ("external_directory", f"{bundle}/*", "allow"),
        ]
    guide = " ".join((bundle / "docs/opencode_v2.md").read_text().split())
    for instructions in (config["agents"]["egm-zoom-out"]["system"], guide):
        assert "Read prior reports only when explicitly supplied and required by the canonical stage." in instructions
        assert "Do not retrieve unsolicited history, memory, or earlier reviewer findings." in instructions
    assert "Do not pass parent history, private reasoning, or memory." in guide
    primary = config["agents"]["egm"]
    # Only the guard plugin grants reviewer dispatch, so it stays denied in the config.
    assert [(r["resource"], r["effect"]) for r in primary["permissions"] if r["action"] == "subagent"] == [
        ("*", "deny"), *((key, "deny") for key in personas),
    ]
    assert "EGM required reads:" in primary["system"]
    for command in config["commands"].values():
        assert command["agent"] == "egm"
        assert str(bundle / "docs/opencode_v2.md") in command["template"]
    with pytest.raises(ValueError, match="interface"):
        ADAPTER["install"](tmp_path / "other", "v3")


def test_existing_destination_is_untouched(tmp_path: Path) -> None:
    sentinel = tmp_path / "existing.txt"
    sentinel.write_text("other worker")
    with pytest.raises(FileExistsError):
        ADAPTER["install"](tmp_path)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "other worker"


@pytest.mark.parametrize("suffix", ["bad*", "bad?", "bad[glob]", "{env:HOME}", "bad\npath", r"bad\path"])
def test_template_paths_are_rejected(tmp_path: Path, suffix: str) -> None:
    if "\\" in suffix and Path(suffix).name != suffix:
        pytest.skip("Requires a literal backslash in a POSIX filename")
    with pytest.raises(ValueError):
        ADAPTER["install"](tmp_path / suffix)
    assert not list(tmp_path.iterdir())


def test_source_destination_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the source"):
        ADAPTER["install"](ROOT / "unused-opencode-test-destination")


def test_command_interpolation_paths_are_rejected(tmp_path: Path) -> None:
    # V1 replaces positional placeholders and $ARGUMENTS across the template.
    for component in ("$ARGUMENTS", "$1"):
        with pytest.raises(ValueError, match="template or glob"):
            ADAPTER["install"](tmp_path / component / "candidate")
    assert not list(tmp_path.iterdir())


def test_symlink_parent_cannot_bypass_path_validation(tmp_path: Path) -> None:
    target = tmp_path / "unsafe[glob]"
    target.mkdir()
    alias = tmp_path / "safe"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        ADAPTER["install"](alias / "candidate")
    assert not list(target.iterdir())


def test_dangling_destination_is_untouched(tmp_path: Path) -> None:
    destination = tmp_path / "candidate"
    destination.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError):
        ADAPTER["install"](destination)
    assert destination.is_symlink()
    assert not (tmp_path / "absent").exists()


def v1_match(pattern: str, value: str) -> bool:
    """Port of V1 Wildcard.match (anomalyco/opencode@1674747): "*" spans "/"."""
    escaped = re.escape(pattern.replace("\\", "/")).replace(r"\*", ".*").replace(r"\?", ".")
    flags = re.S | (re.I if os.name == "nt" else 0)
    return re.fullmatch(escaped, value.replace("\\", "/"), flags) is not None


def v1_read_effect(rules: dict, path: str) -> str | None:
    effect = None
    for pattern, action in rules.items():
        if v1_match(pattern, path):
            effect = action
    return effect


def test_required_reads_pass_env_rules(tmp_path: Path) -> None:
    # V1 checks reads against the path relative to the worktree.
    worktree = tmp_path / "project"
    worktree.mkdir()
    destination = tmp_path / "candidate"
    config = json.loads(ADAPTER["install"](destination).read_text())
    bundle = destination / "bundle"
    required = [
        bundle / "docs/opencode.md",
        *(bundle / "agents").glob("*.md"),
        *(bundle / "resources").rglob("*.md"),
    ]
    for name, agent in config["agent"].items():
        rules = agent["permission"]["read"]
        for path in required:
            assert v1_read_effect(rules, os.path.relpath(path, worktree)) == "allow", (name, path)
        secret = "ask" if name == "egm" else "deny"
        for path in (".env", "app/.env", ".env.local", "app/.env.production", "prod.env"):
            assert v1_read_effect(rules, path) == secret, (name, path)
        assert v1_read_effect(rules, "app/.env.example") == "allow"


@pytest.mark.parametrize("suffix", ["egm.env.candidate", "staging.ENV.d/candidate"])
def test_env_matching_destination_is_rejected(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ValueError, match="read rules"):
        ADAPTER["install"](tmp_path / suffix)
    assert not list(tmp_path.iterdir())
