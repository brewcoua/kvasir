//! Checking each cited sentence of a report against the quotes it cites.
//!
//! A small model cites claims that do not say what its sentence says. Each sentence that cites a
//! claim is checked against the quotes of the claims it cites, by a cascade that spends the dearer
//! models only on the sentences the cheaper ones leave in doubt:
//!
//! 1. A sentence citing no claim the research gathered is removed.
//! 2. The grounding checker, if there is one, keeps a sentence it supports.
//! 3. The fast model removes a sentence it finds unsupported. Without a checker it also keeps one
//!    it finds supported; with one, a disagreement goes on.
//! 4. The reason model has the last word: it keeps, removes, or rewrites a sentence that is only
//!    partly supported. A verdict it cannot give leaves the sentence in, marked `[unverified]`.
//! 5. A rewrite must pass the checker, or the fast model without one, or it is removed too.
//!
//! Each step runs over every sentence before the next begins: the checker and the fast model share
//! one local server, and alternating between them would load a model for each sentence. A sentence
//! that cites nothing is left alone: it is a transition, or a statement that the evidence does not
//! settle something, which is what the writer is asked to say. Tier 2 of verification.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use kvasir_llm::LlmError;
use kvasir_run::RunCtx;
use kvasir_types::results::{
    LowTrustSentence, RemovedSentence, RevisedSentence, UnverifiedSentence, VerifyStage,
};
use kvasir_types::runs::Progress;

use crate::claims::{Claim, quoted_claims};
use crate::ground::grounded;
use crate::render::MARKER;
use crate::signatures::{Support, escalate, verify};
use crate::{Tools, readable};

/// Appended to a sentence no step could judge.
pub const UNVERIFIED: &str = "[unverified]";

// The end of a sentence: its punctuation, then any markers that trail it, then a space before
// what starts a sentence. Requiring a capital keeps "e.g. this" and "2.5 GB" whole.
static END: LazyLock<fancy_regex::Regex> = LazyLock::new(|| {
    fancy_regex::Regex::new(
        r#"(?<=[.!?])((?:\s*(?:\[\d+(?:\s*,\s*\d+)*\]|\[unverified\]))*)\s+(?=[A-Z*_("“\[])"#,
    )
    .expect("a valid pattern")
});
static MARKS: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"\s*(?:\[(\d+(?:\s*,\s*\d+)*)\]|\[unverified\])").expect("a valid pattern")
});
// What a line keeps when its sentences are checked: a list bullet, a number, a quote marker.
static PREFIX: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"^(\s*(?:[-*+]|\d+\.|>)\s+)").expect("a valid pattern"));
// The first line of a callout, `> [!WARNING]` and its title.
static CALLOUT: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"^\s*> ?\[!\w+\]").expect("a valid pattern"));

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Kept,
    Rewritten,
    Removed,
    Unverified,
}

/// What the cascade decided about one sentence, and the step that decided it. `text` is the
/// sentence to keep: as given, or rewritten when `status` is `Rewritten`.
#[derive(Clone, Debug, PartialEq)]
pub struct Verdict {
    pub status: Status,
    pub text: String,
    pub stage: VerifyStage,
    pub reason: String,
}

/// Sections after verification, and what it did to their sentences.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Verified {
    pub sections: Vec<(String, String)>,
    pub removed: Vec<RemovedSentence>,
    pub revised: Vec<RevisedSentence>,
    pub unverified: Vec<UnverifiedSentence>,
}

