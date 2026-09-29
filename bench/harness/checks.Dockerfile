# Toggle checks image (ADR-0023). Build context: bench/harness.
#
# Playwright's Python image ships the browsers but not the playwright package,
# so this installs the version TECH_STACK.md pins from a hash-locked file. To
# regenerate the lock (from bench/harness):
#   uvx --from uv uv pip compile checks-requirements.in --python-version 3.12 \
#     --python-platform linux --generate-hashes --no-header -o checks-requirements.txt
#
# Run it as pwuser with --security-opt seccomp=chromium-seccomp.json, so Chromium's
# sandbox works (AGENTS.md §6); bench/harness/toggle.py does.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
COPY checks-requirements.txt /tmp/checks-requirements.txt
# The image's Python has pip but no ensurepip (so no venv). This image only runs
# the toggle checks, so installing into its system Python is fine.
RUN python3 -m pip install --no-cache-dir --require-hashes --break-system-packages \
    -r /tmp/checks-requirements.txt
USER pwuser
