#![doc = include_str!("../README.md")]

mod ctx;
mod registry;
mod run;
mod slots;
mod store;
mod usage;

pub use ctx::{RunCtx, model_key};
pub use registry::{NotFound, RunRegistry};
pub use run::{Event, Run, STATUS_CALLS, State};
pub use slots::{Slot, Slots};
pub use store::{INTERRUPTED, RunStore};

/// The stages a run reports its progress under: STORM's four, then those of the research methods.
pub mod stages {
    pub const RESEARCH: &str = "research";
    pub const OUTLINE: &str = "outline";
    pub const ARTICLE: &str = "article";
    pub const POLISH: &str = "polish";

    pub const BRIEF: &str = "brief";
    pub const BACKGROUND: &str = "background";
    pub const ASSESS: &str = "assess";
    pub const WRITE: &str = "write";
    pub const VERIFY: &str = "verify";
}

/// `x` rounded to `digits` decimals as Python's `round` does: on the exact binary value, ties to
/// even.
pub(crate) fn round(x: f64, digits: usize) -> f64 {
    format!("{x:.digits$}").parse().unwrap_or(x)
}
