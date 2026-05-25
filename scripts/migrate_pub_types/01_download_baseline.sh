#!/usr/bin/env bash
# 01_download_baseline.sh — Pull NLM PubMed baseline + update XML files via rsync.
#
# Total transfer ≈ 35-40 GB gzipped.  Time: ~1.5 hours over typical NLM throughput.
# rsync handles resume on interruption: re-running picks up only missing/changed files.
#
# Usage:
#   ./01_download_baseline.sh                    # default destination ./data/
#   ./01_download_baseline.sh /custom/data/dir   # custom destination

set -euo pipefail

DEST="${1:-./data}"
mkdir -p "${DEST}/baseline" "${DEST}/updatefiles"

echo "==> Downloading NLM PubMed baseline → ${DEST}/baseline/"
rsync -avh --partial --info=progress2 \
    rsync://ftp.ncbi.nlm.nih.gov/pubmed/baseline/ "${DEST}/baseline/"

echo "==> Downloading NLM PubMed updatefiles → ${DEST}/updatefiles/"
rsync -avh --partial --info=progress2 \
    rsync://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/ "${DEST}/updatefiles/"

echo "==> Download complete."
echo "    baseline:    $(ls "${DEST}/baseline/" | wc -l) files"
echo "    updatefiles: $(ls "${DEST}/updatefiles/" | wc -l) files"
echo "    total size:  $(du -sh "${DEST}" | cut -f1)"
