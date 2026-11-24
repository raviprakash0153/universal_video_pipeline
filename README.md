# Universal Video Pipeline

## Local lifecycle database and approvals

The production pipeline records jobs, source provenance, rights state, rendered
media SHA-256 values, per-platform output metadata, validation results, approval
history and YouTube publishing attempts in a local SQLite database. By default
the database is `output/pipeline.sqlite3` (ignored by Git). Set
`PIPELINE_DATABASE=/some/path/pipeline.sqlite3` to choose another location.
Set `PIPELINE_USER` to the local reviewer identity; otherwise the CLI uses the
current account name. `PIPELINE_MEDIA_ROOT` is available for storage adapters
and future configuration; current renders continue to use the existing output
directory. `.env.example` contains placeholders only; the application reads
settings from its environment and does not load or log secrets from the DB.

Use the approval CLI to review each platform output separately:

```sh
python3 approval_cli.py list --state not_requested
python3 approval_cli.py inspect <output-id>
python3 approval_cli.py approve <output-id> --by Ravi
python3 approval_cli.py reject <output-id> --by Ravi
python3 approval_cli.py list --state approved
python3 approval_cli.py list --state rejected
python3 approval_cli.py list --state invalidated
```

`inspect` shows the source rights, platform metadata, media location and SHA-256.
Approval is refused for unknown, missing or prohibited rights, and for outputs
without registered media. Approval history is append-only; the database checks
the current rights, platform, metadata and media hash again before publishing,
and records an invalidation if the media or metadata changed. Reapproval always
requires another explicit `approve` decision. The current renderer registers
YouTube outputs because that is the only platform publishing adapter present;
other platform output records can be reviewed independently when those packages
are added. Cloud databases, cloud storage, scheduling and platform API
integrations remain future work.

## Phase 5B.3 news research and Telegram assistant

`command_engine.py` recognizes English, Hinglish, Hindi, and mixed-script commands.
News commands are handled by `news_research.py` through a replaceable multi-source
provider. X API v2, YouTube Data API v3, and the supported Instagram Graph API
surface are queried when configured. Google News RSS is used for discovery only;
article text is fetched from the original publisher before it can support a claim.
Missing APIs are reported as unavailable and do not prevent other providers from
returning partial results. Social posts alone cannot confirm a claim.

Optional research credentials (set in the environment; never commit them):

```sh
export X_BEARER_TOKEN='...'
export YOUTUBE_DATA_API_KEY='...'
export INSTAGRAM_ACCESS_TOKEN='...'
export INSTAGRAM_BUSINESS_ACCOUNT_ID='...'
# Optional: override the versioned Graph API endpoint (default v27.0).
export INSTAGRAM_GRAPH_API_VERSION='v27.0'
```

