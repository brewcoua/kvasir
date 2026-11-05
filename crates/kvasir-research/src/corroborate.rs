//! Which claims other sites agree with, and which they contradict: tiers 4 and 5 of verification.
//!
//! A quote on its page makes a claim one site's word. Another site saying the same makes it more.
//! Each claim is compared only with the claims closest to it in meaning from other sites, since
//! comparing every pair would cost a call for each of the tens of thousands of pairs a run holds.

use std::collections::HashSet;

use futures::{StreamExt, stream};
use indexmap::IndexMap;
use kvasir_fetch::domain;
use kvasir_llm::{LlmError, Sink};
use kvasir_run::RunCtx;
use kvasir_run::stages::VERIFY;
use kvasir_types::runs::Progress;

use crate::claims::{Claim, best_first, quoted_claims};
use crate::encoder::{Encoder, cosine};
use crate::ground::grounded;
use crate::signatures::{Support, contradicts, verify};
use crate::{Tools, readable};

/// Claims from other sites each claim is compared with, at most.
const NEIGHBOURS: usize = 3;
/// Below this cosine similarity, two claims are taken to be about different things.
const SAME_FACT: f64 = 0.8;
/// Words a quote needs before another quote holding it counts as a copy: a figure or a name quoted
/// by two sites is two sources.
const COPY_WORDS: usize = 8;

/// The domain a site is registered under, so `blog.a.org` and `a.org` are one source; the host
/// with its port when it has none, as an IP address or `localhost`.
pub fn registrable(url: &str) -> String {
    let host = url::Url::parse(url).ok().and_then(|u| u.host_str().map(str::to_owned));
    if let Some(host) = host
        && let Some(found) = psl::domain(host.as_bytes()).filter(|d| d.suffix().is_known())
    {
        return host[host.len() - found.as_bytes().len()..].to_owned();
    }
    domain(url)
}

/// Whether the quotes of `claim` and `other` are one text, or one of `COPY_WORDS` words or more
/// holds the other.
pub fn copies(claim: &Claim, other: &Claim) -> bool {
    let folded =
        |c: &Claim| c.quote.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ");
    let (mut a, mut b) = (folded(claim), folded(other));
    if a.chars().count() > b.chars().count() {
        std::mem::swap(&mut a, &mut b);
    }
    !a.is_empty() && (a == b || (a.split(' ').count() >= COPY_WORDS && b.contains(&a)))
}

/// Whether `other` is a second source for `claim`: another registrable domain, and not a copy of
/// its quote.
pub fn independent(claim: &Claim, other: &Claim) -> bool {
    registrable(&claim.url) != registrable(&other.url) && !copies(claim, other)
}

fn publish(ctx: &RunCtx, detail: String, done: usize, total: usize, series: Option<&str>) {
    ctx.publish(Progress {
        done: Some(done as u64),
        total: Some(total as u64),
        series: series.map(str::to_owned),
        ..Progress::new(VERIFY, detail)
    });
}

/// Fills each claim's `corroborated_by` when `corroborate`, and `contradicted_by` when
/// `contradict`, from the claims of other sites closest to it.
///
/// With a grounding checker (`ground`), it decides support; otherwise the `verify` model does.
/// Support is decided for every pair before any is asked about disagreeing: the checker and the
/// `contradict` model share one local server, and alternating between them would load a model for
/// each pair. Two claims either of which supports the other are not asked about disagreeing.
pub async fn cross_check(
    tools: &Tools,
    ctx: &RunCtx,
    claims: &mut [Claim],
    corroborate: bool,
    contradict: bool,
) -> Result<(), LlmError> {
    if !(corroborate || contradict) {
        return Ok(());
    }
    let pairs = neighbours(&tools.encoder, claims, ctx).await?;
    if pairs.is_empty() {
        return Ok(());
    }
    // Merged into it during the research already.
    let mut agree: HashSet<(u64, u64)> = pairs
        .iter()
        .filter(|&&(i, j)| claims[i].corroborated_by.contains(&claims[j].id))
        .map(|&(i, j)| (claims[i].id, claims[j].id))
        .collect();
    if corroborate {
        let asked: Vec<(usize, usize)> = pairs
            .iter()
            .copied()
            .filter(|&(i, j)| !agree.contains(&(claims[i].id, claims[j].id)))
            .collect();
        let total = asked.len();
        let detail = format!("checking {total} pairs of claims from different sites for support");
        publish(ctx, detail, 0, total, Some("support"));
        for (done, (i, j)) in asked.into_iter().enumerate() {
            if supports(tools, ctx, &claims[j], &claims[i]).await? {
                let other = claims[j].id;
                claims[i].corroborated_by.push(other);
                agree.insert((claims[i].id, other));
            }
            let detail = format!("checked {} of {total} pairs for support", done + 1);
            publish(ctx, detail, done + 1, total, Some("support"));
        }
    }
    if contradict {
        contradictions(tools, ctx, claims, &pairs, &agree).await?;
    }
    let corroborated = claims.iter().filter(|c| !c.corroborated_by.is_empty()).count();
    let contradicted = claims.iter().filter(|c| !c.contradicted_by.is_empty()).count();
    let detail = format!("{corroborated} claims corroborated, {contradicted} contradicted");
    publish(ctx, detail, pairs.len(), pairs.len(), None);
    Ok(())
}

