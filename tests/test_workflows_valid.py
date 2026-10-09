"""The GitHub Actions workflows parse on GitHub: an expression outside the steps may only read the contexts GitHub makes
available there (a job-level `env` cannot read `runner`, `steps`, `job` or `env`; GitHub then refuses the whole
workflow, as it did for build-gov-datasets' `${{ runner.temp }}`). The check is the context-availability rule of
actionlint, read with a small indentation scanner (no YAML dependency in the test image). No network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# https://docs.github.com/en/actions/learn-github-actions/contexts#context-availability
WORKFLOW_LEVEL = {"github", "inputs", "vars", "secrets"}
JOB_LEVEL = {"github", "inputs", "vars", "secrets", "needs", "strategy", "matrix"}
STEP_LEVEL = None                                       # steps (and job outputs) may read every context
EVERYWHERE_IN_STEPS = ("steps", "outputs")

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")
_STRING = re.compile(r"'(?:[^']|'')*'")
_CONTEXT = re.compile(r"(?<![\w.\-])([A-Za-z_][\w-]*)\s*(?=[.\[])")
_KEY = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z_][\w-]*)\s*:(?:\s+(.*))?$")


def _contexts(expression: str) -> set[str]:
    return set(_CONTEXT.findall(_STRING.sub("''", expression)))


def _allowed(path: list[tuple[int, str]]) -> set[str] | None:
    keys = [key for _, key in path]
    if keys[:1] != ["jobs"]:
        return WORKFLOW_LEVEL
    if len(keys) >= 3 and keys[2] in EVERYWHERE_IN_STEPS:
        return STEP_LEVEL
    return JOB_LEVEL


def misplaced_contexts(text: str) -> list[str]:
    """`line: context` for every expression that reads a context GitHub does not offer where it stands."""
    problems: list[str] = []
    path: list[tuple[int, str]] = []                    # (indent, key) of the enclosing mapping keys
    block_indent: int | None = None                     # inside a `key: |` / `key: >` scalar deeper than this
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None and indent > block_indent:
            pass                                        # block scalar content: the enclosing key decides
        else:
            block_indent = None
            match = _KEY.match(line)
            if match:
                key_indent = indent + (len(line.lstrip(" ")) - len(line.lstrip(" ").lstrip("- ")))
                while path and path[-1][0] >= key_indent:
                    path.pop()
                path.append((key_indent, match.group(2)))
                if (match.group(3) or "").strip()[:1] in ("|", ">"):
                    block_indent = key_indent
        allowed = _allowed(path) if path else WORKFLOW_LEVEL
        if allowed is None:
            continue
        for expression in _EXPRESSION.findall(line):
            bad = sorted(_contexts(expression) - allowed)
            if bad:
                problems.append(f"{number}: {', '.join(bad)} in ${{{{{expression}}}}} "
                                f"({'.'.join(key for _, key in path)})")
    return problems


def test_there_are_workflows_to_check():
    assert {path.name for path in WORKFLOWS} >= {"tests.yml", "build-gov-datasets.yml", "build-open-data.yml"}


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_expression_reads_a_context_github_does_not_offer_there(workflow):
    assert misplaced_contexts(workflow.read_text("utf-8")) == []


def test_the_scanner_rejects_runner_in_a_job_env_and_accepts_it_in_a_step():
    broken = (
        "on:\n  workflow_dispatch:\n"
        "env:\n  A: ${{ github.run_id }}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    env:\n"
        "      DATASETS: ${{ inputs.datasets || 'all' }}\n"
        "      GOV_WORK: ${{ runner.temp }}/gov-work\n"
        "    steps:\n"
        "      - name: ok\n        if: ${{ steps.x.outcome == 'failure' }}\n"
        "        env:\n          W: ${{ runner.temp }}\n"
        "        run: |\n          echo ${{ runner.os }} ${{ env.A }}\n"
    )
    problems = misplaced_contexts(broken)
    assert len(problems) == 1 and problems[0].startswith("10: runner in") and "jobs.build.env.GOV_WORK" in problems[0]
    assert misplaced_contexts("env:\n  X: ${{ format('{0}', 'runner.temp') }}\n") == []        # strings, functions
    assert misplaced_contexts("jobs:\n  a:\n    if: ${{ steps.s.outputs.x }}\n") != []


def test_build_gov_datasets_sets_its_work_folder_in_the_first_step():
    workflow = (ROOT / ".github" / "workflows" / "build-gov-datasets.yml").read_text("utf-8")
    steps = workflow[workflow.index("    steps:\n"):]
    first = steps[:steps.index("\n      - ", steps.index("      - ") + 1)]
    assert 'echo "GOV_WORK=$RUNNER_TEMP/gov-work" >> "$GITHUB_ENV"' in first
    assert "runner.temp" not in workflow[:workflow.index("    steps:\n")]
    assert steps.index("GITHUB_ENV") < steps.index('rm -rf "$GOV_WORK"')
