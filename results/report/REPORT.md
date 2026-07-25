# OneRec-8B-Pro Serving Benchmark Report

_Generated automatically from `/home/nvadmin/onerec-serving/results` -- do not hand-edit; re-run `bench/generate_report.py` after any new benchmark run._

## Runs included

| Run | Engine |
|---|---|
| `trtllm-tp1-bf16` | trtllm |
| `vllm-tp1-bf16` | vllm |
| `vllm-tp1-fp8` | vllm |

## Throughput / concurrency sweep

- **Highest peak throughput:** `vllm-tp1-bf16` (687 output tok/s at concurrency=256)

- **Best low-concurrency TTFT:** `vllm-tp1-bf16` (17 ms p50 at concurrency=1)


| run             | engine   |   concurrency |   output_tok_s |   output_tok_s_per_gpu |   req_s |   ttft_p50_ms |   e2e_p50_s |   avg_gpu_util_pct |   avg_power_w |   tok_s_per_watt |   n_failed |
|:----------------|:---------|--------------:|---------------:|-----------------------:|--------:|--------------:|------------:|-------------------:|--------------:|-----------------:|-----------:|
| trtllm-tp1-bf16 | trtllm   |             1 |         155.11 |                 155.11 |    0.61 |        121.84 |        1    |               6.97 |        435.86 |             0.36 |          0 |
| trtllm-tp1-bf16 | trtllm   |             2 |         305.44 |                 305.44 |    1.19 |        126.32 |        1.02 |              99.59 |        711.04 |             0.43 |          0 |
| trtllm-tp1-bf16 | trtllm   |             4 |         388.66 |                 388.66 |    1.52 |       1861.89 |        2    |              98.05 |        721.27 |             0.54 |          0 |
| trtllm-tp1-bf16 | trtllm   |             8 |         416.23 |                 416.23 |    1.63 |       4410.56 |        4.46 |              99.75 |        717.58 |             0.58 |          1 |
| trtllm-tp1-bf16 | trtllm   |            16 |         292.91 |                 292.91 |    1.14 |       4606.9  |        9.07 |              37.12 |        306.46 |             0.96 |         32 |
| trtllm-tp1-bf16 | trtllm   |            32 |         318.41 |                 318.41 |    1.24 |       5582.29 |       16.18 |              56.86 |        491.12 |             0.65 |         48 |
| trtllm-tp1-bf16 | trtllm   |            64 |         525.2  |                 525.2  |    2.05 |       2600.17 |       25.98 |              59.8  |        505.18 |             1.04 |         24 |
| trtllm-tp1-bf16 | trtllm   |           128 |         582.7  |                 582.7  |    2.28 |        167.91 |       47.96 |              38.5  |        484.15 |             1.2  |         46 |
| trtllm-tp1-bf16 | trtllm   |           256 |         657.63 |                 657.63 |    2.57 |        393.95 |       84.94 |              33.23 |        501.39 |             1.31 |         46 |
| vllm-tp1-bf16   | vllm     |             1 |         137.69 |                 137.69 |    0.54 |         16.62 |        1.17 |              60.55 |        479.58 |             0.29 |          0 |
| vllm-tp1-bf16   | vllm     |             2 |         269.63 |                 269.63 |    1.05 |        120.02 |        1.18 |              88.31 |        648.84 |             0.42 |          0 |
| vllm-tp1-bf16   | vllm     |             4 |         371.37 |                 371.37 |    1.45 |       1948.7  |        2.09 |              88.25 |        654.87 |             0.57 |          0 |
| vllm-tp1-bf16   | vllm     |             8 |         408.39 |                 408.39 |    1.6  |       4471.62 |        4.58 |              88    |        653.8  |             0.62 |          1 |
| vllm-tp1-bf16   | vllm     |            16 |         289.3  |                 289.3  |    1.13 |       4622.43 |        9.25 |              32.12 |        371.64 |             0.78 |         32 |
| vllm-tp1-bf16   | vllm     |            32 |         402.47 |                 402.47 |    1.57 |       2139.73 |       12.4  |              48.2  |        452.08 |             0.89 |         26 |
| vllm-tp1-bf16   | vllm     |            64 |         428.86 |                 428.86 |    1.68 |       2268.35 |       23    |              54.83 |        542.21 |             0.79 |         48 |
| vllm-tp1-bf16   | vllm     |           128 |         554.21 |                 554.21 |    2.16 |        346.41 |       45.53 |              56.14 |        626.02 |             0.89 |         47 |
| vllm-tp1-bf16   | vllm     |           256 |         687.35 |                 687.35 |    2.68 |        374.59 |       80.15 |              53.57 |        671.31 |             1.02 |         48 |
| vllm-tp1-fp8    | vllm     |             1 |         161.86 |                 161.86 |    0.63 |        121.05 |        0.92 |               4.83 |        356.83 |             0.45 |          0 |
| vllm-tp1-fp8    | vllm     |             2 |         316.96 |                 316.96 |    1.24 |        120.76 |        0.93 |              85.1  |        541.42 |             0.59 |          0 |
| vllm-tp1-fp8    | vllm     |             4 |         375.76 |                 375.76 |    1.47 |       1982.39 |        2.08 |              84.89 |        546.64 |             0.69 |          0 |
| vllm-tp1-fp8    | vllm     |             8 |         415.25 |                 415.25 |    1.62 |       4459.78 |        4.56 |              84.89 |        548.25 |             0.76 |          1 |
| vllm-tp1-fp8    | vllm     |            16 |         290.75 |                 290.75 |    1.14 |       4598.27 |        8.95 |              30.75 |        339.81 |             0.86 |         32 |
| vllm-tp1-fp8    | vllm     |            32 |         288.07 |                 288.07 |    1.13 |       4406.39 |       17.96 |              40.83 |        374.8  |             0.77 |         56 |
| vllm-tp1-fp8    | vllm     |            64 |         432.39 |                 432.39 |    1.69 |       2237.8  |       22.88 |              46.4  |        437.99 |             0.99 |         47 |
| vllm-tp1-fp8    | vllm     |           128 |         660.15 |                 660.15 |    2.58 |        179.97 |       41.96 |              50.33 |        515.08 |             1.28 |         27 |
| vllm-tp1-fp8    | vllm     |           256 |         650.76 |                 650.76 |    2.54 |        392.6  |       87.29 |              50.86 |        611.28 |             1.06 |         47 |

