//! The lists an operator chose: the catalog's, with what was changed on each, and their own.

use std::sync::Arc;

use kvasir_db::DbError;
use kvasir_types::sites::{ListChange, SourceList, is_list_effect, is_list_tag};
use serde::Deserialize;
use sha1::{Digest, Sha1};
use url::Url;

use super::{Sources, normalize};
use crate::now;

const CATALOG: &str = include_str!("catalog.toml");

/// A change to the lists that could not be made.
#[derive(Debug, thiserror::Error)]
pub enum ListError {
    #[error("{0}")]
    Invalid(String),
    #[error("no list {0}")]
    Missing(String),
    #[error(transparent)]
    Store(#[from] DbError),
}

#[derive(Deserialize)]
struct Catalog {
    list: Vec<Entry>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Entry {
    id: String,
    name: String,
    url: String,
    group: String,
    licence: String,
    effect: String,
    tag: String,
}

/// Every list the catalog offers, off, as it ships.
pub fn catalog() -> Vec<SourceList> {
    let catalog: Catalog = toml::from_str(CATALOG).expect("the catalog is valid");
    catalog
        .list
        .into_iter()
        .map(|entry| SourceList {
            id: entry.id,
            name: entry.name,
            url: Some(entry.url),
            entries: Vec::new(),
            enabled: false,
            effect: entry.effect,
            tag: entry.tag,
            ttl_days: None,
            guard: true,
            except: Vec::new(),
            group: entry.group,
            licence: entry.licence,
            custom: false,
        })
        .collect()
}

fn custom(id: &str) -> SourceList {
    SourceList {
        id: id.to_owned(),
        name: String::new(),
        url: None,
        entries: Vec::new(),
        enabled: true,
        effect: "block".to_owned(),
        tag: String::new(),
        ttl_days: None,
        guard: true,
        except: Vec::new(),
        group: "Custom".to_owned(),
        licence: String::new(),
        custom: true,
    }
}

fn apply(list: &mut SourceList, change: ListChange) {
    let ListChange { name, url, entries, enabled, effect, tag, ttl_days, guard, except } = change;
    list.name = name.unwrap_or(std::mem::take(&mut list.name));
    list.url = url.or(list.url.take());
    list.entries = entries.unwrap_or(std::mem::take(&mut list.entries));
    list.enabled = enabled.unwrap_or(list.enabled);
    list.effect = effect.unwrap_or(std::mem::take(&mut list.effect));
    list.tag = tag.unwrap_or(std::mem::take(&mut list.tag));
    // 0 goes back to `sources.ttl_days`.
    list.ttl_days = ttl_days.map_or(list.ttl_days, |days| (days > 0).then_some(days));
    list.guard = guard.unwrap_or(list.guard);
    list.except = except.unwrap_or(std::mem::take(&mut list.except));
}

fn hosts(names: &[String], what: &str) -> Result<Vec<String>, ListError> {
    names
        .iter()
        .map(|name| {
            normalize(name)
                .ok_or_else(|| ListError::Invalid(format!("{what} holds {name:?}, not a DNS name")))
        })
        .collect()
}

/// `list` with its hosts normalized, or why it cannot be kept.
fn checked(mut list: SourceList) -> Result<SourceList, ListError> {
    let invalid = |message: String| Err(ListError::Invalid(message));
    if list.name.trim().is_empty() {
        return invalid("a list needs a name".to_owned());
    }
    if list.url.is_some() == !list.entries.is_empty() {
        return invalid("a list needs a url or entries, and not both".to_owned());
    }
    if let Some(url) = &list.url {
        let web = Url::parse(url).is_ok_and(|url| matches!(url.scheme(), "http" | "https"));
        if !web {
            return invalid(format!("{url:?} is not an http(s) URL"));
        }
    }
    if !is_list_effect(&list.effect) {
        return invalid(format!(
            "{:?} is not block, demote, exempt, trust or kind:<kind>",
            list.effect
        ));
    }
    if !is_list_tag(&list.tag) {
        return invalid(format!(
            "{:?} cannot be a list's tag: use lowercase words, and not a tag that sets a tier",
            list.tag
        ));
    }
    list.entries = hosts(&list.entries, "entries")?;
    list.except = hosts(&list.except, "except")?;
    Ok(list)
}

/// `change` with the hosts `list` holds after `checked`, so that what is kept is normalized.
fn as_checked(mut change: ListChange, list: &SourceList) -> ListChange {
    change.entries = change.entries.map(|_| list.entries.clone());
    change.except = change.except.map(|_| list.except.clone());
    change
}

/// Every field of `new` over those of `old`.
fn merged(old: ListChange, new: ListChange) -> ListChange {
    let as_map = |change: &ListChange| match serde_json::to_value(change) {
        Ok(serde_json::Value::Object(map)) => map,
        _ => unreachable!("a change is an object"),
    };
    let mut fields = as_map(&old);
    fields.extend(as_map(&new));
    serde_json::from_value(serde_json::Value::Object(fields)).expect("the fields of a change")
}

impl Sources {
    /// The catalog's lists, with what was changed on them, then the operator's own.
    pub async fn lists(&self) -> Result<Vec<SourceList>, DbError> {
        let mut lists = catalog();
        let mut own = Vec::new();
        for (id, change) in self.store.list_changes().await? {
            let Ok(change) = serde_json::from_str::<ListChange>(&change) else {
                tracing::warn!("source list {id} unreadable, ignored");
                continue;
            };
            match lists.iter_mut().find(|list| list.id == id) {
                Some(list) => apply(list, change),
                None => {
                    let mut list = custom(&id);
                    apply(&mut list, change);
                    own.push(list);
                }
            }
        }
        lists.append(&mut own);
        Ok(lists)
    }

