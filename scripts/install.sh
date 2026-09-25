#!/bin/sh
# Installs locally. Does not enable login startup or move any downloads.
set -eu
cd "$(dirname "$0")/.."
PYTHON="${DOWNLOAD_SUGGEST_PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install -e '.[model]'
./scripts/build_app.sh
mkdir -p "$HOME/Applications"
if [ -e "$HOME/Applications/Tuck.app" ]; then
    printf 'An installed app already exists. Quit it and move it aside before reinstalling.\n' >&2
    exit 1
fi
ditto .build/Tuck.app "$HOME/Applications/Tuck.app"
.venv/bin/python -m download_suggest configure
.venv/bin/python -m download_suggest register-runtime --extractor "$HOME/Applications/Tuck.app/Contents/MacOS/DownloadSuggest"
printf 'Installed. Run .venv/bin/python -m download_suggest download-model once, then open the app in Applications.\n'
