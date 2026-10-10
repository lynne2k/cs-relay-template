# Contributor and agent instructions

This repository is a public, portable workflow template. Treat real jobs and deployment details as private runtime data.

- Keep tracked manifests and examples relative to their containing job or example directory. Never introduce a developer's home path, SSH account, server address, cloud project ID, browser session, token or cookie.
- Leave deployment-specific fields empty in defaults. Read them from an explicit local configuration; fail with an actionable error when a remote action lacks configuration.
- Do not copy private production histories, source portraits, generated media or third-party plugin implementation into this repository.
- An independently reviewed public demo explicitly approved by the user may be externally linked from the README through the exact, hash-bound attachment exception in `docs/publication.md`; production photos, jobs and deployment details remain private.
- Keep original source files unchanged. Generated endpoints are derived, versioned assets with source hashes and review notes.
- Historical HLTV portraits come from the dated ranking page's five-player team card after a Profile/year/team check. Following the card into a live Team page loses the historical context. News illustrations and ranking graphics are evidence, not identity references.
- Under the user-provided policy, collect and review both Past and Present sources before endpoint generation. Keep identity and layout references separate.
- Reuse the same preprocessed Past endpoint as both the age clip's last frame and the handoff clip's first frame. Do not conceal mismatches by replacing a single output frame.
- Treat frame count, duration, resolution and FPS as explicit backend/preset parameters. Do not advertise 107 frames at 24 fps as a universal model rule.
- Captions change within a clip and across handoffs. Respect the declared label format and no-background style.
- A successful inference call or technical validation is not visual approval. Preserve pending/rejected review states and document unresolved model artifacts.
- Use the existing scripts and request/result contracts. Do not claim automatic ChatCut integration, cloud provisioning or image generation if only an operator handoff exists.
- Run relevant tests after code changes. Keep tests synthetic; do not make the test suite depend on a private server, authenticated browser or paid model request.
- Before committing, review staged files for credentials, private network information, personal paths and unlicensed example media. A scanner supplements this review; it cannot prove absence of secrets.
