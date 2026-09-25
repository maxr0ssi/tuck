# Setup and development

A local macOS menu-bar app that suggests a name and folder for new downloads. Review each suggestion, edit it if needed, then **Move** or **Leave**. After filing, a quiet confirmation line offers Undo.

The app watches for completed downloads; you do not need to press a button in your browser. Notifications are generic and open the review queue. Nothing moves automatically.

## Install

Requires macOS on Apple Silicon, Python 3.12+, and Xcode Command Line Tools (`xcode-select --install`). The UI uses native AppKit; the watcher uses Python's standard library. Optional local classification uses [TinyJev](https://pypi.org/project/tinyjev/) through MLX.

```sh
git clone <your-repository-url>
cd tuck
./scripts/install.sh
.venv/bin/python -m download_suggest download-model
open ~/Applications/Tuck.app
```

If `python3` is older than 3.12, set `DOWNLOAD_SUGGEST_PYTHON` to a newer interpreter when running the installer. Keep the checkout and its `.venv` in place. This source install registers their paths as the app's runtime. The build is locally signed, not notarized or packaged for distribution.

The one-time model download contacts the model host for weights. Normal inference runs with Hugging Face and Transformers offline settings. The app has no cloud classification service or document upload feature. Model weights have their own upstream license, separate from this project's MIT license.

To use rules without downloading a model, install with `pip install -e .`, run `./scripts/build_app.sh`, copy the built app to Applications, then run `configure --rules-only` and `register-runtime --extractor` with the installed executable path. All commands are available through `.venv/bin/python -m download_suggest`.

## Your folders stay private

Setup creates a directory-only map under `~/Library/Application Support/Tuck/config.json`. It skips hidden directories and common build or cache folders, and does not descend into Git repositories. Review this map and remove destinations you do not want suggested. Rebuild it after reorganizing Documents.

```sh
.venv/bin/python -m download_suggest configure --replace
.venv/bin/python -m download_suggest doctor
```

The private config contains `folders` with stable IDs, unique labels, descriptions, and existing destination paths. Labels must be distinct even when letter case differs. Add descriptions to improve classification. Optional `rules` match keywords in a filename or bounded text excerpt and take priority over the model. The `instructions` field controls the model's folder choice. See [the fictional example](../examples/config.example.json); create those example folders before using it, or substitute your own existing folders **only in your private configuration**. Restart the app after editing configuration.

Private configuration, the queue, move history, runtime paths, and the local session credential live outside the checkout. Local files under `.private/` or `.local/`, model weights, logs, databases, `.env`, build output, and the virtual environment are also ignored by Git. Do not force-add them. Example configuration and tests contain fictional data only.

Run `python3 scripts/check_public.py` before sharing and inspect the staged diff yourself. This guard catches common private paths, credentials, documents, and binaries; it cannot recognize every kind of personal information. Installing or running the app never pushes Git changes.

## Local decision history

`~/Library/Application Support/Tuck/recommendations.jsonl` records suggestions and decisions as append-only events. Each event has an item ID and timestamp so recommendations can be matched to their outcomes.

The log separates proposed names and folders from requested names and folders. It marks edits explicitly and records attempts, successful moves, failures, Leave and Undo. Model details, exact prompt instructions, package version and source hashes make later comparisons possible. Document bodies and extracted excerpts are excluded.

Existing queue records are imported once and marked reconstructed. Unknown historical decision times stay unknown. These records do not claim to capture the original model context.

The file is owner-readable and owner-writable only. If a write fails, the app shows that history may be incomplete. Logging failure does not turn a successful move into an apparent failure.

JSONL files are ignored and rejected by the publication check even when force-staged. The only image exceptions are the reviewed screenshots and menu bar illustration, pinned by their exact file hashes.

## Everyday behavior

- First launch baselines existing Downloads files. It suggests newly arriving files, including downloads that arrive while the app is stopped after initial setup.
- Common partial-download extensions are ignored. A file must remain stable before review. The optional **Review older downloads** action imports the initial backlog after confirmation.
- A generic notification opens the queue. Choose Review downloads from the menu-bar icon to open it. Pause suggestions temporarily stops discovery.
- Suggestions use a filename, up to 2,400 characters of text, the first page of text-based PDFs, text from the first five PowerPoint slides, or Office title metadata. PowerPoint extraction handles `.pptx` files up to 20 MB with bounded XML reads. Legacy `.ppt`, images, and scanned PDFs use their filenames; there is no OCR.
- A warm local model suggests its best available folder, including when evidence is limited; review that choice before approving. It chooses among existing folder IDs and filename candidates, cannot execute commands or invent destination paths, and preserves the filename extension.
- Missing, busy, or slow models fall back to a quick editable suggestion. Leave keeps the original file in Downloads and dismisses that queue entry.
- Move and Undo refuse overwrites, changed files, and symlink destinations. Interrupted or ambiguous operations preserve recoverable files and show an error instead of deleting uncertain data. Hidden `.download-suggest-*` staging directories may remain after an interrupted move; inspect them before deleting them.

