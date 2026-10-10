# Public repository boundary

Publish code, schemas, prompt templates, synthetic fixtures and generic documentation. Keep operational material outside the release:

- Real original photos, derived portraits, generated clips and their licensing/provenance records.
- Production jobs, local configurations, absolute filesystem paths and host/account identifiers.
- SSH keys, authorization headers, API credentials, browser profiles, cookies and downloaded login pages.
- Cloud workload IDs, internal endpoint URLs, editor project/asset IDs and service logs.

The examples deliberately use empty deployment fields, relative paths and invented subject labels. A loopback URL documents a local service; it is not a shared remote endpoint. Users supply their own installation root, credentials and host configuration at runtime.

Review staged content as well as the working tree before publishing. Ignore rules do not remove a file that was already tracked, and deleting a sensitive file from the latest tree does not remove it from earlier commits. If credentials ever enter a public history, revoke/rotate them and follow the hosting provider's history-removal procedure.

Automated scans can identify common secrets and private paths, but cannot establish that a repository is safe to publish. Inspect added examples, screenshots, test failure output and diagnostic reports. A public issue should use a minimal sanitized reproduction rather than an uploaded production job.

The MIT license covers the contributed software and documentation, not third-party people photographs, trademarks, model weights, services or private plugin code. Contributors must have permission to redistribute any added sample media. Synthetic local fixtures are sufficient for tests; real identities and paid inference are not required.
