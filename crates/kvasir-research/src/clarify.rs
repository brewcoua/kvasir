//! The questions worth asking before a run starts. One fast-model call, outside any run.

use kvasir_llm::{Llm, LlmError, NoSink, TASK_RETRY_FOR, Target, predict};

use crate::signatures::clarify;

/// At most this many questions are asked.
const MAX_QUESTIONS: usize = 3;

/// Questions worth asking before researching `question`, or none. `target` is the `clarify` role.
/// It retries as briefly as a client's housekeeping call does: the user is waiting on it.
pub async fn clarify(
    llm: &dyn Llm,
    target: &Target,
    question: &str,
) -> Result<Vec<String>, LlmError> {
    let input = clarify::Input { question: question.to_owned() };
    let mut questions =
        predict::<clarify::Sig>(llm, target, &input, TASK_RETRY_FOR, &NoSink).await?.questions;
    questions.truncate(MAX_QUESTIONS);
    Ok(questions)
}
