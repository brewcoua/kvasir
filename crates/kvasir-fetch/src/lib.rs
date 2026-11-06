//! Reading a page from the web: who may be asked and how often, what is kept, and what a page that
//! a plain request cannot read falls back to.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use kvasir_extract::{Link, Unusable};

mod guard;
mod polite;
mod robots;
mod store;
mod web;

pub use guard::{Guard, GuardedResolver, Lookup, is_public, system_lookup};
pub use polite::{HOST_CONCURRENCY, Politeness, Slot};
pub use robots::Robots;
pub use store::{Cached, Found, PageStore, Row, Stats, domain, page_id};
pub use web::{Web, retry_after};

/// A read that stalls this long has failed.
pub const TIMEOUT: Duration = Duration::from_secs(20);
/// A download stops here. A PDF cut short does not parse, and falls back to its snippet like any
/// other unreadable page.
pub const MAX_BYTES: usize = 20_000_000;

/// Unix seconds, now.
pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// How a page was read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Engine {
    Static,
    Render,
    Stealth,
}

impl Engine {
    pub fn name(self) -> &'static str {
        match self {
            Self::Static => "static",
            Self::Render => "render",
            Self::Stealth => "stealth",
        }
    }

    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "static" => Some(Self::Static),
            "render" => Some(Self::Render),
            "stealth" => Some(Self::Stealth),
            _ => None,
        }
    }
}

/// A page read: its text, and the links found before any of the page was dropped.
#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    pub url: String,
    pub final_url: String,
    pub title: String,
    pub text: String,
    pub engine: Engine,
    pub links: Vec<Link>,
    /// Unix seconds when the page was read from its site, which a cached page keeps.
    pub fetched_at: f64,
}

/// The page could not be read as text.
#[derive(Clone, Debug, thiserror::Error)]
pub enum FetchError {
    #[error("{0}")]
    Failed(String),
    /// A static page that holds no article.
    #[error(transparent)]
    Unusable(#[from] Unusable),
}

/// What can change while runs fetch: what each host was last asked, and any wait it asked for, is
/// kept.
#[derive(Clone, Debug, PartialEq)]
pub struct Options {
    /// Named in the user agent, or empty.
    pub contact: String,
    pub host_interval: Duration,
    /// Whether a page that needs JavaScript is rendered in a browser.
    pub render: bool,
    /// Whether rendering goes through the stealth browser.
    pub stealth: bool,
    /// Pages under this age are served from the cache.
    pub max_age_hours: u32,
}