![latency_vs_throughput](latency_vs_throughput.png)

![throughput_scaling](throughput_scaling.png)

![ttft_vs_concurrency](ttft_vs_concurrency.png)

## Single-user latency sweep (concurrency = 1)

| run             | engine   |   input_len |   output_len |   ttft_p50_ms |   ttft_p99_ms |   e2e_p50_s |   e2e_p99_s |   mean_itl_ms |   n_failed |
|:----------------|:---------|------------:|-------------:|--------------:|--------------:|------------:|------------:|--------------:|-----------:|
| trtllm-tp1-bf16 | trtllm   |         128 |          128 |        23.499 |        28.763 |       0.509 |       0.527 |         3.83  |          0 |
| trtllm-tp1-bf16 | trtllm   |         128 |          256 |        22.912 |        27.821 |       0.999 |       1.005 |         3.828 |          0 |
| trtllm-tp1-bf16 | trtllm   |         512 |          128 |        23.345 |        29.286 |       0.511 |       0.516 |         3.836 |          0 |
| trtllm-tp1-bf16 | trtllm   |         512 |          256 |        23.494 |        25.608 |       1.003 |       1.008 |         3.841 |          0 |
| trtllm-tp1-bf16 | trtllm   |        2048 |          128 |        32.066 |        33.964 |       0.529 |       0.531 |         3.909 |          0 |
| trtllm-tp1-bf16 | trtllm   |        2048 |          256 |        31.911 |        32.533 |       1.03  |       1.034 |         3.914 |          0 |
| vllm-tp1-bf16   | vllm     |         128 |          128 |        12.165 |        13.287 |       0.585 |       0.597 |         4.515 |          0 |
| vllm-tp1-bf16   | vllm     |         128 |          256 |        12.021 |        13.18  |       1.162 |       1.164 |         4.511 |          0 |
| vllm-tp1-bf16   | vllm     |         512 |          128 |        14.839 |        15.584 |       0.592 |       0.6   |         4.547 |          0 |
| vllm-tp1-bf16   | vllm     |         512 |          256 |        14.751 |        16.08  |       1.179 |       1.19  |         4.569 |          0 |
| vllm-tp1-bf16   | vllm     |        2048 |          128 |        33.512 |        35.424 |       0.625 |       0.626 |         4.653 |          0 |
| vllm-tp1-bf16   | vllm     |        2048 |          256 |        33.716 |        37.64  |       1.208 |       1.222 |         4.621 |          0 |
| vllm-tp1-fp8    | vllm     |         128 |          128 |        11.001 |        11.555 |       0.46  |       0.469 |         3.553 |          0 |
| vllm-tp1-fp8    | vllm     |         128 |          256 |        10.995 |        11.869 |       0.914 |       0.917 |         3.543 |          0 |
| vllm-tp1-fp8    | vllm     |         512 |          128 |        13.841 |        14.346 |       0.473 |       0.474 |         3.613 |          0 |
| vllm-tp1-fp8    | vllm     |         512 |          256 |        13.852 |        14.621 |       0.937 |       0.938 |         3.619 |          0 |
| vllm-tp1-fp8    | vllm     |        2048 |          128 |        28.071 |        28.501 |       0.488 |       0.489 |         3.623 |          0 |
| vllm-tp1-fp8    | vllm     |        2048 |          256 |        28.158 |        30.033 |       0.952 |       0.954 |         3.623 |          0 |

## How to read this report

- **TTFT (time-to-first-token)** approximates perceived responsiveness for an interactive/streaming UI.
- **E2E latency** is full request completion time (prefill + decode of all output tokens).
- **Throughput (tokens/s)** is aggregate *output* token throughput across all concurrent in-flight requests at that load level -- this is what determines how many GPUs/replicas you need to serve a given QPS target.
- **tok/s/GPU** normalizes throughput by GPU count so tensor-parallel configs can be compared fairly against single-GPU replicas.
- See `docs/BENCHMARK_METHODOLOGY.md` for full methodology and `docs/SERVING_OPTIONS.md` for qualitative engine trade-offs.
