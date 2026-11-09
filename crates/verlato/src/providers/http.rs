//! Minimal blocking HTTP for model providers. Follows Python urllib's proxy behaviour:
//! `HTTPS_PROXY`/`HTTP_PROXY` (either case) unless the host matches `NO_PROXY`.

use std::time::Duration;

pub enum HttpError {
    /// The server answered with an error status (code, reason phrase).
    Status(u16, String),
    /// Connection, TLS, timeout or protocol failure.
    Transport(String),
}

fn env_any(names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| std::env::var(name).ok().filter(|v| !v.trim().is_empty()))
}

fn host_of(url: &str) -> String {
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let authority = authority
        .rsplit_once('@')
        .map(|(_, h)| h)
        .unwrap_or(authority);
    if let Some(stripped) = authority.strip_prefix('[') {
        return stripped.split(']').next().unwrap_or("").to_lowercase();
    }
    authority.split(':').next().unwrap_or("").to_lowercase()
}

fn ipv4(text: &str) -> Option<u32> {
    let octets: Vec<u32> = text
        .split('.')
        .map(|p| p.parse::<u8>().ok().map(u32::from))
        .collect::<Option<_>>()?;
    (octets.len() == 4).then(|| octets.iter().fold(0, |acc, o| (acc << 8) | o))
}

/// Whether `NO_PROXY` exempts `host` (exact names, domain suffixes, `*`, IPv4 CIDRs).
pub fn bypasses_proxy(host: &str, no_proxy: &str) -> bool {
    no_proxy
        .split(',')
        .map(str::trim)
        .filter(|e| !e.is_empty())
        .any(|entry| {
            if entry == "*" {
                return true;
            }
            let entry = entry.to_lowercase();
            if let Some((network, bits)) = entry.split_once('/') {
                return match (ipv4(network), bits.parse::<u32>(), ipv4(host)) {
                    (Some(network), Ok(bits), Some(address)) if bits <= 32 => {
                        let mask = if bits == 0 {
                            0
                        } else {
                            u32::MAX << (32 - bits)
                        };
                        network & mask == address & mask
                    }
                    _ => false,
                };
            }
            let suffix = entry.trim_start_matches('.');
            host == suffix || host.ends_with(&format!(".{suffix}"))
        })
}

fn proxy_for(url: &str) -> Option<String> {
    let host = host_of(url);
    if let Some(no_proxy) = env_any(&["NO_PROXY", "no_proxy"]) {
        if bypasses_proxy(&host, &no_proxy) {
            return None;
        }
    }
    if url.to_lowercase().starts_with("https://") {
        env_any(&["HTTPS_PROXY", "https_proxy"])
    } else {
        env_any(&["HTTP_PROXY", "http_proxy"])
    }
}

pub fn post_json(
    url: &str,
    headers: &[(&str, String)],
    body: &str,
    timeout: Duration,
) -> Result<String, HttpError> {
    let mut builder = ureq::AgentBuilder::new().timeout(timeout);
    if let Some(proxy) = proxy_for(url) {
        match ureq::Proxy::new(&proxy) {
            Ok(proxy) => builder = builder.proxy(proxy),
            Err(error) => {
                return Err(HttpError::Transport(format!(
                    "invalid proxy {proxy}: {error}"
                )))
            }
        }
    }
    let mut request = builder.build().post(url);
    for (name, value) in headers {
        request = request.set(name, value);
    }
    match request.send_string(body) {
        Ok(response) => response
            .into_string()
            .map_err(|error| HttpError::Transport(error.to_string())),
        Err(ureq::Error::Status(code, response)) => {
            Err(HttpError::Status(code, response.status_text().to_owned()))
        }
        Err(ureq::Error::Transport(error)) => Err(HttpError::Transport(error.to_string())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_proxy_matching() {
        let no_proxy = "localhost,127.0.0.0/8,.internal.example,api.example.com";
        assert!(bypasses_proxy("localhost", no_proxy));
        assert!(bypasses_proxy("127.0.0.1", no_proxy));
        assert!(bypasses_proxy("llm.internal.example", no_proxy));
        assert!(bypasses_proxy("api.example.com", no_proxy));
        assert!(!bypasses_proxy("example.com", no_proxy));
        assert!(!bypasses_proxy("10.0.0.1", no_proxy));
        assert_eq!(host_of("http://user@127.0.0.1:8088/v1/chat"), "127.0.0.1");
        assert_eq!(host_of("https://[::1]:443/x"), "::1");
    }
}