X requires API access that permits recent search. YouTube requires an enabled
YouTube Data API v3 project and API key. Instagram's Graph API only exposes
authorized professional-account media; it does not provide unrestricted public
search. Without a supported token and account ID, Instagram is explicitly marked
unavailable. No subscription is purchased by this tool. Official account
identity is treated as unknown until tied to a first-party company link; names,
badges, and lookalike handles do not establish authenticity. Rockstar and
Take-Two first-party domains and trusted independent publisher domains are
checked through RSS discovery and then read at the publisher URL.
Rockstar's linked X, YouTube, and Instagram identities come from its [official
contact page](https://www.rockstargames.com/contact); Take-Two social accounts
are not marked official without a corresponding first-party reference.

Research is kept separate from footage rights checks and video production.

### Daily research operations

Run a normal local research pass. It prints progress, query/provider outcomes,
topic/category and verification counts, and the ranked shortlist, then saves the
run, source status, article snapshots, claims, evidence, and briefing in SQLite.
If Telegram credentials are present, it sends to the authorized numeric user ID;
without them, it prints a preview and keeps the briefing locally.
The daily research CLI uses free RSS and publisher pages only; it does not call
the optional account APIs described above.

```sh
python3 research_cli.py run
# Preview only: makes bounded research requests, saves only preview job status, sends nothing
python3 research_cli.py run --preview
# Provider diagnostics (bounded calls; prints timings and failures)
python3 research_cli.py run --diagnostic
# Narrow a run
python3 research_cli.py run --category science --category technology_ai --coverage india
```

Set `NEWS_REQUEST_TIMEOUT` (default 8 seconds) and `NEWS_RETRY_LIMIT` (0 or 1;
default 0) to tune bounded requests. Missing dates and stale stories are excluded
from latest research; publisher access failures and verification uncertainty
remain visible in the per-query metrics and saved snapshots.

Research categories use headline subject signals. Political and legal proceedings
take precedence over incidental technology or science mentions, and international
conflicts take precedence over other subjects. Technology coverage includes
processors, chips, semiconductors, data centers, robotics, software,
cybersecurity, AI, and related computing terms. Generic mentions of
“technology” alone do not qualify. A topic is eligible when at least one of its
classified `relevant_categories` is selected; a mixed-category story can qualify
through any selected category, while a story relevant only to unselected
categories is removed after cross-query merging.

The final shortlist is the first ten ranked topics. For
`--coverage india_and_global`, it includes India and global discovery results
when both have eligible topics, with score order adjusted only as needed to
include both. These labels describe the query that found a story; they do not
verify the story’s geographic impact. A single available geography remains
eligible without an invented counterpart. The shortlist limits each publisher
identity to two topics when alternatives exist. Original-source URLs identify
syndicated copies as one publisher; the limit relaxes when no eligible
alternative remains. CLI output reports when either diversity rule changes the
shortlist. No additional configuration is required.

Each query tracks discovered articles, rejected or inaccessible articles,
accepted evidence, clustered topics, cross-query merges, category assignments,
selected-category exclusions, and the final shortlist. A zero-result explanation
identifies the stage where results stopped. Inaccessibility is reported as an
access or quality failure, not as verified irrelevance. Routine output gives
stage counts and concise explanations; `--diagnostic` prints detailed provider
and rejection diagnostics, and `--verbose` prints all rejection records.

### Phase 5B.6 research quality evaluation

The quality evaluator checks the existing research pipeline locally. Run the
labelled synthetic fixtures or request detailed diagnostics:

```sh
python3 research_cli.py evaluate-quality
python3 research_cli.py evaluate-quality --diagnostic
python3 research_cli.py evaluate-quality --history
python3 research_cli.py evaluate-quality --history --db /path/to/pipeline.sqlite3 --run-id <run-id> --diagnostic
```

The category table counts **synthetic fixture rows** assigned to each category;
these are not discovered production articles or discovery-funnel counts.
Historical snapshots do not retain candidate-stage counts, query settings, or
shortlist selection separately, so those measures are unavailable. No shortlisted topics alone does not establish a discovery
failure, and unrequested categories are not failures. Geography counts describe
India/global discovery provenance, not geographic impact. Publisher identity
uses an original-source URL when recorded; unknown identities remain visible.
Evidence summaries do not treat multiple URLs as independent corroboration.

Synthetic expected labels are independent from predictions produced by the
production classifier. Classification reports micro-averaged precision, recall,
and F1 over category assignments; undefined zero-denominator metrics are shown
as unavailable. Titles and labels exercise recognized and missed categories,
verification states, publisher identity, duplicate headlines, and missing data.
Duplicate and syndication indicators are approximate synthetic fixture checks,
not production clustering or independent-source verification; URL differences
do not prove source independence, and unknown identities remain separate. Their
classification metrics measure only those fixtures and do not demonstrate
real-world news coverage quality. Historical classification accuracy is
unavailable because snapshots have no independent ground-truth labels. History
mode opens SQLite with read-only URI access and does not modify or create the
database. Evaluation makes
no network requests, publishes nothing, and sends no messages.

### Phase 5B.7.3 offline research evaluation

Compare saved evaluation bundles against independent annotations, entirely
offline. Repeat `--bundle` to compare runs; `--output` writes deterministic
JSON alongside the readable terminal report:

```sh
python3 research_cli.py evaluate-bundles --bundle output/evaluation/run-a.json
python3 research_cli.py evaluate-bundles --bundle run-a.json --bundle run-b.json --output evaluation-results.json
```

Metrics, denominators, annotation gaps and unsupported measures are documented
in [docs/real_world_evaluation.md](docs/real_world_evaluation.md).

Telegram credentials are read from the shell environment. To enter the token
without echoing it or placing it in shell history, use:

```sh
read -s "TELEGRAM_BOT_TOKEN?Telegram bot token: " && export TELEGRAM_BOT_TOKEN
read "TELEGRAM_ALLOWED_USER_ID?Your numeric Telegram user ID: " && export TELEGRAM_ALLOWED_USER_ID
```

Perform a deliberate delivery, retry a definite Telegram rejection, and inspect
the saved run and delivery state. `research_cli.py run --preview` prints the
complete Telegram message without saving research results or sending it (the
SQLite operational job row records that a preview ran):

```sh
python3 research_cli.py deliver --run-id <run-id>   # sends once
python3 research_cli.py deliver --run-id <run-id> --retry
sqlite3 "${PIPELINE_DATABASE:-output/pipeline.sqlite3}" \
  'SELECT run_id,status,attempts,chat_id,message_id,last_error FROM telegram_delivery;'
sqlite3 "${PIPELINE_DATABASE:-output/pipeline.sqlite3}" \
  'SELECT r.run_id,r.created_at,t.headline,t.category,t.verification_status,t.score FROM research_runs r JOIN research_topics t USING(run_id) ORDER BY r.created_at DESC,t.score DESC LIMIT 30;'
sqlite3 "${PIPELINE_DATABASE:-output/pipeline.sqlite3}" \
  'SELECT run_id,topic_id,publisher,publication_date,url FROM discovered_articles ORDER BY run_id DESC LIMIT 30;'
```

A transport timeout can happen after Telegram accepted a message. Such outcomes
are recorded as `uncertain` and automatic retry is suppressed to avoid sending a
duplicate. A definite Telegram rejection is recorded as `failed` and supports an
explicit retry. Briefings remain stored in SQLite in either case. The polling
bot preserves the private-chat and configured-user authorization checks; send
`brief`, then reply with a listed topic number or a custom topic. Custom topics
are stored as `UNRESEARCHED` until sources are checked.

For a delivery check, first run a normal research pass in the same Terminal
session with the two environment variables set, then use the printed
`deliver --run-id ...` command (or run `research_cli.py run` again to research
and send a new briefing). The automated suite mocks all Telegram/network calls.

### Optional local daily scheduler (macOS launchd)

Configure a daily time, IANA timezone, categories, and coverage. The launchd
configuration checks once per minute, applies the selected timezone in Python,
and launches at most one completed daily run. An OS lock blocks overlapping
processes; startup marks orphaned running records as interrupted before the next
attempt. Status reports the last job and next due time.

```sh
python3 research_cli.py schedule configure --time 08:15 --timezone Asia/Kolkata \
  --coverage india_and_global --category technology_ai --category business_economy
python3 research_cli.py schedule status
python3 research_cli.py schedule pause
python3 research_cli.py schedule resume
python3 research_cli.py schedule plist --plist research.launchd.plist
# Review the generated plist, then explicitly install and load it:
python3 research_cli.py schedule install --plist research.launchd.plist
# Explicitly unload and remove it:
python3 research_cli.py schedule remove
```

The configuration and plist are generated locally. The install command is the
only command above that activates launchd, and it is never run automatically.
macOS launchd only runs while the Mac is awake and available; it cannot run
while the computer is shut down. The scheduler calls the research CLI boundary,
so a future cloud scheduler can replace this adapter without changing research.
SQLite and all collected research stay local.

The Telegram adapter accepts text and voice messages (voice is locally
transcribed using the existing Whisper dependency). For example:

```sh
export TELEGRAM_BOT_TOKEN='...'
# Recommended: restrict replies to your Telegram numeric user ID.
export TELEGRAM_ALLOWED_USER_ID='123456789'
python telegram_text_dry_run.py --dry-run
```

Create a bot with Telegram's BotFather, open a private chat with it, and send
`Fetch the latest news about GTA 6`, `Verify this news I heard: GTA 6 is delayed
again`, or a voice equivalent. The assistant returns a concise report with
source links, publication dates when found, evidence, and a verification status.
It asks whether Shorts, long, or both formats are desired when unspecified; that
answer is only captured as an output preference and does not start production.
Only Telegram polling/replies and news RSS/article requests happen at runtime.
The bot token is read from the environment and is not logged. Stop with Ctrl+C.
`NEWS_REQUEST_TIMEOUT` and `NEWS_RETRY_LIMIT` optionally configure provider
timeouts (seconds; default 12) and retry count (default 1).

### Safe live Google News resolver diagnostic

To collect live evidence for a selected article on macOS, run this from Terminal
(replace the topic if needed):

```sh
cd ~/Documents/universal_video_pipeline && umask 077 && set -o pipefail && report="$HOME/google-news-selected-article-diagnostic-$(date +%Y%m%d-%H%M%S).txt" && python3 telegram_text_dry_run.py --dry-run --diagnose-selected-news "GTA 6" | tee "$report" && printf '\nDiagnostic report: %s\n' "$report"
```

This uses Google News RSS discovery and the selected-article research path, but
does not poll Telegram or send a message. It requires no bot token. The local
report records resolver stages, publisher URL checks, article retrieval and
extraction results. It omits request bodies, credentials and authorization
headers, and redacts URL query values. It is evidence gathering only; the
diagnostic does not change resolver behavior, timeouts, verification rules or
rights safeguards.

To diagnose the two target Google News items directly, without discovery or
relevance ranking, pass their URLs to the isolated URL mode:

```sh
python3 telegram_text_dry_run.py --dry-run --diagnose-news-urls \
  'https://news.google.com/rss/articles/CBMiiAFBVV95cUxNdW9aZ3o1S3I4RVZfX016SXRVZkM1RER0VmY4Rm9udDZWM2lGUEcwSERkY0x3ejJNODNqd2tFQVhtRmUzdXV0VmE0TEZiZ2hXb0I4TVRNNTlKb0JITlRFbWpBX20tNW9wZVRxTHlIMi1oRUZWQ0RpMUU4UzVHbHJyUURQVlBfcFVN?oc=5' \
  'https://news.google.com/rss/articles/CBMiaEFVX3lxTE9kVjVUSDRhOFdEeDNEM0J3Wm9IdFNjWDNEdm1FQUdRMXZvUW9YaC1Vek5ZZHRuTmtvWkY0anVaV282aXVLMl9rTHd3bVpDM1lvZjIzQ2szVEtlLS1QaGxoZ2pwV3ZkZmZk?oc=5'
```

Each result reports signature-page fetch and classification, signature and
timestamp extraction, batchexecute request/response and parsing, HTTPS and
publisher-domain validation, and publisher article extraction. The two calls
run independently with the configured request timeout. Query values and raw
exception messages are omitted; exception type and safe failure category remain.

- `Make 3 Shorts from https://...` preserves the requested count.
- `Make a long video from https://...` selects long format.
- `Make both formats` selects both but asks for a URL.
- For video commands, if no format, URL, or count was supplied, the response asks for clarification.
- Caption language defaults to the pipeline's existing `original` setting;
  Hinglish is selected only when explicitly requested.

A local, cross-platform Python CLI for producing:
- YouTube Shorts in 9:16 (1080x1920), with optional word-highlight karaoke captions.
- Long-form videos in 16:9 (1920x1080).
- Optional YouTube uploads using OAuth.

No Antigravity, Docker, Node.js path, or external repository is required.

## Important security note
Do not copy the uploaded `client_secret.json` or `token.json` into this project. Those files contained OAuth credentials and were exposed in this chat. Revoke the existing Google OAuth grant, rotate/delete the exposed OAuth client secret in Google Cloud, create a new OAuth Client ID of type **Desktop app**, and save its JSON here as `client_secret.json`. The pipeline creates its own `youtube_token.json` after a new browser authorization. Keep all three files private and out of Git/cloud sharing.

## 1. Install prerequisites

Install Python 3.10+ and FFmpeg (both `ffmpeg` and `ffprobe` must work in Terminal).

macOS with Homebrew:
```bash
brew install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows:
```powershell
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```
Install FFmpeg separately and ensure `ffmpeg` and `ffprobe` are on PATH.

Linux: install FFmpeg with your distribution package manager, then create the venv and install requirements.

## 2. Make Shorts

One clip:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" --clip 00:02:10-00:02:45
```

Three manually selected clips:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" \
  --clip 00:02:10-00:02:45 \
  --clip 00:08:20-00:08:55 \
  --clip 00:14:00-00:14:35
```

Create evenly spaced candidate clips (not a true viral-moment detector):
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" --auto-clips 3 --clip-length 35
```

Disable subtitles:
```bash
python video_pipeline.py shorts --source "input.mp4" --clip 00:00:20-00:00:55 --no-subtitles
```

Add a title and channel lower-third (disabled by default):
```bash
python video_pipeline.py shorts --source "input.mp4" --clip 00:00:20-00:00:55 \
  --banner --channel-name "My Channel"
python video_pipeline.py long --source "input.mp4" --banner --channel-name "My Channel"
```

For URL sources, the banner uses the source title and uploader/channel metadata. For local files,
it uses the filename as the title and `--channel-name` for the channel line. `--title` overrides
the displayed title. The banner uses `/System/Library/Fonts/Geneva.ttf`.

## 3. Make a long-form 16:9 video

Whole source:
```bash
python video_pipeline.py long --source "YOUTUBE_URL"
```

Trim a section:
```bash
python video_pipeline.py long --source "YOUTUBE_URL" --start 00:12:00 --end 00:23:30
```

## 4. Upload to YouTube

Enable YouTube Data API v3 in your Google Cloud project. Configure a Desktop app OAuth client, save its JSON as `client_secret.json`, and run:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" \
  --clip 00:02:10-00:02:45 --upload --privacy private
```
The first upload opens a browser authorization window. Start with `--privacy private`; review the video in YouTube Studio before making it public.

## 5. Output folders

- `output/`: final MP4 deliverables. They are not auto-deleted.
- `work/`: downloaded source, intermediate trim and temporary subtitle files.
- `youtube_token.json`: new OAuth token created after authorization.

Do not run aggressive cleanup while a render is active. Delete `work/` manually after confirming the final outputs.

## Important limitations / design choices

- The automatic clip option spaces clips across the source; it does **not** claim to detect viral moments. Real viral-moment selection requires transcript/semantic scoring and human review.
- The Shorts layout uses a blurred full-screen background with a centered horizontal foreground. It does not automatically detect or isolate a facecam. A facecam-specific layout should be configured for the exact source layout or added using face detection.
- The pipeline re-encodes audio and video to normalize timestamps and avoid independently copying DASH audio/video segments.
- The output is H.264/AAC MP4. The Intel Mac can render on CPU; expect slower processing than a modern GPU.
- Only download and republish content you own or have permission to use. Editing alone does not establish copyright permission.
