//! Minimal blocking HTTP for model providers. Follows Python urllib's proxy behaviour:
//! `HTTPS_PROXY`/`HTTP_PROXY` (either case) unless the host matches `NO_PROXY`.

use std::io::BufRead;
use std::time::Duration;

pub enum HttpError {
    /// The server answered with an error status (code, reason phrase, response body).
    Status(u16, String, String),
    /// Connection, TLS, timeout or protocol failure.
    Transport(String),
    /// The server closed the connection without answering (Python's `RemoteDisconnected`).
    Disconnected(String),
}

impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HttpError::Status(code, reason, _) => write!(f, "HTTP Error {code}: {reason}"),
            HttpError::Transport(message) | HttpError::Disconnected(message) => {
                f.write_str(message)
            }
        }
    }
}

fn env_any(names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| std::env::var(name).ok().filter(|v| !v.trim().is_empty()))
}

/// The URL's `host[:port]` authority (without user info), lowercased.
fn authority_of(url: &str) -> String {
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    authority
        .rsplit_once('@')
        .map(|(_, h)| h)
        .unwrap_or(authority)
        .to_lowercase()
}

fn host_of(authority: &str) -> String {
    if let Some(stripped) = authority.strip_prefix('[') {
        return stripped.split(']').next().unwrap_or("").to_owned();
    }
    authority.split(':').next().unwrap_or("").to_owned()
}

fn ipv4(text: &str) -> Option<u32> {
    let octets: Vec<u32> = text
        .split('.')
        .map(|p| p.parse::<u8>().ok().map(u32::from))
        .collect::<Option<_>>()?;
    (octets.len() == 4).then(|| octets.iter().fold(0, |acc, o| (acc << 8) | o))
}

/// Whether `NO_PROXY` exempts the `host[:port]` authority. Like urllib, entries match the host
/// or the host with its port (`localhost:11434`), exactly or as a domain suffix; `*` and IPv4
/// CIDRs are supported too.
pub fn bypasses_proxy(authority: &str, no_proxy: &str) -> bool {
    let host = host_of(authority);
    let host = host.as_str();
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
            [host, authority]
                .iter()
                .any(|name| *name == suffix || name.ends_with(&format!(".{suffix}")))
        })
}

fn proxy_for(url: &str) -> Option<String> {
    let authority = authority_of(url);
    if let Some(no_proxy) = env_any(&["NO_PROXY", "no_proxy"]) {
        if bypasses_proxy(&authority, &no_proxy) {
            return None;
        }
    }
    if url.to_lowercase().starts_with("https://") {
        env_any(&["HTTPS_PROXY", "https_proxy"])
    } else {
        env_any(&["HTTP_PROXY", "http_proxy"])
    }
}

fn send(
    url: &str,
    headers: &[(&str, String)],
    body: &str,
    timeout: Duration,
) -> Result<ureq::Response, HttpError> {
    send_with(url, "POST", headers, Some(body), timeout)
}

/// GET `url` and return the body text.
pub fn get_text(
    url: &str,
    headers: &[(&str, String)],
    timeout: Duration,
) -> Result<String, HttpError> {
    send_with(url, "GET", headers, None, timeout)?
        .into_string()
        .map_err(|error| HttpError::Transport(error.to_string()))
}

fn send_with(
    url: &str,
    method: &str,
    headers: &[(&str, String)],
    body: Option<&str>,
    timeout: Duration,
) -> Result<ureq::Response, HttpError> {
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
    let mut request = builder.build().request(method, url);
    for (name, value) in headers {
        request = request.set(name, value);
    }
    let sent = match body {
        Some(body) => request.send_string(body),
        None => request.call(),
    };
    match sent {
        Ok(response) => Ok(response),
        Err(ureq::Error::Status(code, response)) => {
            let reason = response.status_text().to_owned();
            let body = response.into_string().unwrap_or_default();
            Err(HttpError::Status(code, reason, body))
        }
        Err(ureq::Error::Transport(error)) => {
            let message = error.to_string();
            let lowered = message.to_lowercase();
            let disconnected = error.kind() == ureq::ErrorKind::Io
                && ["closed", "end of file", "reset", "eof"]
                    .iter()
                    .any(|m| lowered.contains(m));
            Err(if disconnected {
                HttpError::Disconnected(message)
            } else {
                HttpError::Transport(message)
            })
        }
    }
}

pub fn post_json(
    url: &str,
    headers: &[(&str, String)],
    body: &str,
    timeout: Duration,
) -> Result<String, HttpError> {
    send(url, headers, body, timeout)?
        .into_string()
        .map_err(|error| HttpError::Transport(error.to_string()))
}

/// POST and hand each non-empty response line to `on_line` (newline-delimited JSON streams).
pub fn post_lines(
    url: &str,
    headers: &[(&str, String)],
    body: &str,
    timeout: Duration,
    mut on_line: impl FnMut(&str) -> Result<(), String>,
) -> Result<(), HttpError> {
    let response = send(url, headers, body, timeout)?;
    let reader = std::io::BufReader::new(response.into_reader());
    for line in reader.lines() {
        let line = line.map_err(|error| HttpError::Transport(error.to_string()))?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        on_line(line).map_err(HttpError::Transport)?;
    }
    Ok(())
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
        assert_eq!(
            authority_of("http://user@127.0.0.1:8088/v1/chat"),
            "127.0.0.1:8088"
        );
        assert_eq!(host_of(&authority_of("https://[::1]:443/x")), "::1");
        // Port-qualified entries match the host with that port only (as urllib does).
        assert!(bypasses_proxy("localhost:11434", "localhost:11434"));
        assert!(!bypasses_proxy("localhost:8080", "localhost:11434"));
        assert!(bypasses_proxy("api.example.com:443", "example.com:443"));
    }
}
