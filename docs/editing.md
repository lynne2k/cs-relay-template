# Editable timeline handoff

ChatCut is optional. This repository defines the files and checks to carry into an editor; it does not bundle ChatCut's private plugin source, an authenticated client or an existing project. Use the editor's supported import/editing tools and your own account.

## Handoff bundle

Keep these together in the private job:

| Artifact | Purpose |
| --- | --- |
| `job.json` | Ordered segments, selected endpoints, captions and provenance. |
| `remote/results/result.json` | Result-to-segment mapping and output hashes. |
| `remote/results/segments/*.mp4` | Original individual generated clips. |
| `captions/events.json` | Editable text intervals in seconds. |
| `captions/final.srt` | Text/timing interchange; it does not encode the complete visual style. |
| `renders/timeline.json` | Local assembly record, if a preview was assembled. |
| `qa/` | Contact sheets and diagnostics, if generated. |

Record the editor's returned project, timeline, asset and item identifiers in a private job-side handoff record after import. Do not hardcode those identifiers in reusable scripts or publish the record. Reopen the returned project to verify the persisted result rather than assuming a successful tool response means all clips are usable.

## Timeline construction

1. Match the canvas and FPS to the selected native output preset.
2. Import the individual original clips and place them in manifest order. Do not use a flattened preview as the only editable source.
3. Check actual clip frame counts before assigning starts and caption boundaries. The example's 107-frame segments are preset-specific.
4. Retain the original media's audio if it is wanted and reviewed; otherwise mute it explicitly. Check for unintended model-generated speech. Add music only as a separate, deliberately selected track.
5. Add editable text overlays or the editor's supported caption items using the events below.
6. Review the composed viewer and playback before exporting. Export is a separate delivery step, not evidence of visual approval.

## Caption contract

Esports labels use `Name | Team | Year`. Undated labels use `Name | Present` and `Name | Past`. Team/year information must match the displayed endpoint, not the person's present-day team.

The labels change within each clip:

| Clip | Starting label | Ending label |
| --- | --- | --- |
| A's age transition | `A | Present` | `A | Past` |
| A-to-B handoff | `A | Past` | `B | Present` |
| B's optional final half-cycle | `B | Present` | `B | Past` |

The chain builder initially switches at each clip's midpoint. Inspect the actual generated action and adjust the editable event boundary if the subject or age is revealed earlier/later. The caption must describe what is visible, not just follow an arbitrary timer.

Use readable white text with an optional subtle outline/shadow, centered in a safe lower area. The text background is transparent: no black band, box or border beneath it. Size and margin should scale with the actual canvas. Avoid fixed coordinates from another person's project. An optional fade should not leave a misleading label visible through an identity change.

An SRT carries text and time, not complete style. A rendered SRT preview is not proof that the editor project has the agreed placement and no-background treatment. Verify both start/end labels and the change inside every clip on the composed timeline.

## Acceptance and recovery

Check asset availability, the complete sequence, lack of unintended gaps, total duration, all caption changes and the optional terminal half-cycle. Preserve the generated clips and handoff mappings so missing media can be relinked by hash. A new editor project does not require new model inference.

If authentication expires, use the editor's normal sign-in flow; never commit a session cookie or copy another person's login state. If an operation fails, retain the existing project and determine whether it was applied before retrying an insert or export.
