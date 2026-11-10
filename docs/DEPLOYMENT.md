# 部署与运维

## 1. 本地运行（推荐个人/小企业）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run ui/streamlit_app.py
```

* 数据目录：`data/`（多公司账套在 `data/companies/{id}/`）；可整体复制迁移；
* 默认端口 8501，可在 `.streamlit/config.toml` 或命令行 `--server.port` 修改；
* 首次启动自动创建默认公司并迁移旧版扁平数据（见 `workspace.ensure_schema`）。

## 2. Docker

```bash
docker build -t tax-agent .
docker run -d --name tax-agent -p 8501:8501 \
  -v "$(pwd)/data:/app/data" \
  tax-agent
```

* `data/` 挂载到容器 `/app/data`，账套与设置持久化；
* 容器内无 macOS Vision OCR：申报表截图核对请使用 XML/OFD 发票、手动粘贴文本，
  或在本机运行（OCR 为可选项）；
* 需要 DeepSeek 时在界面设置页填写 Key（保存在挂载的 data 内）。

## 3. 多公司（多账套）

* 一家公司一个独立目录：`data/companies/{id}/`（台账/归档/备份/上传隔离）；
* 新增：设置 → 企业信息 → 新建公司（或 CLI `main.py company-add`）；
* 切换：侧边栏「当前公司」（或 `main.py company-switch --id`）；
* 删除：默认软删除（移动到 `companies/_deleted/`），当前公司/唯一公司不可删除；
* **备份按公司独立**：在对应公司下创建备份，恢复只影响该公司。

## 4. 备份与恢复

```bash
main.py backup --tag before_upgrade      # 创建 zip（data/*.csv + manifest + README，不含 API Key）
main.py backups                          # 列表
main.py restore --file data/backups/backup_xxx.zip        # 只预览（dry-run）
main.py restore --file ... --yes         # 确认恢复（先自动安全备份）
```

* 设置页「数据管理」同样支持创建/校验/删除/上传恢复；
* 恢复前务必确认当前公司（多账套下备份文件属于特定公司）；
* 建议每周备份 + 每次升级前备份，备份文件异地保存。

## 5. 升级

1. `main.py backup --tag before_upgrade` 备份；
2. 更新代码 / 镜像（`docker pull` 或 `git pull`）；
3. 启动：`workspace.ensure_default()` 自动执行 schema 迁移（当前 schema v2：多公司目录）；
4. 打开界面检查：公司切换、台账数据、期间状态、归档/备份列表；
5. 重大版本升级请阅读 [CHANGELOG.md](../CHANGELOG.md) 与 [RELEASE.md](RELEASE.md)。

## 6. 安全建议

* 不要把 `data/`（含 `user_settings.json`、客户信息）提交到公开仓库；
* 服务器部署时限制访问：反向代理 + HTTPS + 访问认证（Streamlit 本身无账号体系，
  多人使用请放在 VPN/内网或加代理认证）；
* 定期轮换 DeepSeek API Key，发现泄露立即在平台吊销；
* 详见 [SECURITY.md](../SECURITY.md)。

## 7. 迁移到新机器

```bash
# 旧机器
main.py backup --tag migrate
# 复制 backup_*.zip 到新机器 data/backups/ 后
main.py restore --file data/backups/backup_xxx.zip --yes
```

或者直接复制整个 `data/` 目录（保持目录结构）。

## 8. 常见部署问题

| 现象 | 处理 |
|---|---|
| 端口占用 | `--server.port 8502` |
| 容器内 OCR 不可用 | 使用 XML/OFD 发票或手动粘贴文本（本机运行可用 macOS Vision） |
| 报表 PDF 中文乱码 | PDF 使用 reportlab 内置 STSong-Light；如自定义字体请检查 |
| 升级后数据看不到 | 检查当前公司（侧边栏）与 `data/companies.json`，必要时 `main.py companies` |
| 备份恢复后索引变化 | 恢复会还原全部 CSV（含 archives/backups 索引），安全备份文件仍在磁盘 |

## 9. 参考部署：阿里云 + 宝塔 + Docker + Nginx + Let's Encrypt（tax.flacn.net）

本项目官网与在线演示即按此结构部署（CentOS 7.9 + 宝塔 + Docker）：

```text
/                  → /www/wwwroot/tax/site（静态官网，含下载包）
/app/              → 反向代理 127.0.0.1:8501（Streamlit，baseUrlPath=/app）
/download/...      → 安装包下载
```

* 构建镜像：`docker build -t zhixiaoshui:1.7.0 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .`
* 运行容器：`docker run -d --name tax-agent --restart unless-stopped -p 127.0.0.1:8501:8501 -v /www/wwwroot/tax/data:/app/data -e STREAMLIT_SERVER_BASE_URL_PATH=app zhixiaoshui:1.7.0`
* 初始化演示数据：`docker exec tax-agent python scripts/seed_demo.py --reset`
* Nginx：`/app/` 需转发 `Upgrade`/`Connection` 头，`proxy_read_timeout 86400`；`/app/_stcore/health` 作健康检查；
* SSL：Docker certbot webroot 申请，证书在 `/etc/letsencrypt/live/<域名>/`；
  `/etc/cron.d/certbot-tax` 每日 03:00 `certbot renew` + `nginx -s reload` 实现到期自动续签；
* 公共演示数据：`/etc/cron.d/tax-demo-reset` 每日 04:30 `seed_demo.py --reset` 并重启容器。
