//! Read half of the Zotero Web API v3 client (port of client.py).
//!
//! Same musts as the Python client: Link-header pagination with a probe when
//! the metadata lies, Backoff/Retry-After honoured, 429/503 retries, explicit
//! timeouts, user-scoped endpoints, API-key redaction on every error path.

use std::thread;
use std::time::Duration;

use reqwest::Method;
use reqwest::Url;
use reqwest::blocking::Client as Http;
use reqwest::header::{HeaderMap, HeaderValue};
use serde_json::Value;

use crate::config::{Credentials, Error};
use crate::pyjson::fmt_g;

pub const DEFAULT_API_BASE: &str = "https://api.zotero.org";
const PAGE_LIMIT: usize = 100;
const MAX_RETRIES: u32 = 5;

type Params = Vec<(String, String)>;

struct Reply {
    status: u16,
    headers: HeaderMap,
    url: Url,
    body: String,
}

impl Reply {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }

    /// The `rel="next"` target of the Link header, if any.
    fn next_link(&self) -> Option<String> {
        let header = self.header("link")?;
        let mut rest = header;
        while let Some(open) = rest.find('<') {
            let close = open + rest[open..].find('>')?;
            let url = &rest[open + 1..close];
            let tail = &rest[close + 1..];
            let end = tail.find('<').unwrap_or(tail.len());
            if tail[..end].contains("rel=\"next\"") {
                return Some(url.to_string());
            }
            rest = &tail[end..];
        }
        None
    }
}

/// The parameters of a full item dump, as `ZoteroClient.all_items` takes them.
#[derive(Default)]
pub struct ItemsQuery<'a> {
    pub include_trashed: bool,
    pub include: &'a str,
    pub style: &'a str,
    pub if_modified_since: Option<String>,
}

pub struct ZoteroClient {
    http: Http,
    base: String,
    api_key: String,
    prefix: String,
    trace: bool,
    pending_backoff: f64,
    pub last_modified_version: Option<i64>,
}

