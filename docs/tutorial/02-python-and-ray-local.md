# 02 - Python and Ray (Local)

We will build the compute core first. By the end of this section,
you will have:

- A Python script that loads Qwen2.5-0.5B and generates text.
- The same script processing a batch of prompts from a JSONL file.
- The same script using Ray Data to parallelize the batch across
  multiple processes.

No HTTP yet. No Kubernetes yet. Just Python and Ray.

## Evolution in one picture

```
Layer 1                    Layer 2                    Layer 3
one prompt, one process    many prompts, one process  many prompts, Ray actors

+-----------+              inputs.jsonl               inputs.jsonl
|  prompt   |                   |                          |
+-----+-----+                   v                          v
      |                  +-------------+           +----------------+
      v                  | for line in |           |  ray.data      |
+-----------+            |   jsonl:    |           |  .read_json    |
|  model.   |            |   generate  |           |  .map_batches  |
|  generate |            +------+------+           |  (actor pool)  |
+-----+-----+                   v                  +--------+-------+
      |                  results.jsonl                      |
      v                                                     v
    print                                             results.jsonl
```

Same generation logic throughout. Only the surrounding machinery
changes.

## Layer 1: the dumbest possible script

Inside `~/batch-tutorial/`, with your virtualenv active:

```bash
pip install "torch>=2.3,<3" "transformers>=4.45,<5" "accelerate>=0.34,<1"
```

That installs the CPU wheels of PyTorch plus Hugging Face
Transformers, which is the library that understands how to load
Qwen and run `generate()`.

Create `hello_qwen.py`:

```python
# ~/batch-tutorial/hello_qwen.py
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL,
    torch_dtype=torch.bfloat16,
    trust_remote_code=True,
    low_cpu_mem_usage=True,
)
model.eval()

messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is 2 + 2? Answer in one short sentence."},
]

# Qwen's tokenizer ships its own chat template. This turns the
# messages into the exact string Qwen was trained on.
text = tokenizer.apply_chat_template(
    messages, tokenize=False, add_generation_prompt=True
)

inputs = tokenizer(text, return_tensors="pt")
output = model.generate(**inputs, max_new_tokens=32, do_sample=False)

# Strip the prompt tokens so we only decode the generation.
generated = output[0][inputs["input_ids"].shape[1]:]
print(tokenizer.decode(generated, skip_special_tokens=True).strip())
```

Run it:

```bash
python hello_qwen.py
```

First run downloads ~1 GB of model weights into
`~/.cache/huggingface/`. Subsequent runs skip that. After ~10
seconds of CPU generation you get something like:

```
2 + 2 equals 4.
```

### Why each decision

- **bfloat16**: the weights on disk are 16-bit floats. bf16 runs
  on any modern CPU and uses half the memory of fp32.
- **`trust_remote_code=True`**: Qwen ships a custom tokenizer class.
  Transformers runs custom code only when you opt in.
- **`apply_chat_template`**: every chat model has a different
  prompt format. This uses Qwen's own template so we do not have
  to remember it.
- **`do_sample=False`**: deterministic output. For a batch API we
  want reproducibility more than creativity.
- **slicing `output[0][input_length:]`**: `model.generate()`
  returns the full sequence (prompt + generation). We only want
  the generation.

### The finished file in the repo

The production equivalent lives at
`inference/jobs/batch_infer.py:65` in the
`QwenPredictor.__init__` method. Open it and compare.

## Layer 2: batch from a file

One prompt at a time is boring. Let's read a JSONL file, process
each prompt, and write results to another JSONL file. This is the
contract our final service will expose: **input.jsonl in,
results.jsonl out.**

Create `inputs.jsonl`:

```jsonl
{"id":"0","prompt":"What is 2+2?"}
{"id":"1","prompt":"Name three planets."}
{"id":"2","prompt":"What is Kubernetes in one sentence?"}
```

Then create `batch_local.py`:

```python
# ~/batch-tutorial/batch_local.py
import json
import torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
INPUT_PATH = Path("inputs.jsonl")
OUTPUT_PATH = Path("results.jsonl")

tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
if tokenizer.pad_token_id is None:
    tokenizer.pad_token_id = tokenizer.eos_token_id
model = AutoModelForCausalLM.from_pretrained(
    MODEL, torch_dtype=torch.bfloat16, trust_remote_code=True, low_cpu_mem_usage=True
)
model.eval()

def generate(prompt: str, max_tokens: int = 64) -> str:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt},
    ]
    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=2048)
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    generated = output[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(generated, skip_special_tokens=True).strip()

with INPUT_PATH.open() as f_in, OUTPUT_PATH.open("w") as f_out:
    for line in f_in:
        row = json.loads(line)
        response = generate(row["prompt"])
        out = {"id": row["id"], "prompt": row["prompt"], "response": response}
        f_out.write(json.dumps(out) + "\n")

print(f"Wrote {OUTPUT_PATH}")
```

Run it:

