# OpenReward SWE runtime

SWE 不再使用 `run_openreward_agentos_canary_runtime.py` 中的 `calc.py` 人工样例。
运行时按 task 的 `benchmark_family` 显式选择 provider：

- `swebench`：官方 SWE-bench verifier；
- `swegym`：固定 revision 的 SWE-Gym harness；
- `r2e`：R2E-Gym task-scoped 镜像，以及 manifest 中携带的 evaluator test files/
  expected statuses。

三者的镜像、checkout 和评分规则不同。它们共享 ORS 的 session/tool 协议，但不能互相
冒充 benchmark parity。

```text
Slime / policy client
  -> Research Ops ORS (`/openreward`)
  -> SweSandboxBackend (HTTP)
  -> trusted SWE runtime sidecar
       -> task-scoped policy sandbox (断网、限资源、长期 /testbed)
       -> git diff
       -> clean verifier sandbox (断网、限资源)
       -> provider-specific evaluator
  <- binary provider reward + compact verifier metrics
```

生产 runtime 由 Kubernetes sidecar 创建 policy/verifier Pod；policy 和 verifier 不会
拿到 ORS token 或 registry credential。每个 sandbox 均设置网络、CPU、内存、PID 和
capability 限制。提交后 policy sandbox 立即销毁，遗留 session 由 TTL 和进程退出清理。
本地 Docker engine 仅用于开发契约测试，不是 `cluster-d` 正式任务的依赖。

## 1. 生成 canary catalog

离线 Join 产物已包含 `repo/version/base_commit/test_patch/F2P/P2P`。以下命令按仓库
去重选择 8 条，结果固定写入 `config/openreward-swe-canary.json`：

```bash
uv run python scripts/build_swe_canary_catalog.py
```

### 导出 Slime selector 数据

SWE 的 repo、image、test patch 和 verifier 只保留在 ORS 服务端 catalog。Slime
训练数据只携带公开 query 与 `split/index`：

```bash
uv run python scripts/export_openreward_slime_dataset.py \
  --catalog config/openreward-swe-canary.json \
  --environment swe \
  --split train \
  --mode interactive \
  --output data/openreward_swe_canary_20260720/slime_selector.jsonl
```

Slime adapter 会把 selector 传给 `/create`，由 ORS 服务端恢复完整 task；不要把
catalog 或 `test_patch` 拷贝进训练 dataset。

在接真实 SGLang policy 前，可以用确定性的 `submit` action 验证 Slime adapter、
selector、真实 sidecar 和官方 verifier 的完整边界：

```bash
uv run --extra swe-runtime python scripts/run_slime_openreward_swe_integration.py \
  --slime-project-root /path/to/slime/slime \
  --index 0
```

这个 control 不代表模型能力：它提交空的 policy 增量，预期 reward 为 0。它只验证
token/logprob/loss-mask、ORS session/task ID 和 SWE terminal metadata 能否回到同一个
Slime `Sample`。

当前 8 条来自 Astropy、Django、Matplotlib、Seaborn、Flask、Requests、Xarray 和
Pylint。`test_patch` 只交给环境/verifier；policy prompt 只使用 `query`。

## 2. 本地启动 SWE sidecar

```bash
uv sync --extra dev --extra swe-runtime
export ORS_SWE_SANDBOX_TOKEN='<由运行环境注入>'
export ORS_SWE_REQUIRE_AUTH=true
uv run --extra swe-runtime python scripts/run_openreward_swe_runtime.py \
  --host 127.0.0.1 --port 9101
```

可调参数全部通过环境变量传入：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `ORS_SWE_CPUS` | `2` | 单容器 CPU 上限 |
| `ORS_SWE_MEMORY` | `8g` | 单容器内存上限 |
| `ORS_SWE_PIDS_LIMIT` | `512` | 单容器 PID 上限 |
| `ORS_SWE_COMMAND_SECONDS` | `120` | policy 单次 bash 上限 |
| `ORS_SWE_VERIFY_SECONDS` | `1800` | 官方 verifier 上限 |
| `ORS_SWE_SESSION_TTL_SECONDS` | `3600` | 遗留 session TTL |
| `ORS_SWE_PULL_MISSING_IMAGES` | `true` | 缺镜像时是否拉官方镜像 |
| `ORS_SWE_IMAGE_NAMESPACE` | `swebench` | 官方 image namespace |
| `ORS_SWE_IMAGE_TAG` | `latest` | 官方 instance image tag |
| `ORS_SWE_HARNESS_ROOT` | 空 | 可选的官方 SWE-bench 源码根目录 |
| `ORS_SWE_HARNESS_REVISION` | `4b677...` | 使用源码目录时必须匹配的 Git revision |
| `ORS_SWE_HARNESS_FAMILY` | `swebench` | `swebench`、`swegym` 或 `r2e` |

在 `cluster-d` 使用 Kubernetes runtime；以下命令不会读取或打印 token：

```bash
python scripts/deploy_openreward_swe_k8s.py \
  --namespace rl-training \
  --host-root "$PWD" \
  --work-root "$PWD/data/openreward_swe_k8s/work" \
  --harness-root "$PWD/data/swe_gym_harness" \
  --harness-family r2e \
  --harness-revision r2e-gym.manifest.v1
```

## 3. 接入统一 ORS

sidecar 和统一 ORS 使用相同 token 引用：

```bash
export ORS_SWE_SANDBOX_URL=http://127.0.0.1:9101
export ORS_SWE_SANDBOX_TOKEN='<由运行环境注入>'

uv run python scripts/run_openreward_server.py \
  --mode contract \
  --real-domain swe \
  --backend-config config/openreward-backends.example.yaml \
  --task-catalog config/openreward-swe-canary.json \
  --host 127.0.0.1 --port 8020
```

ORS 的 `bash/read_file/write_file/submit` 四个动作保持不变。`submit` 同步返回当前
provider 的二值 reward；metadata 只保留 patch 哈希、测试计数、超时和 harness 版本，
不返回 hidden test patch 或完整测试日志。

sidecar 只加载官方包的 `test_spec`、`grading` 和 `log_parser`。SWE-bench 4.0.4
把数据集下载、Modal 等 CLI 依赖声明成硬依赖，但在线 verifier 不使用它们；镜像构建
因此以 `--no-deps` 安装固定版本，再由锁文件安装 Docker、requests、unidiff 三个实际
运行依赖。使用本地源码时还会校验 Git revision，避免静默换 harness。

## Provider 边界与当前证据

`swebench` 仍使用官方 SWE-bench 4.0.4 的 `test_spec`、`grading` 和 `log_parser`。
`swegym` 使用固定的 SWE-Gym harness revision。`r2e` 不验证 `/testbed` Git HEAD 与
manifest `base_commit` 相等，因为 R2E 的 task image tag 才是可执行 snapshot 身份；它会
记录声明的 commit、image ref 和 provider version，并在独立 evaluator sandbox 写入
manifest test files 后执行 `/testbed/run_tests.sh`。

2026-08-02 的 R2E 空 patch probe 已通过真实 ORS -> Kubernetes sandbox -> evaluator
回路，证据在 `data/openreward_r2e_swe_probe_20260802.json`。该 control 的 reward 为
`0.0`，只证明环境可执行，不代表模型能力或 SWE-bench 成绩。
