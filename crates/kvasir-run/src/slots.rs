//! How many runs may go at once.

use std::sync::{Arc, Mutex};

/// Unlike a semaphore, the limit can change while runs hold slots: a lower one refuses new runs
/// until enough have finished.
#[derive(Clone, Debug)]
pub struct Slots(Arc<Mutex<Taken>>);

#[derive(Debug)]
struct Taken {
    limit: usize,
    active: usize,
}

/// A claimed slot, given back when dropped.
#[derive(Debug)]
pub struct Slot(Arc<Mutex<Taken>>);

impl Slots {
    pub fn new(limit: usize) -> Self {
        Self(Arc::new(Mutex::new(Taken { limit, active: 0 })))
    }

    pub fn set_limit(&self, limit: usize) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).limit = limit;
    }

    /// A slot if one is free. Never waits.
    pub fn try_claim(&self) -> Option<Slot> {
        let mut taken = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if taken.active >= taken.limit {
            return None;
        }
        taken.active += 1;
        Some(Slot(Arc::clone(&self.0)))
    }

    pub fn active(&self) -> usize {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).active
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).active -= 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_slot_is_given_back_when_dropped_and_a_lower_limit_waits_for_runs_to_end() {
        let slots = Slots::new(2);
        let (a, b) = (slots.try_claim().unwrap(), slots.try_claim().unwrap());
        assert!(slots.try_claim().is_none());
        slots.set_limit(1);
        drop(a);
        assert!(slots.try_claim().is_none());
        drop(b);
        assert!(slots.try_claim().is_some());
        assert_eq!(slots.active(), 0);
    }
}
