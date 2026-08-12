# domain_whois

A practical enrichment plugin: for every **domain** asset found during a scan it
queries the free, global RDAP bootstrap service (`rdap.org`) — the modern,
JSON-based wholesale successor to the whois protocol — and attaches the
authoritative WHOIS registration data to the asset. Everything lands in
`asset.properties["whois"]` and is therefore included in the exported JSON /
CSV / GraphML reports automatically.

## Why RDAP

RDAP (RFC 7480-7484) is served by every gTLD and country-code registry,
globally, in English, over HTTPS, with no API key and no command-line whois
tool. Where traditional `whois` output varies wildly between registrars, RDAP
is a single consistent JSON shape.

## Enriched fields

* `registrar`        — registrar organization (e.g. `NameCheap, Inc.`)
* `registrant`       — registrant organization / name when published
* `registrant_email` — registrant contact email when published
* `status`           — DNS/status flags (e.g. `client transfer prohibited`)
* `created` / `expires` — registration & expiration timestamps
* `nameservers`      — NS hosts (deduplicated, sorted)
* `dnssec`           — secureDNS delegation info when enabled
* `abuse`            — abuse email / phone when the registry publishes one
* `handle`           — registry record handle

## Behavior on big scans

* Sub-domain lookup is folded into the parent registrable domain
  (`www.example.com` → query `example.com`) and results are cached per
  registrable domain, so a whole subdomain tree costs a single API call.
* A hard per-scan budget (`max_queries`, default 200) prevents runaway traffic.
* All request errors are downgraded to debug logs; the scan is never blocked.

## Configuration

Default in `config.yaml`; override from `breeding-config.yaml`:

```yaml
plugins:
  dir: ""
  domain_whois:
    enabled: true
    max_queries: 200
    timeout: 20
    sources:
      - "https://rdap.org/domain/{domain}"
      - "https://rdap.verisign.com/{tld}/v1/domain/{domain}"
```

* `timeout` — per-source request timeout in seconds (default 20). Raise it if
  registries on your network are slow.
* `sources` — ordered list of URL templates tried in turn until one succeeds.
  `{domain}` is the registrable domain, `{tld}` the top-level label (e.g. `com`).
  Default is the global `rdap.org` bootstrap followed by Verisign's fast
  registry endpoint (covers `com`/`net`); add or reorder as needed. If every
  source fails, the asset's `whois` property records a short `error` instead of
  staying empty, so the JSON/HTML reports make the failure visible.

## Output

On scan completion `whois_report.json` is written into the current run
directory containing the query count and the per-domain cache.