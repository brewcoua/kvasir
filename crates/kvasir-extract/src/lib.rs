#![doc = include_str!("../README.md")]

mod check;
mod html;
mod normalize;
pub mod pdf;
mod split;

pub use check::{Reason, Unusable, check_page, too_deep};
pub use html::{INTERACTIVE, Link, Region, Static, extract, read_html};
pub use normalize::{BadUrl, normalize};
pub use split::split;

/// A page past this is a dump rather than an article, and parsing it costs more than it gives.
pub const MAX_CHARACTERS: usize = 1_000_000;
/// About 1,500 tokens: room for the notes, the instructions and the answer in a 16k context.
pub const PIECE_CHARACTERS: usize = 6000;

/// The first `count` characters of `text`.
pub fn head(text: &str, count: usize) -> &str {
    match text.char_indices().nth(count) {
        Some((at, _)) => &text[..at],
        None => text,
    }
}
