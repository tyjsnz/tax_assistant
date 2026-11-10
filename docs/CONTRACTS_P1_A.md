# P1-A 契约：多公司（多账套）+ 地区参数化（CONTRACTS_P1_A）

> 目标：从"单公司本地工具"升级为"可服务多家公司 / 可开源给不同地区用户"的框架：
> 公司注册表、账套数据隔离、切换、旧数据迁移、城建税地区参数化。
>
> **Lead 已完成基础层（只读，勿改）**：`workspace.py`、`config.py` 路径与地区改造、
> `engine/vat.py` 与 `engine/policy_params.py` 地区感知、`tests/conftest.py`、`main.py`/`ui/streamlit_app.py` 启动初始化、CLI 公司命令。

---

## 1. 基础层 API（只读）

### 1.1 `config.py`

```python
DATA_ROOT          # 数据根目录（测试由 TAX_DATA_DIR 隔离）
DATA_DIR           # **当前激活公司**的数据目录（workspace.activate 切换）
BACKUP_DIR/UPLOAD_DIR  # 当前公司的 backups/uploads（随 activate 切换）
LOGS_DIR/EXPORT_DIR    # 全局（不随公司切换）
COMPANIES_FILE     # DATA_ROOT/companies.json
SCHEMA_FILE        # DATA_ROOT/schema.json
COMPANY            # 默认企业信息（首家公司创建时使用）
REGION_PRESETS     # 地区预设：云南/通用 × 市区7% / 县城镇5% / 其他1%
EDUCATION_SURTAX_RATE = 0.03
LOCAL_EDUCATION_SURTAX_RATE = 0.02

get_company() -> dict          # 当前激活公司（含 region_id/city_maintenance_tax_rate）
get_region() -> dict           # {region_id,label,province,city,county,city_maintenance_tax_rate,
                               #  education_surtax_rate,local_education_surtax_rate,
                               #  surtax_rate_full,surtax_rate_discounted}
get_region_presets() -> list[dict]
```

> 注意：**文件路径必须每次调用动态读取** `config.DATA_DIR/BACKUP_DIR/UPLOAD_DIR`，
> 不要在模块导入时缓存（公司切换后缓存会指向旧公司）。

### 1.2 `workspace.py`

```python
data_root() / companies_root() / company_dir(id) / registry_path() / schema_path()
SCHEMA_VERSION = 2
DEFAULT_COMPANY_ID = "main"

load_registry() / save_registry(reg)
list_companies() -> list[dict]   # 含 is_active / data_dir / has_data
get_company(id=None) -> dict|None
active_company_id() -> str
active_company() -> dict          # 未初始化时回退默认企业
activate(company_id, persist=True) -> dict
add_company(payload, activate_new=False, company_id=None) -> dict
update_company(company_id, patch) -> dict
delete_company(company_id, hard=False) -> dict   # 默认软删除；禁止删除当前/唯一公司
ensure_default() -> dict           # 首次升级自动迁移旧数据 + 承接旧企业信息
ensure_schema() / schema_version() / reset_all() / workspace_summary()
```

### 1.3 地区对算税的影响（已实现）

* `engine.calc_vat(...)` 自动读取当前公司地区城建税税率（也可显式传 `city_maintenance_tax_rate`）；
* 附加税费 = 增值税 ×（城建税 + 教育 3% + 地方教育 2%）× 六税两费减半；
* `engine.get_rate_table()` / `describe_policy()` 返回地区信息与正确税率表；
* 公司切换后，算税、申报表、归档中的附加税费自动随地区变化（无需改调用方）。

---

## 2. UI 增量（ui-dev）

只改：`ui/streamlit_app.py`（**仅侧边栏公司切换部分**，保留 Lead 添加的 workspace 初始化行）、
`ui/views/settings.py`、`ui/views/dashboard.py`、`ui/components/archive_api.py`、
可新增 `ui/components/company_api.py`、`tests/test_ui_smoke.py`。不改其它文件。

### 2.1 侧边栏公司切换器（`ui/streamlit_app.py`）

* 在侧边栏企业名片处新增公司选择框：`st.selectbox("当前公司", options=公司列表, format_func=名称+编号, key="company_switch")`；
* 公司列表来自 `workspace.list_companies()`；选中项与 `workspace.active_company_id()` 不一致时：
  `workspace.activate(id)` → `ui.components.cache.clear()`（如有）→ `st.rerun()`；
* 展示当前公司的地区（`config.get_region()["label"]`）与城建税税率；
* 公司数量 >1 时显示切换器；仅 1 家时也可显示但禁用切换（或显示提示）；
* 模块缺失/异常时中文提示，不崩溃。

### 2.2 设置 → 企业信息（`ui/views/settings.py`）

重构该 tab 为**多公司管理**：

1. 当前公司信息编辑表单：名称、简称、统一社会信用代码、主管税务机关、省/市/区县、行业、会计准则、
   地区预设（`st.selectbox` 显示 `label`（城建税税率））、备注；保存调用
   `workspace.update_company(active_id, patch)`，显示保存结果与当前城建税税率；
2. 公司列表：表格显示 编号/名称/地区/是否当前/数据文件；每行「切换」「删除」按钮
   （删除二次确认；删除当前/唯一公司时展示后端 ValueError 中文提示）；
3. 新建公司：表单（名称*、简称、信用代码、行业、地区预设、创建后立即切换）→
   `workspace.add_company(payload, activate_new=...)`；成功后刷新公司列表；
4. 显示工作区信息：数据根目录、schema 版本、当前公司数据目录（`workspace.workspace_summary()`）。

### 2.3 首页（`ui/views/dashboard.py`）

* 企业名片增加地区行（`config.get_region()["label"]` + 城建税税率）；
* 无其它结构性变化。

### 2.4 `ui/components/archive_api.py`

* 修复模块级 `UPLOAD_DIR = Path(config.UPLOAD_DIR)` 的**缓存问题**：改为函数内动态读取
  `config.UPLOAD_DIR`（公司切换后路径会变）；
* 其余归档/备份/恢复封装保持接口不变；确保切换公司后归档目录/备份目录指向新公司。

### 2.5 测试（`tests/test_ui_smoke.py`，保持 9 页）

新增：

1. 侧边栏存在公司切换器（`st.selectbox`）且渲染无异常；
2. 设置页「企业信息」tab 在单公司/空数据下渲染正常（含公司列表、新建公司表单、地区选择）；
3. 使用 `workspace.add_company` 预置第二家公司后，切换 selectbox → `at.run()` 无异常，
   且 `workspace.active_company_id()` 已切换（AppTest 可驱动 selectbox.set_value）；
4. 切换后首页/台账仍正常渲染（数据隔离不崩溃）。

注意：测试环境 `tests/conftest.py` 每个用例会重置为单公司空数据；不要依赖真实 data/。

---

## 3. 完成定义（DoD）

1. `pytest tests/test_ui_smoke.py -q` 全绿（≥ 18 用例）；
2. 手动/Agent 验证：切换公司 → 台账数据隔离 → 算税结果随地区城建税变化 → 归档/备份路径指向新公司；
3. 不改 `workspace.py`、`config.py`、`ledger/**`、`engine/**`、`closing/**`、`archiving/**`、`tests/conftest.py`、`docs/`；
4. 中文提示、异常不白屏；完成后 `send_message` 给 Lead（文件清单/页面结构/测试结果/遗留问题）。
