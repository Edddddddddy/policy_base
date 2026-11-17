# IM 集成（git 源 + DVC）

> 适用：SO-101 平台 P3（2026-11-17）｜ 关联：[inference_manager docs/local-mode.md](../../inference_manager/docs/local-mode.md)、
> [dvc-distribution.md](dvc-distribution.md)、[inference_manager AGENTS.md](../../inference_manager/AGENTS.md)
>
> **结论：选中路线 A。** IM 以 `runtime.repo_source: git` 按固定 commit 拉取本仓库（repo_url 可为
> `file:///home/ubuntu/yayyrobotics/policy_base` 或本地绝对路径），预检稀疏展开 `submission.yaml`，
> 完整 prepare 保留 `.git` 后在仓库根执行既有 `dvc pull`，`provenance.weights_source=manager_dvc`。
> **IM 生产代码无需改动**；本轮只新增 IM 示例配置、离线集成测试与文档（见 §5）。
> 路线 B（IM `repo_source: local` 只读复制后 dvc pull）实测被否决：副本不含 `.git`，DVC 直接拒绝。
>
> **官方固定 commit `8d5186fe3594705f23746a3db07226900f00a3af` 已全链路复核通过**（2026-11-17，
> `file:///home/ubuntu/yayyrobotics/policy_base` + 真实 MinIO `dvc pull`，见 §2.6/§4.3）；
> 该 hash 之后**只允许文档类提交**（代码/配置/权重指针变更须重新复核）。

`submission.yaml` 声明 `policy.model: policy-based` + `runtime.kind: uv`，因此 IM 必须拉取 DVC 权重；
IM 既有生产路径本就支持该组合（`artifacts/preparer.py` → `artifacts/git.py` + `artifacts/dvc.py`），
缺的只是「本地 file:// 仓库 + policy-based」这一组合的正式证据与配置样例。

## 1. 路线对比

| 维度 | 路线 A：IM git 源（选中） | 路线 B：IM local 模式（否决） |
|---|---|---|
| IM 配置 | `runtime.repo_source: git`（默认） | `runtime.repo_source: local` + `runtime.local_repo_root` |
| launch.repo_url | `file:///home/ubuntu/yayyrobotics/policy_base` 或绝对路径 | 本地绝对目录或 `file://` |
| 提交证明 | `git fetch --depth=1 --filter=blob:none origin <commit>`，`FETCH_HEAD` 必须等于请求的完整 40/64 位 hash | 读取源目录 `.git` HEAD 比对；无 `.git` 时 WARN、`head_verified=false` |
| 预检形态 | `sparse-checkout set --no-cone -- /submission.yaml` 只展开声明文件 | 只读复制整棵源码树（排除 `.git`、`.venv*`、缓存等） |
| prepare 形态 | `sparse-checkout disable` 展开完整源码树，**保留 `.git`** | 复用预检副本，副本**不含 `.git`** |
| `dvc pull`（policy-based） | ✅ 成功（workspace 是 git 仓库，可解析 `.dvc` 指针） | ❌ `ERROR: <workspace> is not a git repository` |
| IM 对上行为 | 成功，`weights_source=manager_dvc` | prepare 明确报 `本地仓库模式不支持 policy-based 权重拉取；请使用 rule-based 或预置权重`（`LOCAL_POLICY_DVC_ERROR`，不静默降级） |
| rule_base 兼容 | ✅（rule-based 跳过 DVC，但仍要求固定 commit + git 仓库） | ✅（当前默认本地模式） |
| 代价/风险 | 每次准备需 fetch（本地 file:// 毫秒级）；服务用户需对源仓库只读；commit 必须固化 | 无法支持 policy-based；无 `.git` 时提交不可证明 |

**为什么不能直接把 local 模式加上 dvc pull**：local 模式的所有者层设计（`artifacts/local.py`）明确把
`.git` 排除在复制之外（源仓库只读、副本不是 git 仓库）；而 DVC 3.63 的 `pull` 要求工作树是 git 仓库
（`dvc init --no-scm` 的裸 DVC 仓库除外，本仓库不是）。要让 local 模式支持 DVC 需要改生产代码
（保留 `.git`/改复制排除策略，或改用全局 DVC cache + `--no-scm`），超出本轮「不加生产代码改动」的边界，
且会与 local 模式「只读复制、不执行 Git」的既有语义冲突。路线 A 直接复用 IM 既有生产路径，风险最小。

## 2. 复现证据（2026-11-17，全部离线可重复）

