//! A tab a person drives from elsewhere, to clear a search engine's block in this machine's
//! browser, which is what the engine's cookie will be tied to. It has its own browser, so nothing
//! it does touches the renders, and no address bar: it stays on the site it was opened on.

use std::sync::Arc;

use chromiumoxide::browser::{Browser, BrowserConfig};
use chromiumoxide::cdp::browser_protocol::fetch::{
    ContinueRequestParams, EnableParams as FetchEnable, EventRequestPaused, FailRequestParams,
};
use chromiumoxide::cdp::browser_protocol::input::{
    DispatchKeyEventParams, DispatchKeyEventType, DispatchMouseEventParams, DispatchMouseEventType,
    InsertTextParams, MouseButton,
};
use chromiumoxide::cdp::browser_protocol::network::{ErrorReason, ResourceType};
use chromiumoxide::cdp::browser_protocol::page::{
    EventScreencastFrame, ScreencastFrameAckParams, StartScreencastFormat, StartScreencastParams,
};
use chromiumoxide::handler::viewport::Viewport;
use chromiumoxide::page::Page;
use futures::{Stream, StreamExt};
use serde::Deserialize;
use tempfile::TempDir;
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tokio::time::timeout;

use crate::RenderError;
use crate::chromium::{Abort, Allow, LOAD_TIMEOUT, STEALTH_ARGS};

const WIDTH: u32 = 1280;
const HEIGHT: u32 = 720;
const FRAME_QUALITY: i64 = 60;

/// What a person does, in the page's own pixels.
#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Input {
    Mouse {
        action: MouseAction,
        x: f64,
        y: f64,
        #[serde(default)]
        pressed: bool,
        #[serde(default)]
        delta_y: f64,
    },
    Key {
        down: bool,
        key: String,
        code: String,
        key_code: i64,
        text: Option<String>,
    },
    Text {
        text: String,
    },
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MouseAction {
    Move,
    Press,
    Release,
    Wheel,
}

pub struct Tab {
    page: Page,
    browser: Browser,
    left: Arc<Notify>,
    _pump: Abort,
    _filter: Abort,
    _profile: TempDir,
}

fn failed(error: impl std::fmt::Display) -> RenderError {
    RenderError::Failed(error.to_string())
}

impl Tab {
    /// `url` in a new browser. A request `allow` refuses fails, and so does the tab going to an
    /// address `stays` refuses, which [`Tab::left`] then tells.
    pub async fn open(
        url: &str,
        allow: Allow,
        stays: Arc<dyn Fn(&str) -> bool + Send + Sync>,
    ) -> Result<Self, RenderError> {
        let profile = tempfile::tempdir().map_err(failed)?;
        let viewport = Viewport {
            width: WIDTH,
            height: HEIGHT,
            device_scale_factor: Some(1.0),
            emulating_mobile: false,
            is_landscape: false,
            has_touch: false,
        };
        // Flags that name automation are what a block looks for, whatever `fetch.stealth` says.
        let config = BrowserConfig::builder()
            .user_data_dir(profile.path())
            .new_headless_mode()
            .no_sandbox()
            .viewport(viewport)
            .disable_default_args()
            .args(STEALTH_ARGS)
            .build()
            .map_err(RenderError::Unavailable)?;
        let (browser, mut handler) = Browser::launch(config)
            .await
            .map_err(|e| RenderError::Unavailable(format!("cannot open a browser: {e}")))?;
        let pump: JoinHandle<()> =
            tokio::spawn(async move { while handler.next().await.is_some() {} });
        let page = browser.new_page("about:blank").await.map_err(failed)?;
        let main = page.mainframe().await.map_err(failed)?;

        let left = Arc::new(Notify::new());
        let mut paused = page.event_listener::<EventRequestPaused>().await.map_err(failed)?;
        page.execute(FetchEnable::default()).await.map_err(failed)?;
        let (filter_page, filter_left) = (page.clone(), Arc::clone(&left));
        let filter = tokio::spawn(async move {
            while let Some(event) = paused.next().await {
                let id = event.request_id.clone();
                let target = event.request.url.clone();
                let leaving = event.resource_type == ResourceType::Document
                    && Some(&event.frame_id) == main.as_ref()
                    && !stays(&target);
                if leaving {
                    filter_left.notify_one();
                }
                let (page, allow) = (filter_page.clone(), Arc::clone(&allow));
                // One at a time would hold the page's requests behind each other's DNS.
                tokio::spawn(async move {
                    if !leaving && allow(target).await {
                        page.execute(ContinueRequestParams::new(id)).await.ok();
                    } else {
                        let reason = ErrorReason::BlockedByClient;
                        page.execute(FailRequestParams::new(id, reason)).await.ok();
                    }
                });
            }
        });

        timeout(LOAD_TIMEOUT, page.goto(url))
            .await
            .map_err(|_| RenderError::Failed(format!("{url}: load timed out")))?
            .map_err(failed)?;
        Ok(Self {
            page,
            browser,
            left,
            _pump: Abort(pump),
            _filter: Abort(filter),
            _profile: profile,
        })
    }

