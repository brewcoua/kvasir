#![doc = include_str!("../README.md")]

pub mod favicons;
pub mod sources;

const SECONDS_PER_DAY: f64 = 86400.0;

fn now() -> f64 {
    kvasir_fetch::now()
}