所有证据位于 `/tmp/p3_im_spike/evidence/`（临时目录，不随仓库提交）；复现脚本
`/tmp/p3_im_spike/run_spike.py`、`/tmp/p3_im_spike/real_readiness.py`。
测试机：Python 3.12.3、git 2.43.0、dvc 3.63.0（dvc-s3 已装）、IM `.venv/bin/dvc`。

### 2.1 合成 git+DVC 仓库（离线，不依赖 MinIO）

`evidence/01_repo_build.log`：临时 git 仓库（commit `7863ca4b1195d11192e0dba5ebf5d1ce0ccdda15`）含
policy-based `submission.yaml`（`targets: [pick_place]`、`resources: 1 GPU / 8192 MiB`）、`pyproject.toml`、
`uv.lock` 占位与 DVC 管理的权重目录 `models/act-spike/pretrained_model`（16 KiB 合成权重，本地目录 remote，
`git ls-files` 不含权重本体、只含 `.dvc` 指针）。

原始 Git 序列（与 IM `SourceRepository.checkout/materialize` 完全一致，`evidence/02_route_a_raw.log`）：

```text
git init --quiet --object-format=sha1
git remote add origin file:///tmp/p3_im_spike/repo
git config remote.origin.promisor true / partialclonefilter blob:none
git fetch --quiet --no-tags --depth=1 --filter=blob:none origin 7863ca4b...
  → stderr: warning: filtering not recognized by server, ignoring   （退出码 0）
git rev-parse FETCH_HEAD^{commit}   → == 请求 commit
git ls-tree <commit> -- submission.yaml → 100644 blob …（普通文件校验通过）
git sparse-checkout set --no-cone -- /submission.yaml
git checkout --quiet --detach <commit> → HEAD == 请求 commit
git sparse-checkout disable            → HEAD 不变，完整源码树展开
dvc pull → A models/act-spike/pretrained_model/ / 3 files fetched and 2 files added（退出码 0）
```

要点：本地 `file://` 服务端（upload-pack）默认不支持 partial clone filter，git 仅打印
`warning: filtering not recognized by server, ignoring` 并**自动降级为全量 fetch**（退出码 0），流程不受影响；
`sparse-checkout disable` 后工作树仍保留 `.git`，这是 DVC pull 的前提。

IM `ArtifactPreparer` 端到端（`repo_source=git`、`enabled_kinds=[uv]`、
`execution.dvc_executable=.venv/bin/dvc`，`evidence/03_route_a_im.log`）：

```text
preflight commit=7863ca4b1195d11192e0dba5ebf5d1ce0ccdda15 model=policy-based kind=uv requires_dvc=True
preflight elapsed=0.317s
prepare  elapsed=0.828s
provenance: {"commit": "7863ca4b…", "repo_url": "file:///tmp/p3_im_spike/repo", "source_path": "<ws>/source",
             "weights": {"declarations": {"models/act-spike/pretrained_model.dvc": "ceb60936…"},
                         "dvc_invoked": true, "weights_source": "manager_dvc"}}
dvc 子进程调用（唯一一次）: [dvc, pull]，cwd=<ws>/source
权重落盘: size=16384 sha256=a1f259d4… == fixture sha256；workspace .git 存在；HEAD == 固定 commit
```

错误路径（同日志）：不存在的 40 位 commit → `RuntimeOperationError: git 子命令失败，退出码 128：…
fatal: git upload-pack: not our ref …`；非完整 hash（`deadbeef`）→
`Git checkout 只接受完整提交哈希`。两者都在拉权重前失败。

### 2.2 路线 B 否决证据

原始命令（`evidence/04_route_b_raw.log`）：复制工作树但排除 `.git`（模拟 local 模式 `materialize`）后：

```text
$ dvc pull
ERROR: /tmp/p3_im_spike/workspace-raw-b is not a git repository   （退出码 255，权重未落盘）
$ git init -q -b main && dvc pull
A models/act-spike/pretrained_model/ / 3 files fetched and 2 files added（退出码 0）
```

说明：路线 B 的唯一差异就是 `.git` 是否存在；补上 `.git` 后同一份文件即可 pull，证实 IM local 模式的
复制排除策略与 DVC 前置条件直接冲突。

IM local 模式实测（`evidence/05_route_b_im.log`）：`repo_source=local` 预检成功并识别
`requires_dvc=True`，prepare 被明确拒绝：

```text
RuntimeOperationError: 本地仓库模式不支持 policy-based 权重拉取；请使用 rule-based 或预置权重
```

