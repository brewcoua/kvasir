//! Chromium, driven over the DevTools protocol.
//!
//! The usual tells of an automated browser are removed: `Runtime.enable`, which a page can time
//! through `console.debug`, is turned off before navigating, and scripts run in an isolated world.
//! With `stealth`, the launch flags that name automation go too. None of it changes a fingerprint.

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Duration;

use chromiumoxide::browser::{Browser, BrowserConfig};
use chromiumoxide::cdp::browser_protocol::browser::BrowserContextId;
use chromiumoxide::cdp::browser_protocol::fetch::{
    ContinueRequestParams, EnableParams as FetchEnable, EventRequestPaused, FailRequestParams,
};
use chromiumoxide::cdp::browser_protocol::network::{
    ErrorReason, EventResponseReceived, ResourceType, SetBypassServiceWorkerParams,
};
use chromiumoxide::cdp::browser_protocol::page::{CreateIsolatedWorldParams, EventLifecycleEvent};
use chromiumoxide::cdp::browser_protocol::target::CreateTargetParams;
use chromiumoxide::cdp::js_protocol::runtime::{DisableParams, EvaluateParams};
use chromiumoxide::handler::viewport::Viewport;
use chromiumoxide::page::Page;
use futures::StreamExt;
use tokio::sync::{Mutex, Semaphore};
use tokio::task::JoinHandle;
use tokio::time::timeout;
use url::Url;

use crate::{BoxFuture, Render, RenderError, Rendered};

const LOAD_TIMEOUT: Duration = Duration::from_secs(15);
// Pages that keep a connection open never go idle, so the wait for it is short and a timeout is
// not an error.
const IDLE_TIMEOUT: Duration = Duration::from_secs(2);
// Longer than a page's own timeouts add up to; past this the render is given up.
const RENDER_TIMEOUT: Duration = Duration::from_secs(45);
// Pages open in the browser at once, whatever the number of hosts.
const MAX_PAGES: usize = 2;
// Hosts whose context, and so whose cookies, the process keeps; the oldest is closed past this. A
// context holds its own cache and storage, so one per host ever seen would grow without end.
const MAX_CONTEXTS: usize = 16;
// From web-som: an element covering more of the viewport than this is a wall or an overlay.
const MAX_COVER_RATIO: f64 = 0.8;
// Firecrawl's AD_SERVING_DOMAINS, matched as a substring of the host as it does.
const AD_HOSTS: [&str; 13] = [
    "doubleclick.net",
    "adservice.google.com",
    "googlesyndication.com",
    "googletagservices.com",
    "googletagmanager.com",
    "google-analytics.com",
    "adsystem.com",
    "adservice.com",
    "adnxs.com",
    "ads-twitter.com",
    "facebook.net",
    "fbcdn.net",
    "amazon-adsystem.com",
];
// Flags without the ones that name automation: `--enable-automation`, and those Patchright drops.
// Site isolation is off because cross-origin frames would otherwise attach as targets of their own,
// where neither the request filter below nor `Runtime.disable` reaches.
const STEALTH_ARGS: [&str; 16] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-dev-shm-usage",
    "--disable-features=TranslateUI",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
];
const ISOLATION_OFF: &str = "--disable-site-isolation-trials";

// It removes what a reader would not read, in one pass over the page. Hidden by style, zero size,
// aria-hidden, a fixed or sticky element covering most of the viewport, and controls. web-som's
// occlusion test (is another element drawn over this one's sample points) is left out: it calls
// elementFromPoint for a grid of points per element, which on a long page costs seconds, and the
// fixed-element rule already catches the overlays that matter.
const CLEAN: &str = r#"
(args) => {
  const [selectors, maxCover] = args;
  const area = innerWidth * innerHeight;
  const drop = new Set(document.body.querySelectorAll(selectors));
  for (const el of document.body.querySelectorAll("*")) {
    const style = getComputedStyle(el);
    if (style.display === "contents" || el.tagName === "BR" || el.tagName === "WBR") continue;
    const rect = el.getBoundingClientRect();
    // Only a leaf is dropped for its size: a wrapper with no height may still hold floats.
    const empty = el.childElementCount === 0 && (rect.width === 0 || rect.height === 0);
    const covers = (style.position === "fixed" || style.position === "sticky")
      && rect.width * rect.height > maxCover * area;
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0"
        || empty || covers || el.getAttribute("aria-hidden") === "true") drop.add(el);
  }
  for (const el of drop) el.remove();
  return document.documentElement.outerHTML;
}
"#;

