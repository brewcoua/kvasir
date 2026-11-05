//! What a robots.txt allows this crawler, by RFC 9309: the group naming it, or `*`; the longest
//! matching rule; and `allow` when two match at one length.

const AGENT: &str = "kvasir";

#[derive(Clone, Debug, PartialEq, Eq)]
struct Rule {
    allow: bool,
    pattern: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Robots {
    rules: Vec<Rule>,
}

impl Robots {
    pub fn parse(text: &str) -> Self {
        // Groups are merged when one agent is named twice, as the RFC asks.
        let (mut named, mut star) = (Vec::new(), Vec::new());
        let (mut is_named, mut is_star) = (false, false);
        // A group naming this crawler governs it even with no rules, which is how a site says that
        // this crawler may read what others may not.
        let mut named_group = false;
        let mut in_rules = true;
        for line in text.trim_start_matches('\u{feff}').lines() {
            let line = line.split('#').next().unwrap_or("").trim();
            let Some((key, value)) = line.split_once(':') else {
                continue;
            };
            let (key, value) = (key.trim().to_ascii_lowercase(), value.trim());
            match key.as_str() {
                "user-agent" => {
                    if in_rules {
                        (is_named, is_star) = (false, false);
                        in_rules = false;
                    }
                    let agent = value.split('/').next().unwrap_or("").to_ascii_lowercase();
                    is_named |= agent == AGENT;
                    named_group |= agent == AGENT;
                    is_star |= agent == "*";
                }
                "allow" | "disallow" => {
                    in_rules = true;
                    // An empty rule says nothing: "Disallow:" allows all.
                    if value.is_empty() {
                        continue;
                    }
                    let rule = Rule { allow: key == "allow", pattern: value.to_owned() };
                    if is_named {
                        named.push(rule.clone());
                    }
                    if is_star {
                        star.push(rule);
                    }
                }
                _ => {}
            }
        }
        Self { rules: if named_group { named } else { star } }
    }

    /// Whether this crawler may read `path`, its path and query as the URL has them.
    pub fn allows(&self, path: &str) -> bool {
        let mut best: Option<(usize, bool)> = None;
        for rule in &self.rules {
            if matches(&rule.pattern, path) {
                let length = rule.pattern.len();
                if best.is_none_or(|(l, allow)| length > l || (length == l && rule.allow && !allow))
                {
                    best = Some((length, rule.allow));
                }
            }
        }
        best.is_none_or(|(_, allow)| allow)
    }
}

/// Whether `pattern` matches from the start of `path`: `*` stands for anything, and a final `$`
/// for the end of the path.
fn matches(pattern: &str, path: &str) -> bool {
    let (pattern, anchored) = match pattern.strip_suffix('$') {
        Some(pattern) => (pattern, true),
        None => (pattern, false),
    };
    let mut parts = pattern.split('*');
    let first = parts.next().unwrap_or("");
    let Some(mut rest) = path.strip_prefix(first) else {
        return false;
    };
    let parts: Vec<&str> = parts.collect();
    for (i, part) in parts.iter().enumerate() {
        if i + 1 == parts.len() && anchored {
            return rest.ends_with(part);
        }
        match rest.find(part) {
            Some(at) => rest = &rest[at + part.len()..],
            None => return false,
        }
    }
    !anchored || rest.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_group_naming_this_crawler_is_preferred_over_the_star() {
        let robots =
            Robots::parse("User-agent: *\nDisallow: /\n\nUser-agent: Kvasir\nDisallow: /private");
        assert!(robots.allows("/a"));
        assert!(!robots.allows("/private/x"));
    }

    #[test]
    fn a_group_naming_this_crawler_with_no_rules_allows_all() {
        let robots = Robots::parse("User-agent: kvasir\nDisallow:\n\nUser-agent: *\nDisallow: /");
        assert!(robots.allows("/a"));
    }

    #[test]
    fn the_star_group_applies_to_others() {
        let robots =
            Robots::parse("User-agent: googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /x");
        assert!(!robots.allows("/x"));
        assert!(robots.allows("/y"));
    }

    #[test]
    fn agents_listed_together_share_the_rules_below() {
        let robots = Robots::parse("User-agent: other\nUser-agent: kvasir\nDisallow: /x\n");
        assert!(!robots.allows("/x"));
    }

    #[test]
    fn the_longest_rule_wins_and_allow_wins_a_tie() {
        let robots =
            Robots::parse("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /c\nAllow: /c");
        assert!(!robots.allows("/a/c"));
        assert!(robots.allows("/a/b/c"));
        assert!(robots.allows("/c"));
    }

    #[test]
    fn wildcards_and_the_end_mark_match() {
        let robots = Robots::parse("User-agent: *\nDisallow: /*.pdf$\nDisallow: /x*y");
        assert!(!robots.allows("/a/b.pdf"));
        assert!(robots.allows("/a/b.pdf?page=2"));
        assert!(!robots.allows("/x1y2"));
        assert!(robots.allows("/x1"));
    }

    #[test]
    fn an_empty_disallow_allows_all_and_comments_are_ignored() {
        assert!(Robots::parse("User-agent: *\nDisallow:\n").allows("/x"));
        let robots = Robots::parse("# nothing\nUser-agent: * # all\nDisallow: /x # no\n");
        assert!(!robots.allows("/x"));
        assert!(Robots::parse("").allows("/x"));
    }
}