即 IM 不会静默跳过权重，也不会错误降级为 rule-based（符合 IM 既有不变量）。

### 2.3 真实 policy_base 就绪性预检（未提交工作区，只读）

`evidence/06_real_policy_base_readiness.log`：把本仓库工作区中「固定提交会出现」的 DVC 元数据
（`.dvc/config`、`.dvcignore`、`models/*.dvc`、`models/.gitignore`、`submission.yaml`、`pyproject.toml`、
`uv.lock`）只读复制到 `/tmp` 新 workspace（`git init`、不带 `.dvc/cache` 与权重工作树），
用 IM `CheckpointResolver` + 真实 `/home/ubuntu/.local/bin/dvc`（dvc-s3）拉 MinIO 权重：

```text
dvc pull elapsed=3.536s
weights_source=manager_dvc, dvc_invoked=true
pulled weights dir: models/act-20261117-act-wrist-smoke
  7 files / 206,722,219 B（config.json、model.safetensors、pre/post processor、train_config.json）
  model.safetensors sha256=e145b7a3b9ca396e93c724a265d8d189e490b7e050405feecef1d8981f4dc844
pulled == policy_base 工作区源文件: True
```

凭据只走本机 `~/.aws/credentials`（0600）或进程环境变量 `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`，
未写入任何仓库/文档/日志；MinIO endpoint 与 remote 见 [dvc-distribution.md](dvc-distribution.md)。
这证明「IM DVC 适配器 + 本仓 `.dvc` 配置 + 真实 MinIO 凭据」链路可用；git 源模式还差固定 commit。

### 2.4 真实仓库 file:// 源预检（当前 HEAD，预期拒绝）

`evidence/07_real_policy_base_preflight_old_commit.log`：对
`file:///home/ubuntu/yayyrobotics/policy_base` + 当前 HEAD `8066373365c1ea9f51aa3f6af183f11f3e04d8d4`
（P3 前的初始化提交，尚不含 `submission.yaml`）执行 IM git 源预检：

```text
preflight elapsed=0.244s
RuntimeOperationError: 固定提交的根目录必须包含普通 submission.yaml 文件
```

即 file:// 源与固定 commit fetch 在真实仓库上可用，旧 commit 因缺少交付声明被明确拒绝（预期行为，
不是产品缺陷）。**官方固定 commit 的成功链路需等 Lead 提交后补录**（见 §4.3）；在此之前用 §2.5 的
「真实内容 + 合成 commit」全链路先行降低风险。

### 2.5 真实内容全链路（临时克隆 + 合成固定 commit）

`evidence/08_real_content_clone_chain.log`：为在官方 commit 落定前验证完整链路，把本仓库当前工作区只读
复制到 `/tmp`（排除 `.git`/`.venv`/`.dvc/cache`/权重本体），在副本内 `git init` + commit 得到**临时合成
commit** `f0bb0d89264014669f455919fd82633b61b53ee0`（仅存在于 /tmp，验证后删除；不修改、不提交源仓库），
再用 IM `ArtifactPreparer`（`repo_source=git`、`dvc_executable=/home/ubuntu/.local/bin/dvc`、
`command_timeout_s=900`）对 `file:///tmp/.../policy_base-clone` 跑真实 preflight+prepare（真实 MinIO `dvc pull`）：

```text
preflight commit=f0bb0d89… model=policy-based kind=uv requires_dvc=True, elapsed=0.327s
  预检工作树只有 [.git, submission.yaml]
prepare elapsed=3.100s
provenance weights_source=manager_dvc dvc_invoked=true
  declarations: {models/act-20261117-act-wrist-smoke.dvc: f4e7f2fd…}
workspace .git 存在；git HEAD == 合成固定 commit
权重 7 文件 / 206,722,219 B；model.safetensors sha256=e145b7a3…，与 policy_base 源文件一致
```

边界：repo_url 是 /tmp 临时克隆而非 `file:///home/ubuntu/yayyrobotics/policy_base`，commit 也不是官方 hash；
因此不能替代官方复核。官方复核随后已完成（§2.6），本条保留为合成 commit 的可复现对照证据。

### 2.6 官方固定 commit 全链路复核（commit 8d5186f，已完成）

`evidence/09_official_chain.log`（复核时源仓库 `git status` 为 clean，只读访问）：

