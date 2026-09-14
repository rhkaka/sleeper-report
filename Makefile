# Run `make setup` once, then `make tuesday` every week after waivers process.
UV ?= uv
# Install the package by copying instead of an editable .pth link. Python 3.13+
# skips .pth files that macOS marks hidden, which happens inside iCloud-synced
# folders such as ~/Desktop; a copied install has no .pth and works everywhere.
export UV_NO_EDITABLE = 1

.PHONY: setup tuesday bundle refresh install ui ui-lan test

install:
	$(UV) sync

setup:
	$(UV) run sleeper-report setup

# Full report for the current week: bundle + Claude recommendations.
tuesday:
	$(UV) run sleeper-report report

# Bundle only (no Anthropic call) — paste it wherever you like.
bundle:
	$(UV) run sleeper-report report --bundle-only

# Force a fresh player database download, then run the full report.
refresh:
	$(UV) run sleeper-report report --refresh-players

# Local web UI (setup wizard, generate reports, read them). Opens a browser tab.
ui:
	$(UV) run sleeper-report serve

# Same, reachable from other devices on your Wi-Fi (phone, tablet).
ui-lan:
	$(UV) run sleeper-report serve --host 0.0.0.0 --no-browser

test:
	$(UV) run python tests/test_bundle.py > /dev/null
	$(UV) run python tests/test_web.py
	bash tests/test_pages_parity.sh

# Serve the GitHub Pages version locally.
pages:
	python3 -m http.server 8790 --directory docs