/// `sections` with each cited sentence kept, rewritten, marked or removed by the cascade.
///
/// Every section's cited sentences go through the cascade together, so each step loads its model
/// once. Headings, tables and code are left as they are: a table row is not a sentence, and
/// cutting one would break the table.
pub async fn report(
    tools: &Tools,
    ctx: &RunCtx,
    sections: &[(String, String)],
    claims: &HashMap<u64, Claim>,
) -> Result<Verified, LlmError> {
    let mut items: Vec<(String, Vec<Claim>)> = Vec::new();
    for (_, text) in sections {
        edit(text, |sentence| {
            let mut ids = cited(sentence);
            let mut seen = HashSet::new();
            ids.retain(|id| seen.insert(*id));
            let found = ids.iter().filter_map(|id| claims.get(id)).cloned().collect();
            items.push((sentence.to_owned(), found));
            sentence.to_owned()
        });
    }
    let total = items.len();
    let mut verdicts = cascade(tools, ctx, &items, kvasir_run::stages::VERIFY).await?.into_iter();
    let mut done = Verified::default();
    for (title, text) in sections {
        let edited = edit(text, |sentence| {
            let verdict = verdicts.next().expect("one verdict per sentence");
            let section = title.clone();
            match verdict.status {
                Status::Removed => {
                    done.removed.push(RemovedSentence {
                        section,
                        sentence: sentence.to_owned(),
                        reason: verdict.reason,
                        stage: verdict.stage,
                    });
                    String::new()
                }
                Status::Rewritten => {
                    done.revised.push(RevisedSentence {
                        section,
                        before: sentence.to_owned(),
                        after: verdict.text.clone(),
                        reason: verdict.reason,
                    });
                    verdict.text
                }
                Status::Unverified => {
                    done.unverified.push(UnverifiedSentence {
                        section,
                        sentence: sentence.to_owned(),
                        reason: verdict.reason,
                    });
                    format!("{} {UNVERIFIED}", verdict.text)
                }
                Status::Kept => verdict.text,
            }
        });
        done.sections.push((title.clone(), edited));
    }
    let detail = format!("removed {} of {total} cited sentences", done.removed.len());
    ctx.publish(Progress {
        done: Some(total as u64),
        total: Some(total as u64),
        ..Progress::new(kvasir_run::stages::VERIFY, detail)
    });
    Ok(done)
}

/// The cited sentences of `sections` whose claims all come from sites of low trust.
pub fn low_trust(
    sections: &[(String, String)],
    claims: &HashMap<u64, Claim>,
    low: &HashSet<u64>,
) -> Vec<LowTrustSentence> {
    let mut found = Vec::new();
    for (title, text) in sections {
        edit(text, |sentence| {
            let ids: HashSet<u64> =
                cited(sentence).into_iter().filter(|id| claims.contains_key(id)).collect();
            if !ids.is_empty() && ids.is_subset(low) {
                found.push(LowTrustSentence {
                    section: title.clone(),
                    sentence: sentence.to_owned(),
                });
            }
            sentence.to_owned()
        });
    }
    found
}