```bash
# 源仓库 HEAD / commit 核对
git -C /home/ubuntu/yayyrobotics/policy_base rev-parse HEAD
# 8d5186fe3594705f23746a3db07226900f00a3af
git -C /home/ubuntu/yayyrobotics/policy_base ls-tree -r --name-only HEAD | grep -E '^(submission.yaml|uv.lock|.dvc/config|models/.*\.dvc)$'
# .dvc/config, models/act-20261117-act-wrist-smoke.dvc, submission.yaml, uv.lock

# IM 侧真实 preflight + prepare（ArtifactPreparer, repo_source=git, dvc_executable=/home/ubuntu/.local/bin/dvc,
# command_timeout_s=900；workspace 在 /tmp，真实 MinIO dvc pull；脚本 /tmp/p3_im_spike/official_chain.py）
env -u PYTHONPATH /home/ubuntu/yayyrobotics/inference_manager/.venv/bin/python \
    /tmp/p3_im_spike/official_chain.py 8d5186fe3594705f23746a3db07226900f00a3af
```

```text
repo_url: file:///home/ubuntu/yayyrobotics/policy_base
commit:   8d5186fe3594705f23746a3db07226900f00a3af   （policy_base HEAD 一致，git status clean）
preflight commit=8d5186fe… model=policy-based kind=uv requires_dvc=True elapsed=0.357s
  预检工作树只有 [.git, submission.yaml]
prepare elapsed=2.480s
provenance weights_source=manager_dvc dvc_invoked=true
  declarations: {models/act-20261117-act-wrist-smoke.dvc: f4e7f2fd69fad6…}
workspace .git 存在；git HEAD == 8d5186fe…
权重 7 文件 / 206,722,219 B（与 .dvc 指针 size/nfiles 一致）
model.safetensors size=206,699,736 sha256=e145b7a3b9ca396e93c724a265d8d189e490b7e050405feecef1d8981f4dc844
model 内容 == policy_base 源文件；manifest 从 materialized worktree 重新解析一致
PASS；workspace 已清理，policy_base 源仓库未被写入（复核前后 git status 均为 clean）
```

结论：**路线 A 在官方 commit 上端到端成立**——IM 按固定 commit 拉取、保留 `.git`、真实 MinIO `dvc pull`
还原 198 MB 权重，`weights_source=manager_dvc`。复核对象锁定为 commit `8d5186f`；该 hash 之后只允许
文档类提交，任何代码/配置/`.dvc` 指针变更都必须重新跑本节复核并更新记录。

## 3. IM 配置样例

新增 `inference_manager/configs/manager.local.git.example.yaml`（基于
`configs/manager.local.example.yaml` 调整；schema/parser 校验与租约测试见 §4.1）。关键差异：

```yaml
git:
  repositories: []          # 可选 DVC remote 覆盖；默认用仓库自身 .dvc/config（policy_base=MinIO s3）

resources:
  nodes:
    - name: local
      max_instances: 4
      gpus:
        - {id: "0", memory_mib: 8192}  # 声明式配额；须 >= submission.resources.gpu_memory_mib_per_gpu

runtime:
  enabled_kinds: [uv]
  repo_source: git          # 关键：git=固定 commit 拉取 + dvc pull；local 只读复制（见 leader 文档）
  execution:
    system_python_fallback: true
    preserve_pythonpath: true          # 保留已 source 的 ROS 2 覆盖层（实例 import rclpy）
    uv_executable: uv
    git_executable: git
    dvc_executable: /home/ubuntu/.local/bin/dvc  # 绝对路径：env.sh 会摘掉 ~/.local/bin
    command_timeout_s: 900.0           # 覆盖 198 MB 级 dvc pull + uv sync
```

launch 请求（IM 受理前会做 Git/submission/station_type 预检）：

```text
repo_url   = file:///home/ubuntu/yayyrobotics/policy_base   （或 /home/ubuntu/yayyrobotics/policy_base）
commit_hash= <40 位完整 hash，必须包含 submission.yaml/.dvc/models/*.dvc>
station_type = pick_place
```

要求与注意：

- **DVC 工具**：`dvc` 3.63.0 且带 `dvc-s3`（`dvc doctor` 的 Supports 含 s3），`dvc_executable` 写绝对路径；
  安装/凭据见 [dvc-distribution.md](dvc-distribution.md) §2–§3。
- **凭据**：Git 地址若非本机 file://，沿用 IM 既有 Token/SSH 用户配置；MinIO/S3 凭据走服务用户
  `~/.aws/credentials` 或进程环境变量，**禁止入库/写进配置**。
