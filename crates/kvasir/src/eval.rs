//! `kvasir eval`: research a fixed set of questions through a running service, then score each
//! result.
//!
//! Each question in `eval/questions.toml` is researched without clarifications and its result
//! saved. A result already saved is not researched again, so an interrupted evaluation resumes.
//! Every saved result is then scored:
//!
//! - whether the brief chose the output type the question's wording asks for;
//! - a checklist: the reason model answers yes or no to each of the question's checks against the
//!   report. A small judge's absolute scores are unreliable; a yes or no to a narrow check is not;
//! - citations: each cited sentence of the final report against the quotes of the sources it
//!   cites. This repeats the pipeline's own verification on what survived it, with the reason
//!   model as judge.
//!
//! Scores go to `scores.json` in the same directory, with a summary table on stdout.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use kvasir_config::Settings;
use kvasir_llm::gateway::Gateway;
use kvasir_llm::{LlmError, NoSink, RETRY_FOR, Signature, Target, roles, signature};
use kvasir_research::signatures::{Support, verify};
use kvasir_research::verify::{cited, sentences};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

pub const USAGE: &str = "usage: kvasir eval <out> [--url URL] [--questions PATH] [--only ID...] \
[--method METHOD] [--options JSON]";

signature!(check, "kvasir.research.evaluate.Check", include_str!("prompts/check.md"),
    in { question: String = "", report: String = "", requirement: String = "" },
    out { met: bool = "", reason: String = "one short sentence" });

#[derive(Debug, Deserialize)]
struct Question {
    id: String,
    question: String,
    output: String,
    checks: Vec<String>,
}

#[derive(Deserialize)]
struct Questions {
    question: Vec<Question>,
}

#[derive(Debug, Serialize)]
struct Score {
    id: String,
    output: String,
    expected_output: String,
    checks_met: usize,
    checks: usize,
    cited_sentences: usize,
    supported_sentences: usize,
    uncited_sentences: usize,
    removed_by_pipeline: usize,
    claims: usize,
    sources: usize,
    seconds: f64,
    /// Each failed check and unsupported sentence, with the judge's reason, for reading by hand.
    failures: Vec<String>,
}

#[derive(Debug, PartialEq)]
struct Args {
    out: PathBuf,
    url: String,
    questions: PathBuf,
    only: Option<Vec<String>>,
    method: String,
    options: Value,
}

fn parse(args: &[&str]) -> anyhow::Result<Args> {
    let mut parsed = Args {
        out: PathBuf::new(),
        url: "http://localhost:8080".into(),
        questions: PathBuf::from("eval/questions.toml"),
        only: None,
        method: "ledger".into(),
        options: json!({}),
    };
    let mut out = None;
    let mut rest = args.iter().copied().peekable();
    while let Some(arg) = rest.next() {
        let mut value = || rest.next().with_context(|| format!("{arg} takes a value"));
        match arg {
            "--url" => parsed.url = value()?.trim_end_matches('/').to_owned(),
            "--questions" => parsed.questions = value()?.into(),
            "--method" => parsed.method = value()?.to_owned(),
            "--options" => {
                parsed.options = serde_json::from_str(value()?).context("--options")?;
                if !parsed.options.is_object() {
                    bail!("--options takes a JSON object");
                }
            }
            "--only" => {
                let mut ids = Vec::new();
                while let Some(id) = rest.next_if(|next| !next.starts_with("--")) {
                    ids.push(id.to_owned());
                }
                parsed.only = Some(ids);
            }
            flag if flag.starts_with("--") => bail!("unknown option {flag}"),
            path if out.is_none() => out = Some(PathBuf::from(path)),
            extra => bail!("unexpected argument {extra}"),
        }
    }
    parsed.out = out.context("the directory for results and scores is missing")?;
    Ok(parsed)
}

pub fn main(args: &[&str]) -> anyhow::Result<()> {
    let args = parse(args).context(USAGE)?;
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(evaluate(args))
}

/// The service's own settings: its token, and the reason model to judge with.
async fn settings() -> anyhow::Result<Settings> {
    let mut warnings = Vec::new();
    let env = Settings::from_env(std::env::vars(), &mut warnings)?;
    let db = kvasir_db::Db::open(&env.db(), kvasir_db::schema::KVASIR)?;
    let rows = kvasir_config::store::load(&db).await?;
    let stored = kvasir_config::decode(rows, false, &mut warnings)?;
    let settings = env.with_stored(&stored, false, &mut warnings)?;
    for warning in warnings {
        eprintln!("{warning}");
    }
    if let Err(missing) = settings.ready() {
        bail!("set these settings first: {missing}");
    }
    Ok(settings)
}

