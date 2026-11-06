//! The runs page, built from `web/` by `pnpm build`, which the image does and a checkout has to.
//! Nothing it loads comes from another origin.

use axum::extract::Path;
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};

// Read from disk in a debug build, and built into the binary in a release one. Not required at
// build time, so the service still builds from a checkout where the page is not built.
#[derive(rust_embed::Embed)]
#[folder = "../../src/kvasir/static"]
#[allow_missing = true]
struct Built;

pub async fn index() -> Response {
    match Built::get("index.html") {
        Some(file) => ([(header::CONTENT_TYPE, "text/html")], file.data).into_response(),
        None => (
            StatusCode::SERVICE_UNAVAILABLE,
            "The runs page is not built. Run `pnpm build` in web/.",
        )
            .into_response(),
    }
}

pub async fn asset(Path(path): Path<String>) -> Response {
    match Built::get(&format!("assets/{path}")) {
        Some(file) => ([(header::CONTENT_TYPE, file.metadata.mimetype().to_owned())], file.data)
            .into_response(),
        None => crate::detail(StatusCode::NOT_FOUND, "Not Found"),
    }
}
