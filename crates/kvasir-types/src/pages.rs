//! The page cache as the knowledge page shows it.

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

use crate::sites::SourceInfo;

/// One domain with pages cached.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Site {
    pub domain: String,
    pub pages: u64,
    pub pieces: u64,
    /// Unix seconds of the latest fetch among its pages.
    pub fetched_at: f64,
    pub engines: Vec<String>,
    #[serde(default)]
    pub source: Option<SourceInfo>,
}

/// One cached page, without its text.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PageSummary {
    pub id: String,
    pub url: String,
    pub title: String,
    pub engine: String,
    pub fetched_at: f64,
    pub pieces: u64,
    /// Characters of the extracted text.
    pub length: u64,
}

/// A link of a page, as kept for a run to follow.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PageLead {
    pub url: String,
    pub anchor: String,
    pub context: String,
}

/// A stored run whose result cites the page.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PageRun {
    pub id: String,
    pub topic: String,
}

/// One cached page with the text intake reads.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PageDetail {
    pub id: String,
    pub url: String,
    pub final_url: String,
    pub title: String,
    pub engine: String,
    pub fetched_at: f64,
    /// Markdown, as extracted.
    pub text: String,
    /// By region of the page: main, related, nav or footer.
    pub leads: IndexMap<String, Vec<PageLead>>,
    pub runs: Vec<PageRun>,
}

/// A cached piece scored against a question.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RecallPiece {
    pub page: String,
    pub url: String,
    pub title: String,
    pub site: String,
    pub text: String,
    pub similarity: f64,
    /// Whether it reaches the recall threshold.
    pub clears: bool,
}

/// The pieces recall would take for a question, best first, with the threshold it applies.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Recall {
    pub minimum: f64,
    pub pieces: Vec<RecallPiece>,
}

/// What the page cache holds and what it takes on disk.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CacheStats {
    pub pages: u64,
    pub links: u64,
    pub pieces: u64,
    pub text_bytes: u64,
    pub vector_bytes: u64,
    /// The database file with its -wal and -shm files.
    pub file_bytes: u64,
    pub path: String,
    pub oldest: Option<f64>,
    pub newest: Option<f64>,
}

/// One cached site in the link graph.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SiteNode {
    pub domain: String,
    pub pages: u64,
    /// Links stored on its pages, wherever they lead.
    pub links: u64,
    #[serde(default)]
    pub source: Option<SourceInfo>,
}

/// Links from the pages of one cached site to the pages of another.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SiteEdge {
    pub source: String,
    pub target: String,
    pub count: u64,
}

/// The cached sites and the links between them.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SiteGraph {
    pub sites: Vec<SiteNode>,
    pub edges: Vec<SiteEdge>,
}