async fn evaluate(args: Args) -> anyhow::Result<()> {
    let settings = settings().await?;
    let text = std::fs::read_to_string(&args.questions)
        .with_context(|| format!("cannot read {}", args.questions.display()))?;
    let questions: Vec<Question> = toml::from_str::<Questions>(&text)?
        .question
        .into_iter()
        .filter(|q| args.only.as_ref().is_none_or(|only| only.contains(&q.id)))
        .collect();
    std::fs::create_dir_all(&args.out)?;
    let http = reqwest::Client::new();
    for question in &questions {
        let path = args.out.join(format!("{}.json", question.id));
        if path.exists() {
            continue;
        }
        eprintln!("researching {}", question.id);
        let result = research(&http, &args, &settings.token, question).await;
        write(&path, &result)?;
    }

    let (_, tier, max_tokens) = *roles::RESEARCH
        .iter()
        .find(|(role, ..)| *role == "judge")
        .expect("the judge is a research role");
    let judge = roles::target(&settings, "judge", tier, max_tokens)?;
    let gateway = Gateway::new()?;
    let mut scores = Vec::new();
    let mut errors = Map::new();
    for question in &questions {
        let path = args.out.join(format!("{}.json", question.id));
        let result: Value = serde_json::from_str(&std::fs::read_to_string(&path)?)?;
        if let Some(error) = result.get("error") {
            errors.insert(question.id.clone(), error.clone());
            continue;
        }
        eprintln!("scoring {}", question.id);
        scores.push(score(&gateway, &judge, question, &result).await?);
    }
    write(&args.out.join("scores.json"), &json!({"scores": scores, "errors": errors}))?;
    println!("{}", summary(&scores, &errors));
    Ok(())
}

/// `value` as Python's `json.dumps(value, indent=1)` wrote it.
fn write(path: &Path, value: &Value) -> anyhow::Result<()> {
    let mut out = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
    value.serialize(&mut serde_json::Serializer::with_formatter(&mut out, formatter))?;
    std::fs::write(path, out).with_context(|| format!("cannot write {}", path.display()))
}

/// The run's result, or its error, as the service reports them.
async fn research(http: &reqwest::Client, args: &Args, token: &str, question: &Question) -> Value {
    let body = json!({
        "method": args.method,
        "input": {"question": question.question},
        "options": args.options,
    });
    let sent = http.post(format!("{}/v1/runs", args.url)).bearer_auth(token).json(&body).send();
    let mut response = match sent.await {
        Ok(response) if response.status().is_success() => response,
        Ok(response) => {
            let status = response.status().as_u16();
            return json!({"error": format!("{status}: {}", response.text().await.unwrap_or_default())});
        }
        Err(err) => return json!({"error": err.to_string()}),
    };
    let (mut buffer, mut event, mut stage) = (String::new(), String::new(), String::new());
    loop {
        let chunk = match response.chunk().await {
            Ok(Some(chunk)) => chunk,
            Ok(None) => break,
            Err(err) => return json!({"error": err.to_string()}),
        };
        buffer.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(end) = buffer.find('\n') {
            let line: String = buffer.drain(..=end).collect();
            let line = line.trim_end_matches(['\r', '\n']);
            if let Some(name) = line.strip_prefix("event: ") {
                event = name.to_owned();
            } else if let Some(data) = line.strip_prefix("data: ") {
                let Ok(data) = serde_json::from_str::<Value>(data) else { continue };
                match event.as_str() {
                    "progress" => {
                        let now = data["stage"].as_str().unwrap_or_default();
                        if now != stage {
                            stage = now.to_owned();
                            eprintln!("  {stage}");
                        }
                    }
                    "error" => return json!({"error": data["message"]}),
                    "done" => return data,
                    _ => {}
                }
            }
        }
    }
    json!({"error": "the stream ended without a result"})
}

/// The verdict of `S` and its reason. An unreadable verdict counts as a failure, and says so.
async fn ask<S: Signature>(
    gateway: &Gateway,
    judge: &Target,
    input: &S::Input,
) -> anyhow::Result<Option<S::Output>> {
    match kvasir_llm::predict::<S>(gateway, judge, input, RETRY_FOR, &NoSink).await {
        Ok(output) => Ok(Some(output)),
        Err(LlmError::Parse { .. }) => Ok(None),
        Err(err) => Err(err.into()),
    }
}

