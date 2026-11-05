//! What the service is doing, and what it cost.
//!
//! A run is minutes of work behind a single streaming response, so a client that disconnects, or
//! one that never started the run, has no way to find out how it is going. The registry is that
//! record. It lives in memory; with `runs.store` set to sqlite each run is also written to a
//! [`RunStore`] when it starts and when it ends, which is history for debugging rather than state.

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
