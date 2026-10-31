"""Evaluating research runs against a fixed set of questions.

Each question in `eval/questions.toml` is researched by a running service, without clarifications,
and its result saved. A result already saved is not researched again, so an interrupted evaluation
resumes. Every saved result is then scored:

- whether the brief chose the output type the question's wording asks for;
- a checklist: the strong model answers yes or no to each of the question's checks against the
  report. A small judge's absolute scores are unreliable; a yes or no to a narrow check is not;
- citations: each cited sentence of the final report against the quotes of the sources it cites.
  This repeats the pipeline's own verification on what survived it, with the strong model as judge.

    uv run python -m kvasir.deep.evaluate data/eval/baseline

Scores go to `scores.json` in the same directory, with a summary table on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import dspy  # type: ignore[import-untyped]
import httpx
from dspy.utils.exceptions import AdapterParseError  # type: ignore[import-untyped]

from kvasir.config import Settings
from kvasir.deep import verify
from kvasir.deep.signatures import Verify
from kvasir.runners import build_research_models
from kvasir.storm.lm import GatewayModel

QUESTIONS = Path(__file__).parents[3] / "eval" / "questions.toml"


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    output: str
    checks: list[str]


class Check(dspy.Signature):
    """Decide whether a research report meets one requirement. Judge only what the report says."""

    question: str = dspy.InputField()
    report: str = dspy.InputField()
    requirement: str = dspy.InputField()
    met: bool = dspy.OutputField()
    reason: str = dspy.OutputField(desc="one short sentence")


@dataclass
class Score:
    id: str
    output: str
    expected_output: str
    checks_met: int
    checks: int
    cited_sentences: int
    supported_sentences: int
    uncited_sentences: int
    removed_by_pipeline: int
    claims: int
    sources: int
    seconds: float
    # Each failed check and unsupported sentence, with the judge's reason, for reading by hand.
    failures: list[str]


def load_questions(path: Path) -> list[Question]:
    return [Question(**entry) for entry in tomllib.loads(path.read_text())["question"]]


def frames(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Server-sent events as (event, payload) pairs."""
    event = ""
    for line in lines:
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            yield event, json.loads(line[len("data: ") :])


def research(url: str, question: Question) -> dict[str, Any]:
    """The run's result, or its error, as the service reports them."""
    with httpx.stream(
        "POST", f"{url}/v1/research", json={"question": question.question}, timeout=None
    ) as response:
        if response.status_code != 200:
            return {"error": f"{response.status_code}: {response.read().decode()}"}
        stage = ""
        for event, data in frames(response.iter_lines()):
            if event == "progress" and data["stage"] != stage:
                stage = data["stage"]
                print(f"  {stage}", file=sys.stderr)
            elif event == "error":
                return {"error": data["message"]}
            elif event == "done":
                return dict(data)
    return {"error": "the stream ended without a result"}


def score(judge: GatewayModel, question: Question, result: dict[str, Any]) -> Score:
    failures: list[str] = []
    report = result.get("report", "")

    met = 0
    for requirement in question.checks:
        passed, reason = _ask(
            judge, Check, "met", question=question.question, report=report, requirement=requirement
        )
        met += passed
        if not passed:
            failures.append(f"check: {requirement} ({reason})")

    cited = supported = uncited = 0
    for sentence in prose(report):
        numbers = verify.cited(sentence)
        if not numbers:
            uncited += 1
            continue
        cited += 1
        evidence = _evidence(result, numbers)
        if not evidence:
            failures.append(f"citation: {sentence} (cites no source the run listed)")
            continue
        passed, reason = _ask(judge, Verify, "supported", sentence=sentence, evidence=evidence)
        supported += passed
        if not passed:
            failures.append(f"citation: {sentence} ({reason})")

    return Score(
        id=question.id,
        output=result.get("output", ""),
        expected_output=question.output,
        checks_met=met,
        checks=len(question.checks),
        cited_sentences=cited,
        supported_sentences=supported,
        uncited_sentences=uncited,
        removed_by_pipeline=len(result.get("removed", [])),
        claims=len(result.get("claims", [])),
        sources=len(result.get("citations", [])),
        seconds=result.get("duration_seconds", 0.0),
        failures=failures,
    )