/// Whether a page may request a URL. A page's own scripts choose what it requests, so this stands
/// between them and this service's network.
pub type Allow = Arc<dyn Fn(String) -> BoxFuture<'static, bool> + Send + Sync>;

struct Running {
    // Its own, for a stale lock left by a browser that died would stop the next from starting.
    _profile: tempfile::TempDir,
    browser: Browser,
    pump: JoinHandle<()>,
    contexts: VecDeque<(String, BrowserContextId)>,
}

impl Drop for Running {
    fn drop(&mut self) {
        self.pump.abort();
    }
}

/// One browser, started on the first render and started again if it dies.
pub struct Chromium {
    stealth: bool,
    allow: Allow,
    running: Mutex<Option<Running>>,
    // Set once the browser could not start, so later renders fail without trying again.
    unavailable: std::sync::Mutex<Option<String>>,
    pages: Semaphore,
}

impl Chromium {
    pub fn new(stealth: bool, allow: Allow) -> Self {
        Self {
            stealth,
            allow,
            running: Mutex::new(None),
            unavailable: std::sync::Mutex::new(None),
            pages: Semaphore::new(MAX_PAGES),
        }
    }

    async fn start(&self) -> Result<Running, RenderError> {
        let viewport = Viewport {
            width: 1280,
            height: 720,
            device_scale_factor: Some(1.0),
            emulating_mobile: false,
            is_landscape: false,
            has_touch: false,
        };
        let profile = tempfile::tempdir()
            .map_err(|e| RenderError::Unavailable(format!("cannot render: {e}")))?;
        let mut config = BrowserConfig::builder()
            .user_data_dir(profile.path())
            .new_headless_mode()
            .no_sandbox()
            .viewport(viewport)
            .arg(ISOLATION_OFF);
        if self.stealth {
            config = config.disable_default_args().args(STEALTH_ARGS);
        }
        let unavailable = |reason: String| {
            let reason = format!("cannot render: {reason}");
            *self.unavailable.lock().unwrap_or_else(|e| e.into_inner()) = Some(reason.clone());
            RenderError::Unavailable(reason)
        };
        let config = config.build().map_err(unavailable)?;
        let (browser, mut handler) =
            Browser::launch(config).await.map_err(|e| unavailable(e.to_string()))?;
        let pump = tokio::spawn(async move { while handler.next().await.is_some() {} });
        Ok(Running { _profile: profile, browser, pump, contexts: VecDeque::new() })
    }

    /// A new tab in the host's context, which the process keeps so its cookies persist.
    async fn new_page(&self, host: &str) -> Result<Page, RenderError> {
        let mut running = self.running.lock().await;
        if running.as_ref().is_none_or(|running| running.pump.is_finished()) {
            *running = Some(self.start().await?);
        }
        let running = running.as_mut().expect("just started");
        let known = running.contexts.iter().find(|(name, _)| name == host);
        let context = match known {
            Some((_, id)) => id.clone(),
            None => {
                if running.contexts.len() >= MAX_CONTEXTS
                    && let Some((_, oldest)) = running.contexts.pop_front()
                {
                    // Its pages, if any are still open, fail as their own renders.
                    running.browser.dispose_browser_context(oldest).await.map_err(failed)?;
                }
                let id = running
                    .browser
                    .create_browser_context(Default::default())
                    .await
                    .map_err(failed)?;
                running.contexts.push_back((host.to_owned(), id.clone()));
                id
            }
        };
        let params = CreateTargetParams::builder()
            .url("about:blank")
            .browser_context_id(context)
            .build()
            .map_err(RenderError::Failed)?;
        running.browser.new_page(params).await.map_err(failed)
    }