- **GPU 租约**：`submission.yaml` 声明 1 GPU / 8192 MiB，IM 配置节点必须声明不小于该值的显存配额，
  否则 launch 在受理阶段就因「节点实例配额或 GPU 显存不足」失败（示例为声明式配额、不探测硬件）。
- **超时**：`monitor.preparation_timeout_s` 默认 300 s 覆盖 uv sync + dvc pull；本机 MinIO 实测 pull 约 3.5 s，
  慢网/大权重时按需提高。
- `repo_source` 是**全局开关**：切到 git 后所有算法仓库（含 rule_base）都走固定 commit Git fetch；
  rule_base 的本地绝对路径同样可作为 repo_url，但必须提供已提交的 40/64 位 hash。

## 4. 验证

### 4.1 IM 离线集成测试（新增）

`inference_manager/tests/test_policy_base_git_source.py`（pytest tmp_path 合成 git+DVC 仓库，不连网/不启 ROS）：

```bash
cd /home/ubuntu/yayyrobotics/inference_manager
env -u PYTHONPATH .venv/bin/python -m pytest -q tests/test_policy_base_git_source.py
# 5 passed（2026-11-17 本机约 8–9 s）
```

覆盖断言：preflight 固定 commit + 只展开 submission.yaml；prepare 后 `.git` 存在、权重目录落盘且
字节等于 fixture、provenance `weights_source=manager_dvc`/`dvc_invoked=true`、仅调用一次 `dvc pull`
（无 remote override）；未知 40 位 commit → `git 子命令失败`；非完整 hash → `只接受完整提交哈希`；
示例配置通过 schema/parser 且能为 policy_base 的 1 GPU / 8192 MiB 请求分配租约。

相关回归（不启 ROS/不连网）：

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q \
    tests/test_local_repo.py tests/test_git_partial.py tests/test_real_runtime.py \
    tests/test_preflight.py tests/test_manager_config.py tests/test_preparation_logs.py
# 84 passed in 24.88s
env -u PYTHONPATH .venv/bin/python -m pytest -q      # IM 全量（ROS e2e 自动 skip）
# 570 passed, 10 skipped in 60.21s
env -u PYTHONPATH .venv/bin/ruff check src tests examples scripts
# All checks passed!
```

### 4.2 真实仓库复核（分两阶段）

- **阶段 1（已完成，未提交工作区就绪性）**：§2.3，DVC 真实 MinIO pull 3.536 s / 7 文件 /
  206,722,219 B / sha256 与 [dvc-distribution.md](dvc-distribution.md) 一致；§2.4 真实 file:// 源固定 commit
  fetch 0.244 s（旧 commit 预期拒绝）。
- **阶段 1.5（已完成，真实内容 + 合成 commit）**：§2.5，IM `ArtifactPreparer` 全链路
  preflight 0.327 s + prepare 3.100 s（含真实 MinIO `dvc pull` 198 MB），`weights_source=manager_dvc`、
  `.git` 保留、HEAD==合成固定 commit、权重 sha256 与源一致。
- **阶段 2（已完成，官方固定 commit）**：§2.6，对 `file:///home/ubuntu/yayyrobotics/policy_base` +
  commit `8d5186fe3594705f23746a3db07226900f00a3af` 跑真实 preflight+prepare：preflight 0.357 s、
  prepare 2.480 s（含真实 MinIO `dvc pull` 198 MB），`weights_source=manager_dvc`、`.git` 保留、
  HEAD==commit、权重 7 文件 / 206,722,219 B、`model.safetensors` sha256 `e145b7a3…`。
  结论：「IM Git 源 + DVC 拉起 policy_base 的准备阶段」组件级验收通过；**IM 实例进程启动与实机/平台接线
  仍属未验证**（见 §6），不得写成端到端实机验收。

### 4.3 官方固定 commit 全链路复核记录（已完成）

> 状态：**已完成（2026-11-17）**，验证 commit = `8d5186fe3594705f23746a3db07226900f00a3af`，
> 命令/输出见 §2.6，证据 `/tmp/p3_im_spike/evidence/09_official_chain.log`。
> 复核基线：该 hash 之后**只允许文档类提交**（如补录本节）；代码、配置或 `.dvc` 指针若有变更，
> 必须重跑官方全链路并把新 hash 与结果追加到本节，不得沿用旧结论。

复核入口（可复现）：