    /// The tab's picture as it changes, each a JPEG in base64.
    pub async fn frames(&self) -> Result<impl Stream<Item = String> + Send + use<>, RenderError> {
        let events = self.page.event_listener::<EventScreencastFrame>().await.map_err(failed)?;
        let start = StartScreencastParams::builder()
            .format(StartScreencastFormat::Jpeg)
            .quality(FRAME_QUALITY)
            .max_width(i64::from(WIDTH))
            .max_height(i64::from(HEIGHT))
            .build();
        self.page.execute(start).await.map_err(failed)?;
        let page = self.page.clone();
        Ok(events.then(move |frame| {
            let page = page.clone();
            async move {
                // The next frame comes only once this one is acknowledged.
                page.execute(ScreencastFrameAckParams::new(frame.session_id)).await.ok();
                AsRef::<str>::as_ref(&frame.data).to_owned()
            }
        }))
    }

    pub async fn input(&self, input: Input) -> Result<(), RenderError> {
        match input {
            Input::Mouse { action, x, y, pressed, delta_y } => {
                let kind = match action {
                    MouseAction::Move => DispatchMouseEventType::MouseMoved,
                    MouseAction::Press => DispatchMouseEventType::MousePressed,
                    MouseAction::Release => DispatchMouseEventType::MouseReleased,
                    MouseAction::Wheel => DispatchMouseEventType::MouseWheel,
                };
                let held = matches!(action, MouseAction::Press | MouseAction::Release)
                    || (pressed && matches!(action, MouseAction::Move));
                let mut event = DispatchMouseEventParams::builder().r#type(kind).x(x).y(y);
                if held {
                    event =
                        event.button(MouseButton::Left).click_count(1).buttons(i64::from(pressed));
                }
                if matches!(action, MouseAction::Wheel) {
                    event = event.delta_x(0.0).delta_y(delta_y);
                }
                self.page
                    .execute(event.build().map_err(RenderError::Failed)?)
                    .await
                    .map_err(failed)?;
            }
            Input::Key { down, key, code, key_code, text } => {
                let kind = match (down, &text) {
                    (true, Some(_)) => DispatchKeyEventType::KeyDown,
                    (true, None) => DispatchKeyEventType::RawKeyDown,
                    (false, _) => DispatchKeyEventType::KeyUp,
                };
                let mut event = DispatchKeyEventParams::builder()
                    .r#type(kind)
                    .key(key)
                    .code(code)
                    .windows_virtual_key_code(key_code);
                if let Some(text) = text {
                    event = event.text(text);
                }
                self.page
                    .execute(event.build().map_err(RenderError::Failed)?)
                    .await
                    .map_err(failed)?;
            }
            Input::Text { text } => {
                self.page.execute(InsertTextParams::new(text)).await.map_err(failed)?;
            }
        }
        Ok(())
    }

    /// The page's address and HTML, once it has loaded.
    pub async fn loaded(&self) -> Result<Option<(String, String)>, RenderError> {
        let state = self.page.evaluate("document.readyState").await.map_err(failed)?;
        if state.into_value::<String>().map_err(failed)? != "complete" {
            return Ok(None);
        }
        let url = self.page.url().await.map_err(failed)?.unwrap_or_default();
        Ok(Some((url, self.page.content().await.map_err(failed)?)))
    }

    /// The page's cookies, as a `Cookie` header, HttpOnly ones included.
    pub async fn cookies(&self) -> Result<String, RenderError> {
        let cookies = self.page.get_cookies().await.map_err(failed)?;
        let pairs: Vec<String> =
            cookies.into_iter().map(|cookie| format!("{}={}", cookie.name, cookie.value)).collect();
        Ok(pairs.join("; "))
    }

    /// Resolves once the tab tried to leave the site.
    pub async fn left(&self) {
        self.left.notified().await;
    }

    pub async fn close(mut self) {
        self.browser.close().await.ok();
        self.browser.wait().await.ok();
    }
}
