# 系统升级与更新说明

## 桌面版（手动）

1. 启动时自动检查官网 `version.json`，发现新版本会提示；
2. 点下载，退出旧版，替换 App/安装目录；
3. 数据保存在用户目录，不会被覆盖：
   - macOS：`~/Library/Application Support/税小智`
   - Windows：`%APPDATA%\税小智`

关闭检查：启动前设置环境变量 `TAX_DISABLE_UPDATE_CHECK=1`。

## 局域网 Web 版

```bash
# 升级前备份
# 进入部署目录
cd /path/to/tax
# 拉取新版本
git pull
# 或 docker compose pull
docker compose build
docker compose up -d
# 健康检查
```

数据使用持久化目录/卷，重建容器不丢。

## 发布新版本时维护者要做什么

1. 修改 `config.APP_VERSION`；
2. 更新 `CHANGELOG.md`、发布说明；
3. 构建桌面版/源码包，计算 SHA-256；
4. 更新官网下载文件与 `site/version.json`；
5. 部署 Web；
6. 发 GitHub Release；
7. 验证旧版本能收到更新提示。

> 客户端只检查 `version.json`，不做自动替换；自动更新需要代码签名/公证后再启用。