```bash
cd /tmp/p3_im_spike
env -u PYTHONPATH /home/ubuntu/yayyrobotics/inference_manager/.venv/bin/python \
    official_chain.py 8d5186fe3594705f23746a3db07226900f00a3af
# 期望：preflight.commit == 请求 commit；weights_source=manager_dvc；dvc_invoked=true；
#       workspace/source/.git 存在；git HEAD == commit；
#       权重 7 文件 / 206,722,219 B；model.safetensors sha256=e145b7a3…
#       准备耗时：preflight 0.357 s + prepare 2.480 s
```

注意：脚本依赖 `/tmp/p3_im_spike/real_clone_chain.py` 同目录的临时配置派生方式，若 /tmp 已清理，
按 `inference_manager/configs/manager.local.git.example.yaml` 重建等价配置即可（见 §3）。

## 5. 回滚

1. **IM 配置回滚**：把 `runtime.repo_source` 改回 `local`，并按
   `inference_manager/configs/manager.local.example.yaml` 补回 `runtime.local_repo_root`；重启 manager
   （无活动实例时）。rule_base 恢复本地只读复制路径，无需改本仓库。
2. **验证回滚**：`bash scripts/run_local_e2e.sh`（domain 195，本地 rule_base 用例）与
   `env -u PYTHONPATH .venv/bin/python -m pytest -q tests/test_local_repo.py` 通过即视为回滚成功。
3. **回滚后的 policy-based 行为**：IM 会明确拒绝 policy-based（`LOCAL_POLICY_DVC_ERROR`），
   不会静默跳过权重——这是预期安全行为；需要拉起 policy_base 时必须重新切回 git 源。
4. **平台 pin**：FM 仍 pin rule_base `6401700f98e61664686fa422893f928a8258af6c`；切到 policy_base 需
   用户确认，不在本轮范围。
5. 清理：临时 workspace 由 IM 回收流程删除；`/tmp/p3_im_spike` 为本轮证据目录，可整体删除后按脚本复现。

## 6. 未验证项 / 风险

- **固定 commit 全链路**（§4.3）：官方 commit `8d5186f` 已复核通过（§2.6）；但该 hash 之后的代码/配置/
  `.dvc` 指针变更会使结论失效，只允许文档类提交。
- **IM 实例启动**：本轮只验证到 `ArtifactPreparer.prepare`（Git+DVC）；workspace 内 `uv sync --locked`
  （`UvRuntime.prepare`）、进程拉起、`ready`/`stopped` 与回收未在本轮真实执行（需 ROS 2 Jazzy 覆盖层、
  GPU/推理依赖与实例配置就绪）。
- **实机/平台接线**：domain 0 真机、FM/EP/A1 接线、EP 模板与 FM pin 未做；本仓库 `configs/pick_place/*`
  与本文件不改变平台控制面语义。
- **GPU 配额**：示例 `gpus: [{id: "0", memory_mib: 8192}]` 是声明式配额（IM 不探测硬件），真机须按实际
  GPU 显存与并发数调整；本轮未在真实 GPU 上拉起实例。
- **认证**：本轮真实复核用本机 `file://` 源，不需要 Git 凭据；HTTPS/SSH Git 源与 Token/SSH 用户绑定
  沿用 IM 既有实现（有单独测试），未在本轮重新联调。
- **DVC 边界**：只入一个 smoke checkpoint；凭据轮换/只读账号、air-gapped、`dvc gc` 策略未做
  （见 [dvc-distribution.md](dvc-distribution.md) §6）。
- **已知 quirk**：dvc 3.63 把本地 remote 的 `file://` URL 当作相对路径（实测会在仓库内生成 `file:` 目录）；
  本仓使用 S3 URL 不受影响；IM 离线测试的合成 fixture 因此固定用绝对目录路径。

## 7. 变更清单（本轮 P3-4）

- `inference_manager/configs/manager.local.git.example.yaml`：新增（git 源 + DVC 本地示例配置）。
- `inference_manager/tests/test_policy_base_git_source.py`：新增（离线 git 源 + DVC 集成测试，5 用例）。
- `inference_manager/docs/local-mode.md`：追加「git 源 + DVC（policy_base）」小节（配置差异/切换/回滚/限制）。
- `inference_manager/CHANGELOG.md`：未发布条目补充新增/变更/验证。
- 本文件：新增；官方 commit `8d5186f` 之后追加 §2.6/§4.3 复核记录（纯文档提交）。
- **未改 `inference_manager/src/**`**：证明既有生产代码无需改动即可支持本路线（仅新增测试/示例/文档）。
