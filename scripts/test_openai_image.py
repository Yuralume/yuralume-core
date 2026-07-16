"""Throwaway probe for OpenAI gpt-image-2.

Usage (PowerShell):
    $env:OPENAI_API_KEY="sk-..."
    .venv\Scripts\python.exe scripts\test_openai_image.py "your prompt here"

Optional flags:
    --size 1024x1024 | 1024x1536 | 1536x1024 | auto
    --n 1
    --quality low | medium | high | auto
    --model gpt-image-2
    --out tmp/openai_image

Saves PNG(s) to ``<out>_<i>.png`` next to repo root.
Prints latency + token usage so we can judge cost/perf vs ComfyUI.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

API_URL = "https://api.openai.com/v1/images/generations"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="Natural-language prompt")
    parser.add_argument("--size", default="1024x1536",
                        help="1024x1024 / 1024x1536 / 1536x1024 / auto")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality", default="medium",
                        choices=["low", "medium", "high", "auto"])
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--out", default="tmp/openai_image")
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    payload: dict = {
        "model": args.model,
        "prompt": args.prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    out_base = Path(args.out)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=300.0) as client:
            resp = client.post(API_URL, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        print(f"ERROR: request failed: {exc}", file=sys.stderr)
        return 3
    elapsed = time.monotonic() - t0

    if resp.status_code != 200:
        print(f"ERROR: HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        return 4

    body = resp.json()
    data = body.get("data") or []
    usage = body.get("usage") or {}

    saved: list[str] = []
    for i, item in enumerate(data):
        b64 = item.get("b64_json")
        if not b64:
            print(f"WARN: item {i} has no b64_json (keys={list(item.keys())})",
                  file=sys.stderr)
            continue
        path = out_base.with_name(f"{out_base.name}_{i}.png")
        path.write_bytes(base64.b64decode(b64))
        saved.append(str(path))

    print(json.dumps({
        "model": args.model,
        "size": args.size,
        "quality": args.quality,
        "n": args.n,
        "elapsed_sec": round(elapsed, 2),
        "usage": usage,
        "saved": saved,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
