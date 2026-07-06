# GitVault

自动备份指定 GitHub 用户的所有公开仓库到本仓库的 Releases。

## 工作方式

- 每天 4 次（UTC 01:00 / 07:00 / 13:00 / 19:00，即北京时间 09:00 / 15:00 / 21:00 / 次日 03:00）定时检测目标用户的全部公开仓库
- 通过 `pushed_at` 判断仓库是否有更新，**只备份有改动的仓库**（状态记录在 `state.json`）
- 备份内容为 `git clone --mirror` 完整镜像（所有分支、tag、完整历史），打包为 `仓库名_YYYYMMDD.zip`
- **每个源仓库对应一个 Release（tag = 仓库名）**，zip 作为 asset 上传到对应 Release 下，天然按项目分类
- 每个仓库自动保留最近 **5** 份备份，更旧的自动删除；同一天多次备份会覆盖当日文件

## 配置

在仓库 Settings → Secrets and variables → Actions 中设置：

| Secret | 说明 |
|---|---|
| `BACKUP_USERNAME` | 要备份的 GitHub 用户名 |


## 手动触发

Actions → Backup → Run workflow。勾选 `force_all` 可强制全量备份所有仓库。

## 从备份恢复

1. 从对应 Release 下载 zip 并解压，得到 `仓库名.git` 裸镜像目录
2. 完整恢复：`git clone 仓库名.git 仓库名`（包含全部分支和历史）
3. 推回 GitHub：进入 `仓库名.git` 目录执行 `git push --mirror <新仓库地址>`
