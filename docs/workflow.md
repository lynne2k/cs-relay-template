# Production workflow

Run repository commands from the repository root. Paths recorded in a job are relative to that job. Examples below use invented labels, not photographs of real people.

## 1. Choose the source policy

Use `init JOB --asset-policy hltv` for verified team/year portraits. Use `init JOB --asset-policy user-provided` for a contributor's own reviewed Past/Present sources. Neither workflow should disable strict checking to bypass a missing source.

### Dated esports portraits

1. Open the player's HLTV Profile and establish the desired historical year and team. If selecting a peak year, retain the ranking evidence separately.
2. Open an HLTV ranking page with an explicit date, following `/ranking/teams/YYYY/month/day`. Find that team and its five-player portrait row on the dated page itself.
3. Save the portrait image actually referenced by the player's card, plus a screenshot or page capture proving the date, team and player. Do not follow the card into the team's live Team page: its current roster or picture does not establish the historical snapshot.
4. Review the face, jersey, team and year before registration. A date in a page URL is evidence to inspect, not proof that every image is historically correct.
5. Use `register-player --help` to supply the profile, dated snapshot, portrait and supporting captures. Mark the source approved only after review.

News photographs, top-player ranking cards, graphics and event reports can explain a choice of year, but are not substitutes for the generation portrait. When a dated portrait is unavailable, record a reviewed override with its actual source and reason; do not silently assign a guessed year or jersey. Teamless current states require explicit identity-only review and neutral wardrobe handling supported by the registration command.

This project does not automate authenticated HLTV scraping. Acquire sources through your own browser or an authorized tool, observing the source's access and reuse conditions. Do not copy browser cookies or session tokens into a job.

### User-provided Past/Present photos

Collect both periods for each person before generating their endpoint pair. Do not derive the past face just by asking a model to make the present face younger. Preserve source bytes and label uncertain dates `Past` and `Present` rather than inventing years.

For example, after placing your reviewed inputs outside the tracked example directory:

```sh
python scripts/cs_relay.py register-source jobs/example-relay \
  --player example-a --state present --period present \
  --photo private-inputs/example-a-present.jpg \
  --review-status approved --review-note "Present identity and source reviewed."
python scripts/cs_relay.py register-source jobs/example-relay \
  --player example-a --state past --period past \
  --photo private-inputs/example-a-past.jpg \
  --review-status approved --review-note "Past identity and source reviewed."
```

Repeat for `example-b`. The commands copy each source into the job and record its hash; they do not generate endpoint images. Keep personal downloads and real job directories outside public commits.

## 2. Prepare matched endpoint portraits

An **identity reference** determines the person's period-specific face, hair and clothing. A **layout reference** determines camera height, face position, head size, shoulder width, framing, background and light. Explicitly distinguish these roles in the image-generation request; do not transfer the layout reference's identity or jersey.

Use a separately supplied image-generation tool and retain its model/settings and prompt alongside the reusable asset. Generate a neutral eye-level, front-facing portrait pair with comparable framing. Avoid carrying a selfie's lens distortion or an upward-facing camera angle into the endpoint. Use the approved first endpoint as a layout reference when preparing the other period. Preserve natural differences in age and body shape; “aligned” does not mean artificially making different bodies identical.

Inspect the pair side by side, then at equal scale. Check the eye line, head size, shoulder positions, torso crop, background, eyewear and wardrobe. If an endpoint is already correct, reuse it by hash instead of generating it again. If it is wrong, create a new library version and retain the rejected version's review record.

Freeze a common layout anchor inside the job, for example `assets/layout/example-a-anchor.png`. It can be a copy of the approved first endpoint or a reviewed framing guide. Both periods' registration must reference the same anchor hash. Register the generated result with `register-endpoint`; the user-provided policy also requires a layout review note:

```sh
python scripts/cs_relay.py register-endpoint jobs/example-relay \
  --player example-a --state past \
  --endpoint private-inputs/example-a-past-endpoint.png \
  --layout-reference assets/layout/example-a-anchor.png \
  --layout-note "Eye level, head scale and torso framing match the reviewed present endpoint." \
  --review-status approved --review-note "Past face and clothing match the past source."
```

The reference path above is illustrative: use your actual job-relative anchor. Register the present endpoint with that same reference, not a different guide. A review flag is a declaration by the operator, not an automatic face/layout assessment.

## 3. Build and package a chain

Edit [the example plan](../examples/chain-plan.json). State keys must match the registered manifest. `current` in a chain node names its starting state; for a user-provided job that state can be named `present`. Prompt-file paths are relative to the plan's own directory.

```sh
python scripts/build_relay_chain.py jobs/example-relay examples/chain-plan.json
python scripts/cs_relay.py make-srt jobs/example-relay
python scripts/cs_relay.py validate jobs/example-relay
python scripts/cs_relay.py package jobs/example-relay
```

The builder replaces the job's segment list and caption events. Copy a plan or use a new job when experimenting with a different sequence. `seed_base` makes the seed sequence explicit; it does not promise cross-version model reproducibility.

For two people, the example produces `A Present → A Past`, `A Past → B Present`, then `B Present → B Past`. Set `terminal_age_transition` to false to omit the last half-cycle. Every full cycle contains two clips, not a stitched still-image transition.

Packaging standardizes endpoint dimensions before the worker sees them and reuses one prepared endpoint for a shared boundary at the same resolution. Keep adjacent segments at the same generation resolution. Do not independently stretch one occurrence and center-crop the other. Prepared files are derivatives; originals remain intact.

The included 4-second request prompt was used with a 107-frame, 24-fps output preset. Requested duration, native frame count and exported duration are different fields. Changing a model or preset requires rechecking all three. Do not add frames or speed changes merely to label a native clip “five seconds.”

## 4. Generate with the configured worker

Follow [the worker instructions](configuration.md). Run a workflow/runtime preflight and a small test before paying for a large batch. Preserve the request, endpoint hashes, seeds, workflow and model identifiers in your private job.

The generation template requests a physical shirt-removal action or a lateral handoff. Generative models may disregard those instructions. If a prompt repeatedly produces wrong clothing or identities, record the failure and revise the premise or endpoints; do not label a successful API response as accepted footage.

## 5. Review actual output, then edit

After result binding, run:

```sh
python scripts/qa_generated_segments.py jobs/example-relay
python scripts/qa_occlusion_windows.py jobs/example-relay
```

The scripts produce contact sheets and diagnostic comparisons. They are review aids, not automatic semantic approval. Check actual video playback as well as sampled frames:

- The correct person and period at both ends; no unwanted face morph or clothing swap.
- Consistent endpoint layout at every seam, including between a person's age and handoff clips.
- The intended action, sensible hands and cloth, and no unintended exposed torso or shortened inner layer.
- Expected resolution, FPS and frame count, and no hidden audio or model-generated speech.
- Correct changing captions, no opaque black caption band, and safe placement within the actual canvas.

Use [the editor handoff](editing.md) for editable captions. `assemble` creates a local FFmpeg preview; it is not an editable ChatCut project. Review any resulting letterboxing and caption appearance before sharing it.

`validate` reports technical consistency and visual-review state separately. `validate --mark` additionally requires an explicit approved `visual_review` record bound to `artifacts.master_burned` when burn-in is required, or `artifacts.master_no_subtitles` otherwise, including the reviewed file's path and SHA-256. Record what was actually watched; leave failures pending or rejected. An unreviewed or materially changed render must not inherit an earlier approval.

This marking applies to a local rendered artifact. It does not approve an external ChatCut timeline. Review that timeline in the editor, then bring its exported file back to the job and bind the appropriate render record before using local final acceptance.
