# Reusable assets

A person belongs to an asset library; a video job references a selected version of that person. Changing a video's cast should not require regenerating an already approved portrait.

Keep your private library separate from this public repository. The following is an organizational convention, not a bundled database service:

```text
asset-library/
  people/
    example-a/
      present-v1/
        source/
        evidence/
        endpoint/
        metadata.json
      past-v1/
        source/
        evidence/
        endpoint/
        metadata.json
```

For esports, use versions that also capture year and team. For undated sources, use `past` and `present` plus an immutable version. Never overwrite an approved image in place while preserving its old hash or review.

Each version should retain:

- Original source files, their SHA-256 hashes, source URLs or user-supplied provenance, and capture date when known.
- The identity/period/team decision, with evidence and a reviewer note. Keep uncertainty explicit.
- The generated endpoint and its hash, generation tool/model/settings/prompt, and source identity hash.
- The separately identified layout reference and its hash, plus a review of eye line, head scale, shoulders and crop.
- Approved, pending or rejected status, and the reason for the decision.

Registration copies the selected source and endpoint into a job, recording hashes. This small snapshot makes a job portable and protects it from later library edits. Reuse means selecting the same reviewed library version and bytes; it does not require a fragile absolute symlink back to one workstation.

The public controller's job manifest is the enforceable contract. Library-wide search, deduplication and version selection are operator-managed in this release. Do not describe the folder convention as an implemented asset-management service.

Packaging makes a further endpoint derivative at the exact generation dimensions. A shared seam must reference the same prepared bytes on both sides. If a source or layout changes, regenerate/review the affected endpoint and repackage every segment that uses it. Replacing a single output boundary frame cannot correct inconsistent body scale or motion in surrounding frames.