/// A verdict on each sentence against the claims it cites, in order.
///
/// A sentence with no claims to check against cites nothing the research gathered. Without a
/// grounding checker (`ground`), the fast model is the first to judge. Progress is published under
/// `stage`, for a caller that runs the cascade outside verification.
pub async fn cascade(
    tools: &Tools,
    ctx: &RunCtx,
    items: &[(String, Vec<Claim>)],
    stage: &str,
) -> Result<Vec<Verdict>, LlmError> {
    let total = items.len();
    let mut verdicts: HashMap<usize, Verdict> = HashMap::new();
    let checker = tools.targets.get("ground");

    let settle = |verdicts: &mut HashMap<usize, Verdict>,
                  i: usize,
                  status: Status,
                  decider: VerifyStage,
                  reason: String,
                  text: Option<String>| {
        let text = text.unwrap_or_else(|| items[i].0.clone());
        verdicts.insert(i, Verdict { status, text, stage: decider, reason });
        let settled = verdicts.len() as u64;
        ctx.publish(Progress {
            done: Some(settled),
            total: Some(total as u64),
            series: Some("sentences".into()),
            ..Progress::new(stage, format!("checked {settled} of {total} cited sentences"))
        });
    };
    let begin = |step: &str, pending: usize| {
        ctx.publish(Progress::new(
            stage,
            format!("{step}: checking {pending} of {total} sentences"),
        ));
    };
    // Bold marks a key term for the reader, and is no part of what is checked.
    let claim_of = |i: usize| items[i].0.replace("**", "");
    let ask_verify = async |sentence: String, evidence: &[Claim]| {
        let input = verify::Input { sentence, evidence: quoted_claims(evidence) };
        readable(tools.predict::<verify::Sig>(ctx, "verify", &input).await)
    };
    let holds = async |i: usize, sentence: String| -> Result<bool, LlmError> {
        let evidence = &items[i].1;
        if let Some(checker) = checker {
            let document: Vec<String> =
                evidence.iter().map(|claim| format!("{}\n{}", claim.text, claim.quote)).collect();
            return grounded(tools, ctx, checker, &document.join("\n\n"), &unmarked(&sentence))
                .await;
        }
        let answer = ask_verify(sentence, evidence).await?;
        Ok(answer.is_some_and(|a| a.verdict == Support::Supported))
    };

    ctx.publish(Progress {
        done: Some(0),
        total: Some(total as u64),
        series: Some("sentences".into()),
        ..Progress::new(stage, format!("checking {total} cited sentences against their quotes"))
    });
    let mut pending = Vec::new();
    for (i, (_, evidence)) in items.iter().enumerate() {
        if evidence.is_empty() {
            let reason = "cites no claim the research gathered".to_owned();
            settle(&mut verdicts, i, Status::Removed, VerifyStage::Citation, reason, None);
        } else {
            pending.push(i);
        }
    }

    if checker.is_some() && !pending.is_empty() {
        begin("checker", pending.len());
        let mut unsupported = Vec::new();
        for i in pending {
            if holds(i, claim_of(i)).await? {
                settle(&mut verdicts, i, Status::Kept, VerifyStage::Checker, String::new(), None);
            } else {
                unsupported.push(i);
            }
        }
        pending = unsupported;
    }

    if !pending.is_empty() {
        begin("fast model", pending.len());
    }
    let mut doubtful = Vec::new();
    for i in pending {
        let answer = ask_verify(claim_of(i), &items[i].1).await?;
        // A verdict that cannot be read says nothing about the sentence, so it counts as partial.
        match answer {
            Some(a) if a.verdict == Support::Unsupported => {
                settle(&mut verdicts, i, Status::Removed, VerifyStage::Verify, a.reason, None);
            }
            Some(a) if a.verdict == Support::Supported && checker.is_none() => {
                settle(&mut verdicts, i, Status::Kept, VerifyStage::Verify, String::new(), None);
            }
            _ => doubtful.push(i),
        }
    }

    if !doubtful.is_empty() {
        begin("reason model", doubtful.len());
    }
    let mut rewrites: Vec<(usize, String, String)> = Vec::new();
    for i in doubtful {
        let input = escalate::Input { sentence: claim_of(i), evidence: quoted_claims(&items[i].1) };
        let answer = readable(tools.predict::<escalate::Sig>(ctx, "escalate", &input).await)?;
        match answer {
            None => {
                let reason = "the reason model's verdict could not be read".to_owned();
                settle(&mut verdicts, i, Status::Unverified, VerifyStage::Escalate, reason, None);
            }
            Some(a) if a.verdict == Support::Supported => {
                settle(&mut verdicts, i, Status::Kept, VerifyStage::Escalate, String::new(), None);
            }
            Some(a) if a.verdict == Support::Unsupported => {
                settle(&mut verdicts, i, Status::Removed, VerifyStage::Escalate, a.reason, None);
            }
            Some(a) => rewrites.push((i, a.rewrite.trim().to_owned(), a.reason)),
        }
    }

    if !rewrites.is_empty() {
        begin("rewrites", rewrites.len());
    }
    for (i, rewrite, reason) in rewrites {
        let allowed: HashSet<u64> = items[i].1.iter().map(|claim| claim.id).collect();
        let removed = |verdicts: &mut HashMap<usize, Verdict>, reason: String| {
            settle(verdicts, i, Status::Removed, VerifyStage::Rewrite, reason, None);
        };
        if rewrite.is_empty() {
            removed(&mut verdicts, format!("the reason model gave no rewrite ({reason})"));
        } else if cited(&rewrite).is_empty() {
            // Kept, an uncited sentence would be left alone as a transition, never checked.
            removed(&mut verdicts, "the rewrite cites no claim".into());
        } else if cited(&rewrite).iter().any(|id| !allowed.contains(id)) {
            removed(&mut verdicts, "the rewrite cites a claim the sentence did not".into());
        } else if holds(i, rewrite.replace("**", "")).await? {
            settle(
                &mut verdicts,
                i,
                Status::Rewritten,
                VerifyStage::Rewrite,
                reason,
                Some(rewrite),
            );
        } else {
            removed(&mut verdicts, format!("the rewrite is not supported either ({reason})"));
        }
    }
    Ok((0..total).map(|i| verdicts.remove(&i).expect("every sentence settled")).collect())
}

