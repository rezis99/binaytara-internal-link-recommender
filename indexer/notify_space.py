"""Redeploy hook for the paid-host migration path only.

Streamlit Community Cloud redeploys automatically on push, so this module is a
no-op there. It exists so that a future maintainer moving to Hugging Face PRO
finds it rather than rediscovering the need for it.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    repo = os.environ.get("HF_SPACE_ID")
    if not token or not repo:
        # A missing optional secret must never fail an otherwise good index build.
        print("HF_TOKEN or HF_SPACE_ID not set; skipping redeploy (this is normal "
              "on Streamlit Community Cloud).")
        return 0
    try:
        from huggingface_hub import HfApi
        print(HfApi(token=token).restart_space(repo_id=repo))
        return 0
    except Exception as exc:                                   # noqa: BLE001
        print(f"Space restart failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
