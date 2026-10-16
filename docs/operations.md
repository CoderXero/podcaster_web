# Operating the pipeline

## Current workflow

The home page starts a run by scanning a configured source folder. Entering
the two voice IDs is optional when the values in `ELEVENLABS_VOICE_IDS` are
correct; values entered in the form apply only to that run.

The worker currently advances through the following controlled sequence:

1. Discover Markdown source files.
2. Generate and validate a two-speaker script.
3. Stop at `AWAITING_APPROVAL` for human review.
4. Approve the script, then queue audio generation.
5. Synthesize one ElevenLabs segment per turn and create a validated MP3.
6. Create a remote Gumu-Gumu draft from the MP3 and script metadata.
7. Explicitly publish the draft and verify the remote `published` state.

Remote publication is never an automatic continuation from audio generation.
This keeps the paid and externally visible actions deliberate.

## Useful checks

```bash
curl -s http://127.0.0.1:8080/health/ready | jq
curl -s http://127.0.0.1:8080/api/v1/diagnostics | jq
curl -s 'http://127.0.0.1:8080/api/v1/jobs?limit=20' | jq
```

For a specific job:

```bash
JOB_ID=replace-with-job-id
curl -s "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID" | jq
curl -s "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/events" | jq
curl -s "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/package" | jq
```

The home page starts runs with per-stage toggles. Select the furthest desired
stage; prerequisite stages are enabled automatically. The archive toggle
controls whether the processed Markdown is moved to the local archive after
the script artifact is persisted. Script approval and remote mutations remain
explicit job-page actions.

Recent Work also provides `Continue` for queued, review-ready, and recoverable
failed jobs. `Purge` clears all indexed jobs and events after confirmation,
regardless of state, but preserves generated artifacts and manifests.

The equivalent script approval and audio actions are also available through
the API:

```bash
REVISION=$(curl -s "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID" | jq -r .revision)
curl -s -X POST "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/script/approve" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REVISION}" | jq
curl -s -X POST "http://127.0.0.1:8080/api/v1/jobs/$JOB_ID/audio/generate" | jq
```

The expected successful terminal state for a published episode is:

```text
status: succeeded
pipeline_stage: PUBLISHED_VERIFIED
remote.published.status: published
```

## Troubleshooting by stage

- `SCRIPT_GENERATING` / `LLM_UNAVAILABLE`: check the configured base URL,
  model server, reasoning effort, timeout, and output-token limit. The model
  must return valid JSON matching the two-speaker schema.
- `AUDIO_GENERATING` / `AUDIO_FAILED`: check the ElevenLabs key, exactly two
  voice IDs, and that `ffmpeg` is installed and executable.
- `REMOTE_DRAFT_FAILED`: check OAuth discovery/token settings, Gumu-Gumu base
  URL, the contract gate, and the MP3 package. A draft requires a valid
  `audio/mpeg` upload.
- `PUBLISH_REQUESTED` or `RECONCILIATION_REQUIRED`: do not blindly retry a
  remote mutation. Query the remote episode state and reconcile the job first.

Queued jobs can be cancelled or deleted from their job page. A claimed or
running job should be cancelled rather than deleted.
