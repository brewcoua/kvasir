//! How often a host is asked, and how many requests the process has open.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{Instant, sleep};

/// What a polite crawler opens to one host at once. Not a setting: a host that wants fewer says so
/// with a 429.
pub const HOST_CONCURRENCY: usize = 2;
// Hosts remembered past this are dropped once nothing is waiting on them.
const PRUNE_AT: usize = 1024;

/// What one host has been asked: how many requests are open and when the next may start.
struct Host {
    open: Arc<Semaphore>,
    next_start: Mutex<Instant>,
}

/// A request's place with its host and one of the process's permits.
#[must_use]
pub struct Slot {
    _host: OwnedSemaphorePermit,
    _process: OwnedSemaphorePermit,
}

pub struct Politeness {
    hosts: Mutex<HashMap<String, Arc<Host>>>,
    // Sized once at startup: replacing a semaphore while permits are held loses them.
    permits: Arc<Semaphore>,
}

impl Politeness {
    pub fn new(permits: usize) -> Self {
        Self { hosts: Mutex::new(HashMap::new()), permits: Arc::new(Semaphore::new(permits)) }
    }

    fn host(&self, name: &str) -> Arc<Host> {
        let mut hosts = self.hosts.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(host) = hosts.get(name) {
            return Arc::clone(host);
        }
        if hosts.len() >= PRUNE_AT {
            let now = Instant::now();
            hosts.retain(|_, host| {
                let next = *host.next_start.lock().unwrap_or_else(|e| e.into_inner());
                Arc::strong_count(host) > 1 || next > now
            });
        }
        let host = Arc::new(Host {
            open: Arc::new(Semaphore::new(HOST_CONCURRENCY)),
            next_start: Mutex::new(Instant::now()),
        });
        hosts.insert(name.to_owned(), Arc::clone(&host));
        host
    }

    /// Holds a request's place with its host, then one of the process's permits.
    ///
    /// The wait for the host comes first and holds no process-wide permit, so a host that is being
    /// slow cannot starve fetches to every other host. The start time is claimed in a loop rather
    /// than reserved up front, so a `delay` that lands while others wait moves them too.
    pub async fn slot(&self, name: &str, interval: Duration) -> Slot {
        let host = self.host(name);
        let open = Arc::clone(&host.open);
        let host_permit = open.acquire_owned().await.expect("never closed");
        loop {
            let wait = {
                let mut next = host.next_start.lock().unwrap_or_else(|e| e.into_inner());
                let now = Instant::now();
                if *next <= now {
                    *next = now + interval;
                    break;
                }
                *next - now
            };
            sleep(wait).await;
        }
        let process = Arc::clone(&self.permits).acquire_owned().await.expect("never closed");
        Slot { _host: host_permit, _process: process }
    }

    /// One of the process's permits alone, for a service of the operator's own that no host limit
    /// applies to.
    pub async fn permit(&self) -> OwnedSemaphorePermit {
        Arc::clone(&self.permits).acquire_owned().await.expect("never closed")
    }

    /// Holds back the next request to `name` by at least `by`, as a Retry-After asks.
    pub fn delay(&self, name: &str, by: Duration) {
        let host = self.host(name);
        let mut next = host.next_start.lock().unwrap_or_else(|e| e.into_inner());
        *next = (*next).max(Instant::now() + by);
    }

    #[cfg(test)]
    fn free_permits(&self) -> usize {
        self.permits.available_permits()
    }

    #[cfg(test)]
    fn remembered(&self) -> usize {
        self.hosts.lock().unwrap().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECOND: Duration = Duration::from_secs(1);

    #[tokio::test(start_paused = true)]
    async fn requests_to_one_host_start_an_interval_apart() {
        let polite = Politeness::new(10);
        let start = Instant::now();
        let mut starts = Vec::new();
        for _ in 0..3 {
            let _slot = polite.slot("a.example", SECOND).await;
            starts.push((Instant::now() - start).as_secs());
        }
        assert_eq!(starts, [0, 1, 2]);
    }

    #[tokio::test(start_paused = true)]
    async fn another_host_does_not_wait_for_the_first() {
        let polite = Politeness::new(10);
        let start = Instant::now();
        drop(polite.slot("a.example", SECOND).await);
        drop(polite.slot("b.example", SECOND).await);
        assert_eq!(Instant::now(), start);
    }

    #[tokio::test(start_paused = true)]
    async fn a_host_never_has_more_than_two_requests_open() {
        let polite = Arc::new(Politeness::new(100));
        let open = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let peak = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut tasks = Vec::new();
        for _ in 0..8 {
            let (polite, open, peak) = (polite.clone(), open.clone(), peak.clone());
            tasks.push(tokio::spawn(async move {
                use std::sync::atomic::Ordering::SeqCst;
                let _slot = polite.slot("a.example", Duration::ZERO).await;
                peak.fetch_max(open.fetch_add(1, SeqCst) + 1, SeqCst);
                sleep(Duration::from_millis(20)).await;
                open.fetch_sub(1, SeqCst);
            }));
        }
        for task in tasks {
            task.await.unwrap();
        }
        assert_eq!(peak.load(std::sync::atomic::Ordering::SeqCst), HOST_CONCURRENCY);
    }

    #[tokio::test(start_paused = true)]
    async fn the_host_wait_holds_no_process_permit() {
        let polite = Arc::new(Politeness::new(1));
        drop(polite.slot("a.example", SECOND).await);
        let waiting = {
            let polite = polite.clone();
            tokio::spawn(async move { drop(polite.slot("a.example", SECOND).await) })
        };
        tokio::task::yield_now().await;
        // The only permit there is stays free while this host is being waited for.
        assert_eq!(polite.free_permits(), 1);
        drop(polite.slot("b.example", SECOND).await);
        waiting.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn delay_pushes_the_next_start_later() {
        let polite = Politeness::new(10);
        let start = Instant::now();
        drop(polite.slot("a.example", SECOND).await);
        polite.delay("a.example", 7 * SECOND);
        drop(polite.slot("a.example", SECOND).await);
        assert_eq!((Instant::now() - start).as_secs(), 7);
    }

    #[tokio::test(start_paused = true)]
    async fn delay_never_pulls_the_next_start_earlier() {
        let polite = Politeness::new(10);
        let start = Instant::now();
        drop(polite.slot("a.example", SECOND).await);
        polite.delay("a.example", Duration::ZERO);
        drop(polite.slot("a.example", SECOND).await);
        assert_eq!((Instant::now() - start).as_secs(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn hosts_with_nothing_waiting_are_forgotten() {
        let polite = Politeness::new(10);
        for i in 0..PRUNE_AT + 1 {
            drop(polite.slot(&format!("{i}.example"), Duration::ZERO).await);
        }
        assert!(polite.remembered() < PRUNE_AT, "{}", polite.remembered());
    }
}