    /// Adds a list of the operator's own, on and blocking unless `change` says otherwise.
    pub async fn add_list(self: &Arc<Self>, change: ListChange) -> Result<SourceList, ListError> {
        let seed = format!("{:?} {:?} {}", change.name, change.url, now());
        let digest: String =
            Sha1::digest(seed.as_bytes()).iter().map(|byte| format!("{byte:02x}")).collect();
        let id = format!("custom-{}", &digest[..12]);
        let mut list = custom(&id);
        apply(&mut list, change.clone());
        let list = checked(list)?;
        let kept = serde_json::to_string(&as_checked(change, &list)).expect("a change");
        self.store.put_list(&id, kept).await?;
        Ok(list)
    }

    /// Changes one field or more of a list. A catalog list keeps its source.
    pub async fn change_list(
        self: &Arc<Self>,
        id: &str,
        change: ListChange,
    ) -> Result<SourceList, ListError> {
        let lists = self.lists().await?;
        let Some(mut list) = lists.into_iter().find(|list| list.id == id) else {
            return Err(ListError::Missing(id.to_owned()));
        };
        if !list.custom && (change.url.is_some() || change.entries.is_some()) {
            return Err(ListError::Invalid(format!(
                "{id} is a catalog list, so its url and entries are fixed: add a list of your own"
            )));
        }
        apply(&mut list, change.clone());
        let list = checked(list)?;
        let old = self.store.list_change(id).await?;
        let old = old.and_then(|old| serde_json::from_str(&old).ok()).unwrap_or_default();
        let kept = merged(old, as_checked(change, &list));
        let kept = serde_json::to_string(&kept).expect("a change");
        self.store.put_list(id, kept).await?;
        Ok(list)
    }

    /// Removes a list of the operator's own, or puts a catalog list back as it ships.
    pub async fn remove_list(self: &Arc<Self>, id: &str) -> Result<(), ListError> {
        if !self.lists().await?.iter().any(|list| list.id == id) {
            return Err(ListError::Missing(id.to_owned()));
        }
        Ok(self.store.drop_list(id).await?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_catalog_is_valid_with_unique_ids_and_starts_off() {
        let lists = catalog();
        assert!(lists.len() >= 23);
        let mut ids: Vec<&str> = lists.iter().map(|list| list.id.as_str()).collect();
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), lists.len());
        for list in lists {
            assert!(!list.enabled, "{}", list.id);
            // No remote list can raise or clear a site by default; see docs/lists-plan.md.
            assert!(matches!(list.effect.as_str(), "block" | "demote"), "{}", list.id);
            checked(list).unwrap();
        }
    }

    #[test]
    fn a_list_needs_exactly_one_source_a_name_and_valid_values() {
        let good = || SourceList {
            name: "Mine".to_owned(),
            entries: vec!["WWW.Farm.org".to_owned()],
            ..custom("custom-1")
        };
        assert_eq!(checked(good()).unwrap().entries, ["farm.org"]);
        for bad in [
            SourceList { name: " ".to_owned(), ..good() },
            SourceList { entries: Vec::new(), ..good() },
            SourceList { url: Some("https://l.example/a".to_owned()), ..good() },
            SourceList { entries: Vec::new(), url: Some("ftp://l.example/a".to_owned()), ..good() },
            SourceList { effect: "explode".to_owned(), ..good() },
            SourceList { effect: "kind:unknown".to_owned(), ..good() },
            SourceList { tag: "deny".to_owned(), ..good() },
            SourceList { tag: "Has Spaces".to_owned(), ..good() },
            SourceList { except: vec!["not a host".to_owned()], ..good() },
        ] {
            assert!(matches!(checked(bad.clone()), Err(ListError::Invalid(_))), "{bad:?}");
        }
    }
}