    async fn read(&self, page: &Page, url: &str) -> Result<Rendered, RenderError> {
        page.execute(DisableParams::default()).await.map_err(failed)?;
        page.execute(SetBypassServiceWorkerParams::new(true)).await.map_err(failed)?;
        let mut paused = page.event_listener::<EventRequestPaused>().await.map_err(failed)?;
        page.execute(FetchEnable::default()).await.map_err(failed)?;
        let (filter_page, allow) = (page.clone(), Arc::clone(&self.allow));
        let filter = Abort(tokio::spawn(async move {
            while let Some(event) = paused.next().await {
                let (page, allow) = (filter_page.clone(), Arc::clone(&allow));
                // One at a time would hold the page's requests behind each other's DNS.
                tokio::spawn(async move {
                    let id = event.request_id.clone();
                    if wanted(&event) && allow(event.request.url.clone()).await {
                        page.execute(ContinueRequestParams::new(id)).await.ok();
                    } else {
                        let reason = ErrorReason::BlockedByClient;
                        page.execute(FailRequestParams::new(id, reason)).await.ok();
                    }
                });
            }
        }));

        let mut responses = page.event_listener::<EventResponseReceived>().await.map_err(failed)?;
        let mut lifecycle = page.event_listener::<EventLifecycleEvent>().await.map_err(failed)?;
        timeout(LOAD_TIMEOUT, page.goto(url))
            .await
            .map_err(|_| RenderError::Failed(format!("{url}: load timed out")))?
            .map_err(failed)?;
        let idle = async {
            while let Some(event) = lifecycle.next().await {
                if event.name == "networkIdle" {
                    break;
                }
            }
        };
        let _ = timeout(IDLE_TIMEOUT, idle).await;
        drop(filter);

        let world = CreateIsolatedWorldParams::builder()
            .frame_id(page.mainframe().await.map_err(failed)?.ok_or_else(no_frame)?)
            .world_name("kvasir")
            .build()
            .map_err(RenderError::Failed)?;
        let context = page.execute(world).await.map_err(failed)?.result.execution_context_id;
        let args = serde_json::json!([kvasir_extract::INTERACTIVE, MAX_COVER_RATIO]);
        let eval = EvaluateParams::builder()
            .expression(format!("({CLEAN})({args})"))
            .context_id(context)
            .return_by_value(true)
            .build()
            .map_err(RenderError::Failed)?;
        let found = page.execute(eval).await.map_err(failed)?.result.result.value;
        let html = found
            .and_then(|value| value.as_str().map(str::to_owned))
            .ok_or_else(|| RenderError::Failed(format!("{url}: no document")))?;

        // The last document answered is the page: the ones before it were redirects.
        let mut status = 0;
        while let Ok(Some(event)) = timeout(Duration::ZERO, responses.next()).await {
            if event.r#type == ResourceType::Document {
                status = event.response.status as u16;
            }
        }
        let final_url = page.url().await.map_err(failed)?.unwrap_or_else(|| url.to_owned());
        Ok(Rendered { html, final_url, status })
    }
}

/// Stops a task when dropped.
struct Abort(JoinHandle<()>);

impl Drop for Abort {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn wanted(event: &EventRequestPaused) -> bool {
    if matches!(event.resource_type, ResourceType::Image | ResourceType::Media | ResourceType::Font)
    {
        return false;
    }
    let url = Url::parse(&event.request.url).ok();
    let host = url.as_ref().and_then(Url::host_str).unwrap_or("");
    !AD_HOSTS.iter().any(|ad| host.contains(ad))
}

fn failed(error: impl std::fmt::Display) -> RenderError {
    RenderError::Failed(error.to_string())
}

fn no_frame() -> RenderError {
    RenderError::Failed("page has no frame".to_owned())
}

impl Render for Chromium {
    fn render<'a>(&'a self, url: &'a str) -> BoxFuture<'a, Result<Rendered, RenderError>> {
        Box::pin(async move {
            if let Some(reason) = self.unavailable.lock().unwrap_or_else(|e| e.into_inner()).clone()
            {
                return Err(RenderError::Unavailable(reason));
            }
            let parsed = Url::parse(url).map_err(failed)?;
            let host = parsed.host_str().unwrap_or("").to_owned();
            let _slot = self.pages.acquire().await.map_err(failed)?;
            let page = timeout(RENDER_TIMEOUT, self.new_page(&host))
                .await
                .map_err(|_| RenderError::Failed(format!("{url}: render timed out")))??;
            let read = timeout(RENDER_TIMEOUT, self.read(&page, url)).await;
            page.close().await.ok();
            read.map_err(|_| RenderError::Failed(format!("{url}: render timed out")))?.map_err(
                |e| match e {
                    RenderError::Failed(reason) if !reason.starts_with(url) => {
                        RenderError::Failed(format!("{url}: {reason}"))
                    }
                    other => other,
                },
            )
        })
    }
}
