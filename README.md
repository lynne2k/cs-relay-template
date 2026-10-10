# CS Relay Template

[中文说明](README.zh-CN.md)

A reusable, file-backed workflow for portrait-transition videos: collect and review sources, prepare matched endpoint portraits, generate an age transition and a handoff, then review and edit the sequence with changing captions.

The controller runs locally on macOS or Linux. A ComfyUI worker can run on the same machine or on a separately configured SSH host. ChatCut is an optional editing destination. No particular cloud, GPU model, home directory, account, or server is required by the manifest format.

This is a workflow template, not an automatic video-quality guarantee. Source selection, endpoint generation and visual approval still require human judgment. Image/video models, their weights and third-party editor integrations are separate dependencies.

## What is included

- Source and endpoint registration with hashes, provenance and review records.
- Separate policies for dated esports portraits and user-provided Past/Present photos.
- Alternating age/handoff chain planning, with an optional final age-only half-cycle.
- Prompt templates, job packaging, configurable SSH transfer and result binding.
- Caption events, SRT generation, local preview assembly and visual QA contact sheets.
- A portable editing handoff specification for ChatCut or another editor.

Private photos, historical production jobs, generated media, cloud provisioning settings and authentication material are not included. There is no bundled authenticated image downloader or ChatCut automation client.

## Quick start

Use Python 3.10 or later. Install FFmpeg and ffprobe using your operating system's package manager; SSH transfer also requires `ssh` and `rsync`. Optional QA scripts need the `qa` extra.

Run these commands from a checkout of this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[qa,dev]'
python scripts/cs_relay.py --help
python scripts/create_demo.py jobs/demo-relay
```

The demo creates synthetic geometric images, registers two subjects with four states, and packages a three-clip plan. It needs no real photographs, network request or GPU. Its review notes describe synthetic test inputs; no video is generated or visually approved.

For your own source photographs, create an empty job with `python scripts/cs_relay.py init jobs/example-relay --asset-policy user-provided`. For an SSH worker, make a private copy of [config.example.json](config.example.json), fill in its host alias and worker root, and pass it at initialization:

```sh
cp config.example.json config.local.json
# Edit config.local.json with your own deployment values.
python scripts/cs_relay.py init jobs/remote-example \
  --asset-policy user-provided --config config.local.json
```

Empty host/root fields deliberately leave remote transfer unconfigured. Read [configuration](docs/configuration.md) before setting up a worker.

Continue with [the production workflow](docs/workflow.md). The illustrative [chain plan](examples/chain-plan.json) uses fictional subject labels and contains no real media; register both subjects' source photos and endpoints before building it:

```sh
python scripts/build_relay_chain.py jobs/example-relay examples/chain-plan.json
python scripts/cs_relay.py make-srt jobs/example-relay
python scripts/cs_relay.py validate jobs/example-relay
python scripts/cs_relay.py package jobs/example-relay
```

`package` writes `remote/request.json` and a job-local payload after checking the assets. Uploading that package is not the same as submitting inference. The worker must implement the [request/result contract](docs/configuration.md). Once generated clips have been bound to the job, use the QA scripts and an editor to review the actual output.

## The continuity contract

A complete cycle is `A Present → A Past → B Present`. The exact same preprocessed `A Past` endpoint is used on both sides of that seam. The next cycle continues from `B Present`. Enabling a terminal age transition adds `last Present → last Past`.

One included preset expects 107 frames at 24 fps per segment, about 4.458 seconds. Two such segments make an approximately 8.917-second cycle. These are model/preset-specific values, not a universal five-second contract. Probe the generated output and update caption timing if a different backend produces different timing.

Shared inputs prevent inconsistent endpoint preparation; they do not guarantee that a generative model reproduces either endpoint exactly. Review the generated seam, not only the input images. The shirt-removal prompt can produce exposed torso, shortened inner shirts, warped logos or identity changes despite explicit negative instructions.

## Documentation

- [Source acquisition, matched endpoints and generation](docs/workflow.md)
- [Configuration, worker contract and portability](docs/configuration.md)
- [Reusable asset library](docs/assets.md)
- [ChatCut/editor handoff and captions](docs/editing.md)
- [Public repository boundary](docs/publication.md)
- [Job schema](schemas/job.v1.json) and [chain-plan schema](schemas/chain-plan.v1.json)

Run the local checks with `python -m unittest discover -s tests`. Schema files describe the manifest structure; `cs_relay.py validate` additionally checks local files and workflow rules. A technical pass is not visual approval.

## License

Code, documentation and included prompt templates are provided under the [MIT License](LICENSE). Third-party photos, trademarks, model weights and services retain their own terms; this repository does not grant rights to those materials. Only contribute examples you are entitled to redistribute.
