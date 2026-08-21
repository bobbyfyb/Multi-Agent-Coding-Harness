#!/usr/bin/env bash
set -euo pipefail

CUDA_VISIBLE_DEVICES=1 uv run --project llm_server vllm serve \
  Qwen/Qwen3-Coder-30B-A3B-Instruct \
    --host 127.0.0.1 \
    --port 8000 \
    --dtype bfloat16 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.95 \
    --enable-auto-tool-choice \
    --tensor-parallel-size 1 \
    --tool-call-parser qwen3_coder