const UNREAD: &str = "the judge's verdict could not be read";

async fn score(
    gateway: &Gateway,
    judge: &Target,
    question: &Question,
    result: &Value,
) -> anyhow::Result<Score> {
    let mut failures = Vec::new();
    let report = result["report"].as_str().unwrap_or_default();
    let count = |key: &str| result[key].as_array().map_or(0, Vec::len);

    let mut met = 0;
    for requirement in &question.checks {
        let input = check::Input {
            question: question.question.clone(),
            report: report.to_owned(),
            requirement: requirement.clone(),
        };
        match ask::<check::Sig>(gateway, judge, &input).await? {
            Some(verdict) if verdict.met => met += 1,
            Some(verdict) => failures.push(format!("check: {requirement} ({})", verdict.reason)),
            None => failures.push(format!("check: {requirement} ({UNREAD})")),
        }
    }

    let (mut cited_sentences, mut supported, mut uncited) = (0, 0, 0);
    for sentence in prose(report) {
        let numbers = cited(&sentence);
        if numbers.is_empty() {
            uncited += 1;
            continue;
        }
        cited_sentences += 1;
        let evidence = evidence(result, &numbers);
        if evidence.is_empty() {
            failures.push(format!("citation: {sentence} (cites no source the run listed)"));
            continue;
        }
        let input = verify::Input { sentence: sentence.clone(), evidence };
        match ask::<verify::Sig>(gateway, judge, &input).await? {
            Some(verdict) if verdict.verdict == Support::Supported => supported += 1,
            Some(verdict) => failures.push(format!("citation: {sentence} ({})", verdict.reason)),
            None => failures.push(format!("citation: {sentence} ({UNREAD})")),
        }
    }

    Ok(Score {
        id: question.id.clone(),
        output: result["output"].as_str().unwrap_or_default().to_owned(),
        expected_output: question.output.clone(),
        checks_met: met,
        checks: question.checks.len(),
        cited_sentences,
        supported_sentences: supported,
        uncited_sentences: uncited,
        removed_by_pipeline: count("removed"),
        claims: count("claims"),
        sources: count("citations"),
        seconds: result["duration_seconds"].as_f64().unwrap_or(0.0),
        failures,
    })
}

/// The report's sentences, without headings, tables or code.
fn prose(report: &str) -> Vec<String> {
    let mut fenced = false;
    let mut found = Vec::new();
    for line in report.split('\n') {
        let stripped = line.trim();
        if stripped.starts_with("```") {
            fenced = !fenced;
            continue;
        }
        if fenced || stripped.is_empty() || stripped.starts_with(['#', '|']) {
            continue;
        }
        found.extend(sentences(stripped).iter().map(|sentence| sentence.trim().to_owned()));
    }
    found
}

