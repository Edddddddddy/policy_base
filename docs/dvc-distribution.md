# DVC 权重分发（policy_base）

本仓库用 **DVC** 分发 ACT checkpoint：git 只提交 `.dvc` 指针与 DVC 配置，权重（约 197 MB）
存放在 MinIO（S3 兼容）远程，`dvc pull` 按内容哈希还原。**任何情况下不得把权重文件或凭据提交进 git。**

## 1. 当前登记内容

| 路径（仓库内） | 说明 | 指针文件 |
|---|---|---|
| `models/act-20261117-act-wrist-smoke/` | ACT wrist smoke checkpoint（`config.json`、`model.safetensors`、pre/post processor、`train_config.json`），7 个文件 / 206,722,219 B | `models/act-20261117-act-wrist-smoke.dvc` |

- `models/.gitignore` 由 `dvc add` 生成，忽略数据本体（`/act-20261117-act-wrist-smoke`）；
- `.dvc/cache/`、`.dvc/tmp/`、`.dvc/config.local` 已在 `.dvc/.gitignore` / 仓库 `.gitignore` 中忽略；
- 参考 sha256（源 finetune registry、`20261117-act-wrist-smoke`）：`model.safetensors` =
  `e145b7a3b9ca396e93c724a265d8d189e490b7e050405feecef1d8981f4dc844`。

## 2. Remote（已提交，不含凭据）

`.dvc/config`：

```ini
[core]
    remote = minio
['remote "minio"']
    url = s3://so101-archive/dvc/policy_base
    endpointurl = http://127.0.0.1:9000
```

- bucket/前缀：`s3://so101-archive/dvc/policy_base`（复用本机既有 MinIO bucket，避免新建）；
- endpoint `http://127.0.0.1:9000`（MinIO 本机）；
- **凭据不入库**。dvc-s3 走标准 AWS 凭据链：服务用户 `~/.aws/credentials`（本机开发，权限 600）或
  进程环境变量 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`（部署环境注入）。MinIO 凭据由平台
  管理员维护，禁止写进仓库/示例/日志。

## 3. 工具要求

- `dvc`（本机 `/home/ubuntu/.local/bin/dvc`，v3.63.0）**必须带 S3 支持**（`dvc-s3`）。
  安装（本机落地命令，供复现）：

  ```bash
  # pathspec 1.x 与 dvc 3.63 不兼容（scmrepo 依赖 _DIR_MARK），必须锁 <1
  uv tool install "dvc==3.63.0" --with "dvc-s3==3.3.0" --with "pathspec<1" --force
  dvc doctor   # Supports 应含 s3
  ```

- IM 服务启动环境（`factory_manager/local/integration/real/env.sh`）会摘掉 `~/.local/bin`：
  部署时在 IM 配置 `runtime.execution.dvc_executable` 显式写绝对路径（示例见
  `inference_manager/configs/manager.local.git.example.yaml`）。

## 4. 常用命令

```bash
cd /home/ubuntu/yayyrobotics/policy_base

# 拉取权重（IM 在实例 workspace 的仓库根执行同一命令；-j 可并行）
dvc pull

# 校验本地缓存与远程一致（应输出 "Cache and remote 'minio' are in sync"）
dvc status --cloud

# 更新/新增权重（仅在替换模型时）
cp -a <new_checkpoint_dir> models/<new-model-name>
dvc add models/<new-model-name>
git add models/<new-model-name>.dvc models/.gitignore
dvc push
git commit -m "models: <new-model-name>（dvc 指针）"
```

## 5. 全新克隆验证（P3-3 验收）

```bash
rm -rf /tmp/pb_clone
git clone /home/ubuntu/yayyrobotics/policy_base /tmp/pb_clone
cd /tmp/pb_clone
dvc pull
du -sh models/act-20261117-act-wrist-smoke
sha256sum models/act-20261117-act-wrist-smoke/model.safetensors
# 期望 197 MB，sha256 = e145b7a3…（见 §1）
```

实测记录（2026-11-17，见 `.scratch/session-15-platform-diag/p3-dvc-logs/`）：
- `dvc add`：7 files / 206,722,219 B，2.6 s；
- `dvc push`：8 objects（7 文件 + 1 `.dir`）→ `s3://so101-archive/dvc/policy_base`，198 MB，
  约 5 s（本机 MinIO）；`dvc status --cloud` = in sync；
- MinIO 对象核对：`dvc/policy_base/files/md5/**` 共 8 objects / 206,722,854 B；
- 全新克隆 `dvc pull` 验证结果见 P3 交付报告。

## 6. 未验证 / 边界

- dvc-s3 是**机器级**工具依赖（不是仓库依赖）；换机/重装需按 §3 安装。
- 凭据轮换/只读 MinIO 账号属 P5 加固项，本轮沿用现有归档凭据（同一 bucket 不同前缀）。
- 本轮只入 DVC 一个 smoke checkpoint；多模型/版本目录规则在后续替换时沿用 §4。
- 未做离线（air-gapped）场景；未做 DVC 缓存清理策略（`dvc gc` 未启用，防止误删其他实例引用）。
