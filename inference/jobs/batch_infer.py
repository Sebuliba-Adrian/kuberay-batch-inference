"""
Ray Data batch inference entrypoint for a single batch.

This script is invoked by the FastAPI proxy via `ray job submit`, passing
the batch_id as a CLI argument. The flow:

  1. Read /data/batches/<batch_id>/input.jsonl  (written by the API)
  2. Run Ray Data `map_batches` with a class-based UDF that loads
     Qwen2.5-0.5B once per actor, then generates completions for each
     batch of prompts in parallel across the worker pool.
  3. Write /data/batches/<batch_id>/results.jsonl
  4. Write /data/batches/<batch_id>/_SUCCESS marker when fully done

Design notes
------------
- We use `map_batches` + Transformers, not `ray.data.llm` + vLLM, because
  vLLM's CPU backend is fragile and this repo targets CPU-only laptop
  grading. The vLLM path is the production upgrade; see docs/ARCHITECTURE
  §2.1 and §2.2 for the trade-off discussion.

- The UDF class owns the model. `__init__` runs ONCE per actor - this is
  where the ~5-10s model load cost is paid. `__call__` runs per batch and
  is where the actual inference happens.

- Ray Data captures row-level errors into the output dataset rather than
  crashing the whole pipeline. We translate those into per-prompt error
  records in the output JSONL.

- Input column contract: {"id": str, "prompt": str}
- Output column contract: {"id", "prompt", "response", "finish_reason",
                           "prompt_tokens", "completion_tokens", "error"}

- Concurrency = 2 matches the RayCluster's workerGroupSpecs.replicas,
  so each worker pod hosts exactly one actor. batch_size=8 is a
  conservative CPU-friendly default; tune up to 32 on GPU.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import ray

# ─── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("batch_infer")

# ─── Constants ──────────────────────────────────────────────────────
DEFAULT_RESULTS_ROOT = Path(os.environ.get("RESULTS_DIR", "/data/batches"))
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")


# ─── UDF: one Qwen actor per Ray worker ─────────────────────────────
class QwenPredictor:
    """
    Stateful Ray Data actor that loads Qwen once and serves generations.

    Kept dependency-light on purpose: only torch + transformers, no vLLM.
    Runs on CPU in BF16 by default; swap to ``dtype=torch.float16`` and
    ``device="cuda"`` for GPU.
    """

    def __init__(self, model_name: str, max_tokens: int) -> None:
        # 1. Heavy imports live INSIDE __init__ so Ray doesn't try to
        # serialize torch/transformers when it ships the UDF class to
        # the actor - that would fail because those libs are C-backed.
        import torch  # noqa: PLC0415
        from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

        t0 = time.monotonic()
        log.info("Loading model %s ...", model_name)

        self._torch = torch
        self._max_tokens = max_tokens
        self._model_name = model_name

        # 2. trust_remote_code is required for Qwen2.5's tokenizer
        # (custom chat template handler).
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            trust_remote_code=True,
        )
        # Qwen tokenizer has no pad token by default; use eos as pad.
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        # 3. BF16 weights ~ 1 GB on disk, usable on modern CPUs. If the
        # host lacks BF16 kernels, torch silently upcasts to FP32 which
        # works but doubles memory. 0.5 B params means this is tolerable.
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.bfloat16,
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        )
        self.model.eval()

        log.info(
            "Loaded model %s in %.1fs (dtype=%s, device=cpu)",
            model_name,
            time.monotonic() - t0,
            self.model.dtype,
        )

    def _format_prompt(self, user_prompt: str) -> str:
        """Wrap a raw prompt in Qwen2.5's chat template."""
        # 4. apply_chat_template returns a token-ready string when
        # tokenize=False, honoring the model's built-in role markers.
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": user_prompt},
        ]
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

    def __call__(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Run one batch of prompts through the model."""
        # 5. Ray Data hands us a dict of same-length numpy arrays, one per
        # column. Convert to Python lists for Transformers' tokenizer.
        ids = [str(v) for v in batch["id"]]
        prompts = [str(v) for v in batch["prompt"]]

        # Pre-allocate result columns so exceptions don't leave ragged rows.
        responses: list[str | None] = [None] * len(prompts)
        finish_reasons: list[str | None] = [None] * len(prompts)
        prompt_tokens_out: list[int] = [0] * len(prompts)
        completion_tokens_out: list[int] = [0] * len(prompts)
        errors: list[str | None] = [None] * len(prompts)

        torch = self._torch

        # 6. We process prompts one-by-one in a loop (not a padded batch)
        # because variable-length inputs create wasted KV cache work and
        # because error isolation is cleaner - a malformed prompt cannot
        # corrupt its neighbors. For throughput on GPU, switch to real
        # batched generation with left-padding.
        for idx, prompt in enumerate(prompts):
            try:
                text = self._format_prompt(prompt)
                inputs = self.tokenizer(
                    text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=2048,
                )
                # Keep the prompt-token count for downstream billing /
                # metrics. Subtract one if you consider BOS "not counted".
                prompt_tokens_out[idx] = int(inputs["input_ids"].shape[1])

                with torch.no_grad():
                    output = self.model.generate(
                        **inputs,
                        max_new_tokens=self._max_tokens,
                        do_sample=False,  # deterministic for reproducibility
                        temperature=1.0,
                        top_p=1.0,
                        pad_token_id=self.tokenizer.pad_token_id,
                        eos_token_id=self.tokenizer.eos_token_id,
                    )

                # 7. Strip the prompt tokens to get only the generation.
                generated_ids = output[0][inputs["input_ids"].shape[1]:]
                completion_tokens_out[idx] = int(generated_ids.shape[0])
                response_text = self.tokenizer.decode(
                    generated_ids,
                    skip_special_tokens=True,
                ).strip()

                responses[idx] = response_text
                # finish_reason heuristic: if last token is eos the model
                # stopped on its own; otherwise we hit max_new_tokens.
                last_token = int(generated_ids[-1]) if len(generated_ids) else -1
                finish_reasons[idx] = (
                    "stop" if last_token == self.tokenizer.eos_token_id else "length"
                )

            except Exception as exc:  # noqa: BLE001 - row-level isolation
                errors[idx] = f"{type(exc).__name__}: {exc}"
                log.warning("prompt id=%s failed: %s", ids[idx], exc)

        # 8. Return a dict of same-length arrays, matching Ray Data's
        # block format. Ray will materialize this into the output Dataset.
        return {
            "id": ids,
            "prompt": prompts,
            "response": responses,
            "finish_reason": finish_reasons,
            "prompt_tokens": prompt_tokens_out,
            "completion_tokens": completion_tokens_out,
            "error": errors,
        }


# ─── Pipeline ──────────────────────────────────────────────────────
def run_batch(batch_id: str, model: str, max_tokens: int) -> dict[str, int]:
    """
    Execute one batch end-to-end and return summary counts for the caller.

    This function is safe to re-run: it overwrites results.jsonl on
    every invocation. The _SUCCESS marker is written last, so readers
    can distinguish "in progress / crashed" from "clean output".
    """
    batch_dir = DEFAULT_RESULTS_ROOT / batch_id
    input_path = batch_dir / "input.jsonl"
    results_path = batch_dir / "results.jsonl"
    success_marker = batch_dir / "_SUCCESS"
    error_marker = batch_dir / "_FAILED"

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    log.info("Starting batch_id=%s model=%s", batch_id, model)
    log.info("Reading inputs from %s", input_path)

    # 1. Ray Data reads one block per source file by default. For large
    # inputs, repartition so we have more than #actors blocks to avoid
    # idle workers at the tail of the job.
    ds = ray.data.read_json(str(input_path))
    total = ds.count()
    log.info("Read %d prompts; repartitioning to %d blocks", total, max(2, total // 16 or 2))
    ds = ds.repartition(max(2, total // 16 or 2))

    # 2. Spin up the predictor actor pool with concurrency = worker count.
    # Each actor holds one model replica. batch_size tunes how many
    # prompts each __call__ invocation processes at once - small is safer
    # on CPU because OOM risk scales with batch size.
    log.info("Running map_batches with concurrency=2 batch_size=8")
    out_ds = ds.map_batches(
        QwenPredictor,
        fn_constructor_kwargs={"model_name": model, "max_tokens": max_tokens},
        concurrency=2,
        batch_size=8,
    )

    # 3. Write results as a single JSONL so the API can stream it back
    # without worrying about file ordering. repartition(1) forces one
    # output file. For multi-GB outputs you'd drop this and stream
    # multiple files instead.
    #
    # Ray's `write_json` writes one file per block into a target
    # directory. We point it at a staging dir, then rename the single
    # file into results.jsonl.
    staging_dir = batch_dir / "_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    log.info("Writing results to staging: %s", staging_dir)
    out_ds.repartition(1).write_json(str(staging_dir))

    # 4. Promote the single staged file to results.jsonl
    staged_files = sorted(staging_dir.glob("*.json"))
    if not staged_files:
        raise RuntimeError(f"Ray Data wrote no output files to {staging_dir}")
    staged_files[0].rename(results_path)

    # 5. Tally stats for the caller (the FastAPI status poller reads these
    # out of the job logs or re-counts them from the file).
    completed = 0
    failed = 0
    with results_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("error"):
                failed += 1
            else:
                completed += 1

    log.info(
        "Batch %s done: total=%d completed=%d failed=%d",
        batch_id, total, completed, failed,
    )

    # 6. Success marker last - its presence means "output is clean".
    if error_marker.exists():
        error_marker.unlink()
    success_marker.write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "total": total,
                "completed": completed,
                "failed": failed,
                "model": model,
                "finished_at": time.time(),
            }
        )
    )

    # Clean up staging dir
    for leftover in staging_dir.glob("*"):
        leftover.unlink()
    staging_dir.rmdir()

    return {"total": total, "completed": completed, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen2.5 batch inference on Ray Data")
    parser.add_argument("--batch-id", required=True, help="Batch identifier")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="HF repo id")
    parser.add_argument("--max-tokens", type=int, default=256)
    args = parser.parse_args()

    batch_dir = DEFAULT_RESULTS_ROOT / args.batch_id
    error_marker = batch_dir / "_FAILED"

    try:
        # Connect to the running Ray cluster. When submitted via the
        # Ray Jobs API, RAY_ADDRESS is set in the job environment so
        # ray.init() picks it up automatically.
        ray.init(address="auto", ignore_reinit_error=True)

        counts = run_batch(args.batch_id, args.model, args.max_tokens)
        log.info("Success: %s", counts)
        return 0

    except Exception as exc:  # noqa: BLE001 - top-level crash handler
        log.error("Batch %s FAILED: %s", args.batch_id, exc)
        log.error(traceback.format_exc())
        # Drop a failure marker so the API can promote the batch row
        # to status=failed even if Ray's own status isn't yet polled.
        batch_dir.mkdir(parents=True, exist_ok=True)
        error_marker.write_text(
            json.dumps(
                {
                    "batch_id": args.batch_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "failed_at": time.time(),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