impl ZoteroClient {
    pub fn new(creds: &Credentials, base: &str, trace: bool) -> Result<Self, Error> {
        let mut headers = HeaderMap::new();
        let key = HeaderValue::from_str(&creds.api_key)
            .map_err(|_| Error::Config("ZOTERO_API_KEY is not a valid header value".into()))?;
        headers.insert("Zotero-API-Key", key);
        headers.insert("Zotero-API-Version", HeaderValue::from_static("3"));
        let http = Http::builder()
            .default_headers(headers)
            .timeout(Duration::from_secs(30))
            // httpx never follows redirects, and reqwest would carry the custom
            // Zotero-API-Key header to whatever host Location names.
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| Error::Zotero(e.to_string()))?;
        Ok(Self {
            http,
            base: base.trim_end_matches('/').to_string(),
            api_key: creds.api_key.clone(),
            prefix: format!("/users/{}", creds.user_id),
            trace,
            pending_backoff: 0.0,
            last_modified_version: None,
        })
    }

    fn redact(&self, message: &str) -> String {
        message.replace(&self.api_key, "***")
    }

    fn request(
        &mut self,
        method: Method,
        url: &str,
        params: &Params,
        headers: &[(&str, String)],
    ) -> Result<Reply, Error> {
        if self.pending_backoff > 0.0 {
            eprintln!(
                "zelador: server asked for backoff — waiting {}s",
                fmt_g(self.pending_backoff)
            );
            thread::sleep(Duration::from_secs_f64(self.pending_backoff));
            self.pending_backoff = 0.0;
        }
        let target = if url.starts_with("http") {
            url.to_string()
        } else {
            format!("{}{url}", self.base)
        };
        let mut attempt = 0;
        let reply = loop {
            let mut builder = self.http.request(method.clone(), &target);
            if !params.is_empty() {
                builder = builder.query(params);
            }
            for (name, value) in headers {
                builder = builder.header(*name, value);
            }
            let response = builder
                .send()
                .map_err(|e| Error::Zotero(self.redact(&format!("{method} {url} failed: {e}"))))?;
            let reply = Reply {
                status: response.status().as_u16(),
                headers: response.headers().clone(),
                url: response.url().clone(),
                body: response.text().map_err(|e| {
                    Error::Zotero(self.redact(&format!("{method} {url} failed: {e}")))
                })?,
            };
            if self.trace {
                eprintln!(
                    "{method} {url} -> {} (version {})",
                    reply.status,
                    reply.header("Last-Modified-Version").unwrap_or("-")
                );
            }
            if matches!(reply.status, 429 | 503) && attempt < MAX_RETRIES {
                let wait = reply
                    .header("Retry-After")
                    .and_then(|v| v.parse::<f64>().ok())
                    .unwrap_or(2f64.powi(attempt as i32));
                eprintln!(
                    "zelador: HTTP {} — retrying in {}s",
                    reply.status,
                    fmt_g(wait)
                );
                thread::sleep(Duration::from_secs_f64(wait));
                attempt += 1;
                continue;
            }
            break reply;
        };
        if let Some(backoff) = reply.header("Backoff").and_then(|v| v.parse::<f64>().ok()) {
            self.pending_backoff = backoff;
        }
        if let Some(version) = reply
            .header("Last-Modified-Version")
            .and_then(|v| v.parse().ok())
        {
            self.last_modified_version = Some(version);
        }
        if reply.status >= 400 && reply.status != 404 {
            let snippet: String = reply.body.chars().take(200).collect();
            return Err(Error::Zotero(self.redact(&format!(
                "HTTP {} on {method} {url}: {snippet}",
                reply.status
            ))));
        }
        Ok(reply)
    }

    fn get(&mut self, url: &str, params: &Params) -> Result<Reply, Error> {
        let reply = self.request(Method::GET, url, params, &[])?;
        if reply.status == 404 {
            let snippet: String = reply.body.chars().take(200).collect();
            return Err(Error::Zotero(
                self.redact(&format!("HTTP 404 on GET {url}: {snippet}")),
            ));
        }
        Ok(reply)
    }

    /// Follow `Link: next` until exhausted; `None` means 304 Not Modified.
    ///
    /// A full page with no next link is not trusted as the end (/tags drops
    /// the link early), so the next offset is probed until a short page.
    /// `headers` go on the first request only: a 304 there means nothing changed.
    fn paginated(
        &mut self,
        path: &str,
        mut params: Params,
        mut headers: Vec<(&'static str, String)>,
    ) -> Result<Option<Vec<Value>>, Error> {
        if !params.iter().any(|(k, _)| k == "limit") {
            params.push(("limit".into(), PAGE_LIMIT.to_string()));
        }
        let mut url = path.to_string();
        let mut results = Vec::new();
        loop {
            let reply = self.request(Method::GET, &url, &params, &headers)?;
            headers.clear();
            if reply.status == 304 {
                return Ok(None);
            }
            if reply.status == 404 {
                return Err(Error::Zotero(format!("HTTP 404 on GET {url}")));
            }
            let page: Vec<Value> = serde_json::from_str(&reply.body).map_err(|e| {
                Error::Zotero(self.redact(&format!("GET {url} returned non-list JSON: {e}")))
            })?;
            let count = page.len();
            results.extend(page);
            if let Some(next) = reply.next_link() {
                url = next;
                params = Vec::new();
                continue;
            }
            let mut sent: Params = reply.url.query_pairs().into_owned().collect();
            let lookup = |sent: &Params, key: &str| {
                sent.iter()
                    .find(|(k, _)| k == key)
                    .and_then(|(_, v)| v.parse::<usize>().ok())
            };
            if count < lookup(&sent, "limit").unwrap_or(PAGE_LIMIT) {
                return Ok(Some(results));
            }
            let start = (lookup(&sent, "start").unwrap_or(0) + count).to_string();
            match sent.iter_mut().find(|(k, _)| k == "start") {
                Some(entry) => entry.1 = start,
                None => sent.push(("start".into(), start)),
            }
            let mut bare = reply.url.clone();
            bare.set_query(None);
            url = bare.to_string();
            params = sent;
        }
    }

    /// Full paginated dump; `None` when `if_modified_since` says nothing changed.
    pub fn all_items(&mut self, query: ItemsQuery) -> Result<Option<Vec<Value>>, Error> {
        let mut params: Params = Vec::new();
        if query.include_trashed {
            params.push(("includeTrashed".into(), "1".into()));
        }
        if !query.include.is_empty() {
            params.push(("include".into(), query.include.into()));
        }
        if !query.style.is_empty() {
            params.push(("style".into(), query.style.into()));
        }
        let headers = query
            .if_modified_since
            .map(|v| vec![("If-Modified-Since-Version", v)])
            .unwrap_or_default();
        let path = format!("{}/items", self.prefix);
        self.paginated(&path, params, headers)
    }

    /// A library setting (e.g. tagColors); `None` when unset.
    pub fn setting(&mut self, name: &str) -> Result<Option<Value>, Error> {
        let path = format!("{}/settings/{name}", self.prefix);
        let reply = self.request(Method::GET, &path, &Vec::new(), &[])?;
        if reply.status == 404 {
            return Ok(None);
        }
        serde_json::from_str(&reply.body).map(Some).map_err(|e| {
            Error::Zotero(self.redact(&format!("GET {path} returned invalid JSON: {e}")))
        })
    }

    pub fn all_collections(&mut self) -> Result<Vec<Value>, Error> {
        let path = format!("{}/collections", self.prefix);
        Ok(self
            .paginated(&path, Vec::new(), Vec::new())?
            .unwrap_or_default())
    }

    pub fn all_tags(&mut self) -> Result<Vec<Value>, Error> {
        let path = format!("{}/tags", self.prefix);
        Ok(self
            .paginated(&path, Vec::new(), Vec::new())?
            .unwrap_or_default())
    }

    /// Current library version in one cheap request.
    pub fn library_version(&mut self) -> Result<i64, Error> {
        let path = format!("{}/items", self.prefix);
        let params = vec![
            ("limit".into(), "1".into()),
            ("format".into(), "keys".into()),
        ];
        self.get(&path, &params)?;
        self.last_modified_version
            .ok_or_else(|| Error::Zotero("response carried no Last-Modified-Version".into()))
    }
}