```bash
python batch_local.py
cat results.jsonl
```

You now have a batch processor. It is sequential (one prompt at a
time), single-process, and bounded by how fast one CPU core can
generate. That is fine for 3 prompts. It is not fine for 3000.

### Why JSONL and not JSON

One JSON object per line means:

- We can stream output without loading everything in memory.
- We can process rows as they arrive.
- A malformed row does not corrupt the whole file.
- Every batch job system you will use (Spark, Ray Data, BigQuery,
  even `jq`) understands JSONL natively.

The finished repo uses the same format. Open
`inference/jobs/batch_infer.py` and look at the input contract
comment near line 29.

## Layer 3: parallelize with Ray

Ray takes ordinary Python and makes it distributable. Instead of
a for-loop, we give Ray a dataset and a function, and Ray decides
which worker runs which rows.

Install Ray:

```bash
pip install "ray[data,default]==2.54.1"
```

Create `batch_ray.py`:

```python
# ~/batch-tutorial/batch_ray.py
import json
from pathlib import Path
import ray

INPUT_PATH = Path("inputs.jsonl")
OUTPUT_PATH = Path("results.jsonl")


class QwenPredictor:
    """Stateful Ray Data actor. Loads the model once; handles many batches."""

    def __init__(self, model_name: str = "Qwen/Qwen2.5-0.5B-Instruct",
                 max_tokens: int = 64) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self._max_tokens = max_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.bfloat16,
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        )
        self.model.eval()

    def __call__(self, batch):
        torch = self._torch
        ids = [str(v) for v in batch["id"]]
        prompts = [str(v) for v in batch["prompt"]]
        responses = []
        for prompt in prompts:
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ]
            text = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = self.tokenizer(text, return_tensors="pt",
                                    truncation=True, max_length=2048)
            with torch.no_grad():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=self._max_tokens,
                    do_sample=False,
                    pad_token_id=self.tokenizer.pad_token_id,
                    eos_token_id=self.tokenizer.eos_token_id,
                )
            gen = output[0][inputs["input_ids"].shape[1]:]
            responses.append(self.tokenizer.decode(gen, skip_special_tokens=True).strip())
        return {"id": ids, "prompt": prompts, "response": responses}


ray.init()

ds = ray.data.read_json(str(INPUT_PATH))
print(f"Input dataset: {ds.count()} rows")

out_ds = ds.map_batches(
    QwenPredictor,
    fn_constructor_kwargs={"max_tokens": 64},
    concurrency=1,
    batch_size=4,
)

# Collect and write.
with OUTPUT_PATH.open("w") as f:
    for row in out_ds.iter_rows():
        f.write(json.dumps(row) + "\n")

ray.shutdown()
print(f"Wrote {OUTPUT_PATH}")
```

Run it:

```bash
python batch_ray.py
```

You will see Ray's startup banner, then the same results.jsonl as
before. On one laptop with `concurrency=1` it is not faster -
but the code now scales unchanged to many workers across many
machines.

### Why each Ray decision

- **`ray.init()`** with no args starts a local Ray runtime - a
  head process plus worker processes on the same machine. Later
  we point this at a real cluster.
- **`ray.data.read_json`** turns a JSONL file into a distributed
  dataset. Rows become blocks; blocks can be scheduled
  independently.
- **Class-based UDF**: Ray Data supports two UDF shapes: plain
  functions (stateless, model reloaded every call - terrible)
  and classes (stateful, model loaded once in `__init__` - what
  we want).
- **`concurrency=1`**: one actor. On a 2-worker Ray cluster this
  would be `concurrency=2` so each worker pod gets one actor
  holding one copy of the model.
- **`batch_size=4`**: each actor's `__call__` is invoked with 4
  rows at a time. Tune this based on memory. On GPU you raise
  it to 32+.
- **Heavy imports inside `__init__`**: Ray ships the class to
  each actor via pickle. Pickling `torch` or `transformers` at
  module import would fail because they are C-backed. Importing
  inside `__init__` defers that until after the class is on the
  actor.

### Matches the production code

Open `inference/jobs/batch_infer.py:65`. You will see
`QwenPredictor` with the same shape, just with added error
handling (per-row `try/except`, token counts, finish-reason
tracking) and more comments. The `run_batch()` function around
line 239 does the same `read_json` -> `repartition` ->
`map_batches` -> `write_json` pipeline.

That pipeline is the entire compute plane. Everything we add
from here on is HTTP, persistence, packaging, and orchestration
around it.

## Verify

At the end of this section you should have:

- `~/batch-tutorial/hello_qwen.py`
- `~/batch-tutorial/batch_local.py`
- `~/batch-tutorial/batch_ray.py`
- `~/batch-tutorial/inputs.jsonl`
- `~/batch-tutorial/results.jsonl` (produced by either script)
- `~/.cache/huggingface/` with Qwen weights cached

Continue to
[03-fastapi-postgres-poller.md](03-fastapi-postgres-poller.md).
