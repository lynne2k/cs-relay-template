# Configuration and worker contract

## Local configuration

Copy `config.example.json` to a private `config.local.json` and pass `--config config.local.json` to `init`. The resulting job keeps its runtime configuration in `remote/config.json`. These files belong to your deployment, not to a public example.

| Field | Meaning |
| --- | --- |
| `remote_host` | Your configured SSH alias. Empty by default; required for SSH transfer. |
| `remote_project_root` | Your worker's ComfyUI root. Empty by default; required for SSH transfer. |
| `remote_jobs_root` | Input directory relative to that root; default `input/cs_relay_jobs`. |
| `remote_results_root` | Output directory relative to that root; default `output/cs_relay_jobs`. |
| `remote_comfyui_url` | The worker-side ComfyUI HTTP endpoint; the example uses loopback. Pass the matching value to the worker's `--url` option. |
| `default_video` | Local assembly canvas and FPS. Match the generation plan to avoid unintended padding or rate conversion. |

The worker's `--jobs-subdir` is relative to ComfyUI's `input/`; `--results-subdir` is relative to `output/`. Both default to `cs_relay_jobs`. To customize them, set the controller's `remote_jobs_root` to `input/` plus the chosen jobs subdirectory, and `remote_results_root` to `output/` plus the chosen results subdirectory. Changing a transfer destination alone does not reconfigure the worker. Likewise, the worker's endpoint is set by its own `--url` flag.

Do not place passwords, API tokens, SSH keys or browser cookies in this configuration. Authenticate with your environment's existing SSH or service credential mechanism. `127.0.0.1` always refers to the machine running the process; a local controller cannot use a remote machine's loopback address without a deliberately configured tunnel.

`sync-up` and `sync-down` accept `--host` and `--remote-root` for a per-command override. Use their `--dry-run` mode to inspect the transfer destination before the first upload. Uploading a request is not inference submission.

## Included ComfyUI worker

The worker script constructs a MiniMax H3 ComfyUI API graph from the request. It does not provision a GPU, start a cloud instance, install ComfyUI, download model weights or manage provider billing. Install ComfyUI and the required model weights in your own environment before use. The current graph targets ComfyUI v0.39.2 core nodes; no extra custom-node pack is required. See the [visual workflow and dependency guide](comfyui.md) for the fixed revision, model downloads and importable canvas files.

The graph expects `MiniMaxH3ImageToVideo` with `first_frame` and `last_frame`, the supporting model/sampling/image nodes, and the video/audio decode and save nodes listed in [`api_payload`](../scripts/comfyui_worker.py). `DEFAULT_MODELS` in that file names the included quantized H3, LoRA, text-encoder and VAE preset. Those filenames describe one supported configuration, not downloads shipped with this repository. A segment's `model` object can override the preset, but its names and options must be accepted by the installed node versions. Save your tested ComfyUI/node revisions and model hashes with the private deployment so it can be reproduced.

Set `COMFYUI_ROOT` locally in the worker shell to your chosen installation directory. It is intentionally not assigned a deployment-specific value here. Put the package at `input/cs_relay_jobs/example-relay` beneath that root and make this repository's scripts available on the worker.

```sh
python scripts/comfyui_worker.py example-relay \
  --comfyui-root "$COMFYUI_ROOT" --dry-run
python scripts/comfyui_worker.py example-relay \
  --comfyui-root "$COMFYUI_ROOT" --runtime-validate --resume
```

Use `python scripts/comfyui_worker.py --help` for current flags and `--url` when ComfyUI is not reachable through the default worker loopback endpoint. Runtime validation checks the live node interface; it does not establish output quality or free GPU capacity. Model/preset changes can alter memory use, speed, valid resolutions and native frame counts.

The worker includes a named H3 model-file preset in `DEFAULT_MODELS`; override fields in each request segment's `model` object when your installed filenames differ. Filenames are relative names understood by ComfyUI's loaders, not deployment filesystem paths. The live `--runtime-validate` check verifies model availability and the first/last-frame node inputs, and records a hash of that node's schema. This repository documents a target ComfyUI revision but does not distribute its checkout or model weights. Record the versions actually installed, model hashes and your runtime verification privately for reproducible inference.

`--dry-run` writes prepared API graphs for a new job and never submits inference. For a job already submitted it preserves the existing submission receipts. `--resume` checks request/input hashes and completed output hashes before reuse. If the process lost a submission receipt during a network failure, it stops with `submission_unknown`: inspect your ComfyUI queue/history and reconcile the receipt before retrying. Automatically submitting again in that situation could duplicate billable work.

For an SSH worker, the local transfer commands are:

```sh
python scripts/cs_relay.py sync-up jobs/example-relay --dry-run
python scripts/cs_relay.py sync-up jobs/example-relay
# Execute the worker command in your worker shell and wait for completion.
python scripts/cs_relay.py sync-down jobs/example-relay
```

For a same-machine worker, copy the packaged request and payload into its configured input directory yourself and use the same worker. SSH and rsync are transport options, not model dependencies. Import completed results without SSH:

```sh
python scripts/cs_relay.py import-result jobs/example-relay \
  "$COMFYUI_ROOT/output/cs_relay_jobs/example-relay"
```

This checks the result schema, job and segment IDs, requested-job hash and output hashes before copying files into the job and binding segment video paths. Conflicting existing results are rejected rather than silently overwritten. A result from a different backend can use the same import command if it implements the contract below. Merely copying videos into the job does not register them.

## Request/result boundary

The controller writes `JOB/remote/request.json` and `JOB/remote/payload/`. The request contains an ordered `payload.segments` list. Each segment carries a stable `segment_id`, workflow kind, relative `first_frame` and `last_frame`, prompt, seed, dimensions, FPS, requested duration and optional model overrides.

The included worker reads from its input job directory and writes `result.json` plus `segments/*.mp4` under its output job directory. A compatible external worker can implement the same boundary. Result paths must be relative to the result directory, with no parent-directory traversal. A minimal result shape is:

```json
{
  "schema_version": "cs-relay.result.v1",
  "job_id": "example-relay",
  "request_sha256": "REPLACE_WITH_ACTUAL_REQUEST_SHA256",
  "status": "completed",
  "segments": [
    {
      "segment_id": "01-example-a-age",
      "video_path": "segments/01-example-a-age.mp4",
      "sha256": "REPLACE_WITH_ACTUAL_FILE_SHA256"
    }
  ]
}
```

This is a structural example, not a valid completed job. Return every requested segment with its actual hash; never manufacture successful entries for missing clips. Preserve the actual request-file hash so a result cannot silently be attached to a different packaged request. The controller verifies the files against the applicable result-import checks.

Keep provider-specific deployment details in a private adapter/configuration. Do not add cloud project IDs, actual worker addresses or an operator's directory tree to templates to make one installation convenient.