/// Fills `contradicted_by`, both ways, for the pairs not in `agree` either way round.
///
/// Disagreeing is symmetric, so each pair is asked once. A claim is asked about all its pairs in
/// one call, which cuts the calls about threefold, and the calls run side by side.
async fn contradictions(
    tools: &Tools,
    ctx: &RunCtx,
    claims: &mut [Claim],
    pairs: &[(usize, usize)],
    agree: &HashSet<(u64, u64)>,
) -> Result<(), LlmError> {
    let mut groups: IndexMap<usize, Vec<usize>> = IndexMap::new();
    let mut seen: HashSet<(u64, u64)> = HashSet::new();
    for &(i, j) in pairs {
        let (a, b) = (claims[i].id, claims[j].id);
        if seen.contains(&(a.min(b), a.max(b)))
            || agree.contains(&(a, b))
            || agree.contains(&(b, a))
        {
            continue;
        }
        seen.insert((a.min(b), a.max(b)));
        groups.entry(i).or_default().push(j);
    }
    let total = seen.len();
    let detail = format!("comparing {total} pairs of claims for disagreement");
    publish(ctx, detail, 0, total, Some("disagreement"));
    // Made before any runs, so each call names the event above, as a pool task would.
    let asked: Vec<_> = groups
        .iter()
        .map(|(&i, others)| {
            let ctx = ctx.clone();
            let input = contradicts::Input {
                claim: quoted_claims([&claims[i]]),
                others: quoted_claims(others.iter().map(|&j| &claims[j])),
            };
            async move {
                let found = tools.predict::<contradicts::Sig>(&ctx, "contradict", &input).await;
                readable(found)
            }
        })
        .collect();
    let mut answers = stream::iter(asked).buffered(tools.max_threads.max(1));
    let mut done = 0;
    for (&i, others) in &groups {
        let answer = answers.next().await.expect("one answer per group")?;
        // Only ids it was shown: a small model otherwise cites claims from its own numbering.
        let disagree = answer.map(|a| a.disagree).unwrap_or_default();
        for &j in others {
            if disagree.iter().any(|&id| u64::try_from(id) == Ok(claims[j].id)) {
                let (a, b) = (claims[i].id, claims[j].id);
                claims[i].contradicted_by.push(b);
                claims[j].contradicted_by.push(a);
            }
        }
        done += others.len();
        let detail = format!("compared {done} of {total} pairs for disagreement");
        publish(ctx, detail, done, total, Some("disagreement"));
    }
    Ok(())
}

/// Each claim, by index, with up to `NEIGHBOURS` claims from other sites about the same thing.
pub async fn neighbours(
    encoder: &Encoder,
    claims: &[Claim],
    sink: &dyn Sink,
) -> Result<Vec<(usize, usize)>, LlmError> {
    if claims.len() < 2 {
        return Ok(Vec::new());
    }
    let texts: Vec<String> = claims.iter().map(|claim| claim.text.clone()).collect();
    let vectors = encoder.encode(&texts, false, sink).await?;
    let mut pairs = Vec::new();
    for (i, claim) in claims.iter().enumerate() {
        let similarity: Vec<f64> = vectors.iter().map(|v| cosine(&vectors[i], v)).collect();
        let close = best_first(&similarity)
            .into_iter()
            .filter(|&j| similarity[j] >= SAME_FACT && independent(claim, &claims[j]))
            .take(NEIGHBOURS);
        pairs.extend(close.map(|j| (i, j)));
    }
    Ok(pairs)
}

async fn supports(
    tools: &Tools,
    ctx: &RunCtx,
    other: &Claim,
    claim: &Claim,
) -> Result<bool, LlmError> {
    if let Some(checker) = tools.targets.get("ground") {
        let document = format!("{}\n{}", other.text, other.quote);
        return grounded(tools, ctx, checker, &document, &claim.text).await;
    }
    let input = verify::Input { sentence: claim.text.clone(), evidence: quoted_claims([other]) };
    let verdict = readable(tools.predict::<verify::Sig>(ctx, "verify", &input).await)?;
    Ok(verdict.is_some_and(|v| v.verdict == Support::Supported))
}