/// `line` cut into sentences, each keeping the markers that follow its full stop.
pub fn sentences(line: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut start = 0;
    for found in END.captures_iter(line).filter_map(Result::ok) {
        let marks = found.get(1).expect("the group always takes part");
        parts.push(line[start..marks.end()].to_owned());
        start = found.get(0).expect("the match").end();
    }
    parts.push(line[start..].to_owned());
    parts.retain(|part| !part.trim().is_empty());
    parts
}

/// The claim ids `sentence` cites, in order, repeats included.
pub fn cited(sentence: &str) -> Vec<u64> {
    MARKER
        .captures_iter(sentence)
        .flat_map(|caps| {
            caps[1].split(',').filter_map(|id| id.trim().parse().ok()).collect::<Vec<_>>()
        })
        .collect()
}

/// `sentence` without its claim markers and its `[unverified]` mark.
pub fn unmarked(sentence: &str) -> String {
    MARKS.replace_all(sentence, "").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `text` with each cited sentence replaced by what `decide` returns for it, in order.
///
/// A sentence `decide` returns nothing for is removed. A list item with nothing left goes
/// entirely, rather than leaving an empty bullet, and a callout with every sentence removed goes
/// too, rather than leaving its title alone.
pub fn edit(text: &str, mut decide: impl FnMut(&str) -> String) -> String {
    let mut edited: Vec<String> = Vec::new();
    let mut fenced = false;
    for line in text.split('\n') {
        let start = line.trim_start();
        if start.starts_with("```") {
            fenced = !fenced;
        }
        if fenced
            || start.starts_with("```")
            || start.starts_with('#')
            || start.starts_with('|')
            || !MARKER.is_match(line)
        {
            edited.push(line.to_owned());
            continue;
        }
        let prefix = PREFIX.captures(line).map_or("", |caps| caps.get(1).expect("group").as_str());
        let mut kept = Vec::new();
        for sentence in sentences(&line[prefix.len()..]) {
            if cited(&sentence).is_empty() {
                kept.push(sentence);
                continue;
            }
            let replacement = decide(sentence.trim());
            if !replacement.is_empty() {
                kept.push(replacement);
            }
        }
        if !kept.is_empty() {
            let joined: Vec<&str> = kept.iter().map(|part| part.trim()).collect();
            edited.push(format!("{prefix}{}", joined.join(" ")));
        }
    }
    let after = edited.iter().skip(1).map(String::as_str).chain([""]);
    edited
        .iter()
        .zip(after)
        .filter(|(line, after)| !CALLOUT.is_match(line) || after.trim_start().starts_with('>'))
        .map(|(line, _)| line.as_str())
        .collect::<Vec<_>>()
        .join("\n")
}