/// The claims read from each cited source, with their quotes.
fn evidence(result: &Value, numbers: &[u64]) -> String {
    let none = Vec::new();
    let list = |key: &str| result[key].as_array().unwrap_or(&none);
    let urls: HashSet<&str> = list("citations")
        .iter()
        .filter(|c| c["index"].as_u64().is_some_and(|index| numbers.contains(&index)))
        .filter_map(|c| c["url"].as_str())
        .collect();
    list("claims")
        .iter()
        .filter(|claim| claim["url"].as_str().is_some_and(|url| urls.contains(url)))
        .map(|claim| {
            let text = claim["text"].as_str().unwrap_or_default();
            let quote = claim["quote"].as_str().unwrap_or_default();
            format!("- {text}\n    quote: {quote}")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn share(part: usize, whole: usize) -> String {
    if whole == 0 { "n/a".into() } else { format!("{:.0}%", part as f64 / whole as f64 * 100.0) }
}

/// A markdown table, one row per question, and the totals.
fn summary(scores: &[Score], errors: &Map<String, Value>) -> String {
    let mut rows = vec![
        "| Question | Output | Checks | Cited supported | Uncited | Removed | Sources | Minutes |"
            .to_owned(),
        "| --- | --- | --- | --- | --- | --- | --- | --- |".to_owned(),
    ];
    for s in scores {
        let output = if s.output == s.expected_output {
            s.output.clone()
        } else {
            format!("{} (not {})", s.output, s.expected_output)
        };
        rows.push(format!(
            "| {} | {output} | {}/{} | {}/{} | {} | {} | {} | {:.1} |",
            s.id,
            s.checks_met,
            s.checks,
            s.supported_sentences,
            s.cited_sentences,
            s.uncited_sentences,
            s.removed_by_pipeline,
            s.sources,
            s.seconds / 60.0
        ));
    }
    for (name, error) in errors {
        let error = error.as_str().map_or_else(|| error.to_string(), str::to_owned);
        rows.push(format!("| {name} | failed: {error} | | | | | | |"));
    }
    if scores.is_empty() {
        return rows.join("\n");
    }
    let sum = |field: fn(&Score) -> usize| scores.iter().map(field).sum::<usize>();
    let expected = scores.iter().filter(|s| s.output == s.expected_output).count();
    let minutes = scores.iter().map(|s| s.seconds).sum::<f64>() / scores.len() as f64 / 60.0;
    let totals = [
        format!("Output as expected: {}", share(expected, scores.len())),
        format!("Checks met: {}", share(sum(|s| s.checks_met), sum(|s| s.checks))),
        format!(
            "Cited sentences supported: {}",
            share(sum(|s| s.supported_sentences), sum(|s| s.cited_sentences))
        ),
        format!("Mean minutes per run: {minutes:.1}"),
        format!("Failed runs: {}", errors.len()),
    ];
    format!("{}\n\n{}", rows.join("\n"), totals.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_check_prompt_and_parse_match_dspy() {
        let mut failures = Vec::new();
        kvasir_testkit::golden::check::<check::Sig>(
            &kvasir_testkit::golden::golden(),
            &mut failures,
        );
        assert!(failures.is_empty(), "{}", failures.join("\n\n"));
    }

    #[test]
    fn the_questions_parse() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../eval/questions.toml");
        let questions = toml::from_str::<Questions>(&std::fs::read_to_string(path).unwrap());
        assert_eq!(questions.unwrap().question.len(), 20);
    }

    #[test]
    fn arguments_parse_as_python_took_them() {
        let args = parse(&["out", "--only", "a", "b", "--options", r#"{"max_searches": 8}"#]);
        let args = args.unwrap();
        assert_eq!(args.out, PathBuf::from("out"));
        assert_eq!(args.only, Some(vec!["a".to_owned(), "b".to_owned()]));
        assert_eq!(args.options, json!({"max_searches": 8}));
        assert_eq!(args.method, "ledger");
        assert!(parse(&["--options", "[]", "out"]).is_err());
        assert!(parse(&[]).is_err());
    }

    #[test]
    fn prose_leaves_out_headings_tables_and_code() {
        let report = "# Title\nOne [1]. Two.\n| a | b |\n```\nThree.\n```\n  Four [2].";
        assert_eq!(prose(report), ["One [1].", "Two.", "Four [2]."]);
    }

    #[test]
    fn the_evidence_is_the_claims_of_the_cited_sources() {
        let result = json!({
            "citations": [{"index": 1, "url": "u1"}, {"index": 2, "url": "u2"}],
            "claims": [
                {"url": "u1", "text": "t1", "quote": "q1"},
                {"url": "u2", "text": "t2", "quote": "q2"},
            ],
        });
        assert_eq!(evidence(&result, &[2]), "- t2\n    quote: q2");
        assert_eq!(evidence(&result, &[3]), "");
    }

    #[test]
    fn the_summary_totals_each_score() {
        let score = Score {
            id: "q".into(),
            output: "report".into(),
            expected_output: "table".into(),
            checks_met: 1,
            checks: 3,
            cited_sentences: 0,
            supported_sentences: 0,
            uncited_sentences: 2,
            removed_by_pipeline: 0,
            claims: 4,
            sources: 2,
            seconds: 90.0,
            failures: Vec::new(),
        };
        let mut errors = Map::new();
        errors.insert("r".into(), json!("boom"));
        let table = summary(&[score], &errors);
        assert!(table.contains("| q | report (not table) | 1/3 | 0/0 | 2 | 0 | 2 | 1.5 |"));
        assert!(table.contains("| r | failed: boom | | | | | | |"));
        assert!(table.ends_with(
            "Output as expected: 0%\nChecks met: 33%\nCited sentences supported: n/a\n\
             Mean minutes per run: 1.5\nFailed runs: 1"
        ));
    }
}
