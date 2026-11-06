#![doc = include_str!("../README.md")]

use std::future::Future;
use std::pin::Pin;

#[cfg(feature = "chromium")]
mod chromium;
#[cfg(feature = "chromium")]
mod tab;
#[cfg(feature = "chromium")]
pub use chromium::{Allow, Chromium};
#[cfg(feature = "chromium")]
pub use tab::{Input, Tab};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// A page after its scripts have run, with what a reader would not read removed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rendered {
    pub html: String,
    pub final_url: String,
    pub status: u16,
}

#[derive(Clone, Debug, thiserror::Error)]
pub enum RenderError {
    /// No browser to render with, so the static page's failure stands.
    #[error("{0}")]
    Unavailable(String),
    /// Whatever the browser hit on this page.
    #[error("{0}")]
    Failed(String),
}

/// What can render a page. Tests stand in for the browser here.
pub trait Render: Send + Sync {
    fn render<'a>(&'a self, url: &'a str) -> BoxFuture<'a, Result<Rendered, RenderError>>;
}
