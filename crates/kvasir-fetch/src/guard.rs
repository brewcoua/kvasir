//! This service's own network stays out of reach of any URL a page or a model wrote.
//!
//! The check runs where the connection is made: a resolver that hands the client only public
//! addresses, so a name cannot resolve publicly when checked and privately when connected to. A
//! literal address skips any resolver, so URLs are checked for those, on every redirect too.

use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::Arc;

use kvasir_browser::BoxFuture;
use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use url::{Host, Url};

/// Resolves a name to its addresses. Tests stand in for DNS here.
pub type Lookup = Arc<dyn Fn(String) -> BoxFuture<'static, io::Result<Vec<IpAddr>>> + Send + Sync>;

/// What the system resolver answers.
pub fn system_lookup() -> Lookup {
    Arc::new(|host| {
        Box::pin(async move {
            let found = tokio::net::lookup_host((host.as_str(), 0)).await?;
            Ok(found.map(|address| address.ip()).collect())
        })
    })
}

/// Whether `address` is on the public internet.
pub fn is_public(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => is_public_v4(v4),
        IpAddr::V6(v6) => is_public_v6(v6),
    }
}

fn in_v4(address: u32, network: [u8; 4], bits: u32) -> bool {
    let mask = u32::MAX.checked_shl(32 - bits).unwrap_or(0);
    address & mask == u32::from_be_bytes(network) & mask
}

fn is_public_v4(address: Ipv4Addr) -> bool {
    const NOT_PUBLIC: [([u8; 4], u32); 15] = [
        ([0, 0, 0, 0], 8),
        ([10, 0, 0, 0], 8),
        ([100, 64, 0, 0], 10),
        ([127, 0, 0, 0], 8),
        ([169, 254, 0, 0], 16),
        ([172, 16, 0, 0], 12),
        ([192, 0, 0, 0], 24),
        ([192, 0, 2, 0], 24),
        ([192, 88, 99, 0], 24),
        ([192, 168, 0, 0], 16),
        ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24),
        ([203, 0, 113, 0], 24),
        ([224, 0, 0, 0], 4),
        ([240, 0, 0, 0], 4),
    ];
    let bits = u32::from(address);
    !NOT_PUBLIC.iter().any(|(network, size)| in_v4(bits, *network, *size))
}

fn is_public_v6(address: Ipv6Addr) -> bool {
    let bits = u128::from(address);
    let inside = |network: u128, size: u32| {
        let mask = u128::MAX.checked_shl(128 - size).unwrap_or(0);
        bits & mask == network & mask
    };
    let embedded = |shift: u32| Ipv4Addr::from(((bits >> shift) & 0xffff_ffff) as u32);
    // An IPv4 address in an IPv6 one reaches what the IPv4 address reaches.
    if inside(0, 96) {
        // :: and ::1, and the old IPv4-compatible form.
        return bits > 1 && is_public_v4(embedded(0));
    }
    if inside(0xffff << 32, 96) || inside(0x64_ff9b << 96, 96) {
        return is_public_v4(embedded(0));
    }
    if inside(0x2002 << 112, 16) {
        return is_public_v4(embedded(80));
    }
    const NOT_PUBLIC: [(u128, u32); 8] = [
        (0x64_ff9b_0001 << 80, 48),
        (0x100 << 112, 64),
        (0x2001 << 112, 23),
        (0x2001_0db8 << 96, 32),
        (0x3fff << 112, 20),
        (0xfc00 << 112, 7),
        (0xfe80 << 112, 10),
        (0xff00 << 112, 8),
    ];
    !NOT_PUBLIC.iter().any(|(network, size)| inside(*network, *size))
}

/// Whether a URL may be asked at all: http or https, and no literal private address.
pub fn url_allowed(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && match url.host() {
            Some(Host::Ipv4(v4)) => is_public_v4(v4),
            Some(Host::Ipv6(v6)) => is_public_v6(v6),
            Some(Host::Domain(_)) => true,
            None => false,
        }
}

/// A resolver that answers only when every address of the name is public, as one private address
/// among public ones is how a name is pointed at a private network.
pub struct PublicResolver(pub Lookup);

impl Resolve for PublicResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let lookup = Arc::clone(&self.0);
        Box::pin(async move {
            let host = name.as_str().to_owned();
            let found = lookup(host.clone()).await?;
            if found.is_empty() || !found.iter().copied().all(is_public) {
                let refused = format!("{host} is not on the public internet");
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, refused).into());
            }
            let addresses: Addrs =
                Box::new(found.into_iter().map(|address| SocketAddr::new(address, 0)));
            Ok(addresses)
        })
    }
}