def prose(report: str) -> Iterator[str]:
    """The report's sentences, without headings, tables or code."""
    fenced = False
    for line in report.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith(("#", "|")):
            continue
        yield from (sentence.strip() for sentence in verify.sentences(stripped))


def _evidence(result: dict[str, Any], numbers: list[int]) -> str:
    """The claims read from each cited source, with their quotes."""
    urls = {c["url"] for c in result.get("citations", []) if c["index"] in numbers}
    return "\n".join(
        f"- {claim['text']}\n    quote: {claim['quote']}"
        for claim in result.get("claims", [])
        if claim["url"] in urls
    )


def _ask(model: GatewayModel, signature: type[dspy.Signature], field: str, **inputs: Any) -> Any:
    """The verdict's boolean and reason. An unreadable verdict counts as a failure, and says so."""
    try:
        with dspy.context(lm=model):
            verdict = dspy.Predict(signature)(**inputs)
    except AdapterParseError:
        return False, "the judge's verdict could not be read"
    return bool(getattr(verdict, field)), str(verdict.reason)


def summary(scores: list[Score], errors: dict[str, str]) -> str:
    """A markdown table, one row per question, and the totals."""
    rows = [
        "| Question | Output | Checks | Cited supported | Uncited | Removed | Sources | Minutes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        output = (
            s.output if s.output == s.expected_output else f"{s.output} (not {s.expected_output})"
        )
        rows.append(
            f"| {s.id} | {output} | {s.checks_met}/{s.checks} "
            f"| {s.supported_sentences}/{s.cited_sentences} | {s.uncited_sentences} "
            f"| {s.removed_by_pipeline} | {s.sources} | {s.seconds / 60:.1f} |"
        )
    rows.extend(f"| {name} | failed: {error} | | | | | | |" for name, error in errors.items())
    if not scores:
        return "\n".join(rows)

    def share(part: int, whole: int) -> str:
        return f"{part / whole:.0%}" if whole else "n/a"

    cited = sum(s.cited_sentences for s in scores)
    expected = sum(s.output == s.expected_output for s in scores)
    totals = [
        f"Output as expected: {share(expected, len(scores))}",
        f"Checks met: {share(sum(s.checks_met for s in scores), sum(s.checks for s in scores))}",
        f"Cited sentences supported: {share(sum(s.supported_sentences for s in scores), cited)}",
        f"Mean minutes per run: {mean(s.seconds for s in scores) / 60:.1f}",
        f"Failed runs: {len(errors)}",
    ]
    return "\n".join(rows) + "\n\n" + "\n".join(totals)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m kvasir.deep.evaluate", description=__doc__)
    parser.add_argument("out", type=Path, help="directory for results and scores")
    parser.add_argument("--url", default="http://localhost:8000", help="the kvasir service")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--only", nargs="*", default=None, help="question ids to run")
    args = parser.parse_args(argv)

    questions = [
        q for q in load_questions(args.questions) if args.only is None or q.id in args.only
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    for question in questions:
        path = args.out / f"{question.id}.json"
        if path.exists():
            continue
        print(f"researching {question.id}", file=sys.stderr)
        path.write_text(json.dumps(research(args.url, question), indent=1))

    judge = build_research_models(Settings.from_env())["judge"]
    scores: list[Score] = []
    errors: dict[str, str] = {}
    for question in questions:
        result = json.loads((args.out / f"{question.id}.json").read_text())
        if "error" in result:
            errors[question.id] = result["error"]
            continue
        print(f"scoring {question.id}", file=sys.stderr)
        scores.append(score(judge, question, result))

    (args.out / "scores.json").write_text(
        json.dumps({"scores": [asdict(s) for s in scores], "errors": errors}, indent=1)
    )
    print(summary(scores, errors))


if __name__ == "__main__":
    main()
