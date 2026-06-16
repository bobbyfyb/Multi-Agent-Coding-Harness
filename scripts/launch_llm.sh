#!/usr/bin/env bash
set -euo pipefail

CUDA_VISIBLE_DEVICES=1 uv run --project llm_server vllm serve Qwen/Qwen2.5-Coder-32B-Instruct \
  --host 127.0.0.1 \
  --port 8000 \
  --served-model-name qwen-coder \
  --tensor-parallel-size 1 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