The app must be running to discover downloads. Add it in **System Settings → General → Login Items** if you want it to start at login. macOS may request access to Downloads/Documents. Quit from its menu before uninstalling. Remove the app, checkout, and private Application Support directory to remove the installation; this does not reverse completed moves.

## Speed and limits

The target is a suggestion within two seconds **after the download finishes**, with a warm model. Defaults are a 150 ms polling interval, 400 ms stability window, and an 850 ms inference timeout. Model loading happens in the background; it does not block the review queue. Long downloads, cold model loading, cloud-backed disk stalls, and notification delivery are outside that target. File stability is a completion heuristic, not a browser completion signal; Move rechecks that the file has not changed.

Measured on an Apple M3 Max with TinyJev-0.6B at 8-bit quantization. Four fictional text downloads reached the review queue in **604, 621, 616, and 610 ms**, with the default polling/stability settings and an 18-folder tree containing distracting keyword matches. All four chose the expected folder. This small smoke benchmark checks the integrated path, not general filing accuracy or a hard latency guarantee.

The folder map supports up to 200 destinations. The model always sees every broad category, plus eight more specific folders ranked by keywords. This avoids hiding the right category simply because a paper uses different words from its folder name. Deep, ambiguously named folders may still need manual selection; all configured folders remain selectable in review. Name suggestions are bounded alternatives from the original filename or document title, not open-ended text generation. There is no recursive Downloads cleanup or automatic deletion.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
./scripts/build_app.sh
DOWNLOAD_SUGGEST_TEST_APP=.build/Tuck.app/Contents/MacOS/DownloadSuggest \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_public.py
```

The small end-to-end suite starts real watcher processes and exercises real temporary files and authenticated HTTP requests. It covers discovery, latency, review, editing, leave, undo, collisions, changed files, authentication, restart persistence, private output checks, and native PDF extraction. No unit-test framework or model download is required. Without a built native helper, the PDF check is skipped. CI runs core checks on Linux and builds/tests the app on macOS. Optional live-model verification uses synthetic documents only.

There are 23 end-to-end tests, including PowerPoint slide extraction without title metadata. After fetching the weights, include the real-model scenario with `DOWNLOAD_SUGGEST_TEST_MODEL=1`; it is otherwise skipped. The native helper is supplied through `DOWNLOAD_SUGGEST_TEST_APP`. All 23 passed locally with both enabled. Cross-volume Move/Undo was also checked with a temporary HFS+ volume, including extended attributes and the quarantine marker. Deleted or changed pending files leave the review queue instead of remaining actionable.

The backend binds an ephemeral loopback port, requires a private bearer token on every endpoint, and rejects browser Origin headers. A second service cannot own the same state directory. Local users or processes with access to your account can still read its files; this is not a sandbox against other software running as you.

Source layout. `macos/` is the native UI and PDF helper; `src/download_suggest/` contains setup, classification, and the watcher/move journal. `tests/` contains the end-to-end checks. No background scheduler, browser extension, hosted backend, telemetry, or updater is included.

## Native UI and cross-volume checks

The macOS suite compiles the production UI types with a small AppKit interaction scenario. It checks nested folder selection, Back, Tab and Shift-Tab, Enter and Space, scrolling the focused row into view, destination width and the actual filed filename.

For the cross-volume check, set `DOWNLOAD_SUGGEST_TEST_DESTINATION_ROOT` to an empty folder on a separate disposable test volume. It copies a synthetic file, pauses the real copy process, checks that queue requests remain responsive, then resumes the copy and verifies Undo. Without a separate volume this scenario is skipped. State refreshes use a separate SQLite reader while file moves remain serialized.

For a manual UI pass, use a fictional download and a map with a parent folder, two nested levels and a branch containing 30 folders. Open the picker, navigate with mouse and keyboard, reach the final folder, choose it, reopen and cancel with Escape, then approve Move and Undo. Confirm the file returns to Downloads and no unexpected destination was used.
