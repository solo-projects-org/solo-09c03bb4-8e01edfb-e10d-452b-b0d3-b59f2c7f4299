# 助餐补贴领取率差值区间比对

Flask + SQLite 网页应用：导入逐户调查 CSV 和同批样本核定的全区合资格户数，补录回访答案，并计算两条街道“领取户数 / 合资格户数”差值在所有相容补全中的精确最小值与最大值。

## 启动

### Docker Compose

```bash
docker compose up --build
```

启动后打开：

- <http://127.0.0.1:8000/>
- 同时映射了 Flask 常用端口：<http://127.0.0.1:5000/>（均转到容器 `8000`）

SQLite 数据库保存在 Compose 命名卷 `subsidy-data` 的 `/data/subsidy.db`。若本机 `5000` 端口被占用，可删除 `compose.yaml` 中的 `"5000:8000"` 映射。

### 本地直接运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python wsgi.py        # 等价入口：python app.py
# 或：gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 wsgi:app
```

也可指定数据库位置：

```bash
DATABASE_PATH=/path/to/subsidy.db python wsgi.py
```

## 网页操作

1. 首页选择逐户 CSV，填写“全区核定合资格户数”，点击“导入并整批校验”。
2. 没有数据时可点击“一键载入库内样例”。样例为 3 条街道、16 户，核定数 8。
3. 进入“逐户数据/回访”，文本框已放好演示批次，可直接“整批提交回访”；再次提交会显示新补 0 项、相同答案未增计。
4. 进入“街道比较”，选择第一条街道（被减数）和第二条街道（减数）。
5. 若所选街道存在合资格分母为 0 的补全，页面会显示警告；边界只使用两个分母均为正的补全。若不存在这样的补全，页面明确提示“不报差值”。

CSV 表头支持：

```csv
户号,街道,是否有资格,是否领取
H001,人民路街道,是,是
H002,人民路街道,是,否
H003,人民路街道,是,
H004,人民路街道,,否
```

也支持 `household_id,street,eligible,received`、`yes/no`、`true/false`、`1/0`、中文“是/否”和 UTF-8/GB18030 编码。后两项留空表示未知。领取为“是”的家庭在任何补全中都必有资格；资格为“否”却领取为“是”会被视为冲突。

## 主要 HTTP/JSON 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /healthz` | 健康检查 |
| `POST /import` / `POST /api/import` | multipart 上传 `file` 与 `district_total`，或 JSON 提供 `csv`、`district_total` |
| `POST /api/sample/load` | 载入库内样例 |
| `GET /sample/csv` | 下载样例 CSV |
| `GET /households` / `GET /api/households` | 逐户页面/JSON |
| `POST /callbacks` / `POST /api/callbacks/batch` | 成批回访；支持 JSON、CSV 文本、表单数组 |
| `POST /api/households/<户号>/callback` | 单户补空的便捷接口 |
| `GET|POST /compare`、`/api/compare`、`/api/bounds` | 比较街道，参数 `street1`、`street2` |
| `GET /api/state` | 当前版本、核定数、户数和相容性状态 |

回访 JSON 示例：

```bash
curl -X POST http://127.0.0.1:8000/api/callbacks \
  -H 'Content-Type: application/json' \
  -d '{
    "answers": [
      {"household_id":"H004","eligible":"是"},
      {"household_id":"H005","eligible":"否"},
      {"household_id":"H102","eligible":"是","received":"是"}
    ]
  }'
```

比较示例：

```bash
curl 'http://127.0.0.1:8000/api/compare?street1=人民路街道&street2=建设路街道'
```

返回中的 `min`、`max` 含精确分数、小数和百分比；顶层也有 `min_value`、`max_value`、`minimum`、`maximum` 等便捷字段。`revision` 是本次快照使用的生效数据版本。

## 区间算法：不逐户枚举组合

同一街道中答案形态相同的户先聚合成 7 个计数：

- `a`：已知领取；必为合资格。
- `b`：已知合资格、未领取。
- `d`：已知合资格、领取项空。
- `f`：资格项空、已知未领取。
- `g`：两项皆空。
- `x`：已知不合资格。

因此：

- 最少合资格数 `p = a+b+d`
- 最多合资格数 `u = p+f+g`
- 固定分母 `e` 时，最少领取数为 `a`
- 固定分母 `e` 时，最多领取数为  
  `a+d+min(e-p,g)`

全区核定数 `T` 通过“其他街道还能吸收多少合资格户”耦合两个所选分母，而不是把两条街道分别取最好情况。对第一条街道的每个可行整数分母，第二条街道的最大领取率在其峰值点或可行区间端点取得，可用 `Fraction` 精确比较。复杂度为聚合 `O(户数)` 加 `O(第一条街道可行分母数)`，没有 `2^漏答数` 的逐户组合枚举。随机小数据集已用 `itertools.product` 穷举全部补全交叉验证。

## 事务、并发与快照

- 导入和成批回访都先完整解析、合并、校验，再写入；任何冲突都会回滚整批，旧数据保持可查。
- 回访使用 SQLite `BEGIN IMMEDIATE` 写事务，并在取得写锁后重新读取所有已生效答案，再把“本批答案 + 已生效答案 + 全区核定数”合并核对。
- 相同答案重复提交是幂等 no-op：`newly_filled` 为 `0`，`unchanged_answers` 计数增加，数据不会重复。
- 比较开始时在一个只读事务中读取 `revision`、核定数和各街道聚合数，随后立即提交/关闭事务，在数据库连接外计算。因此一次比较不会混用两个版本，计算期间也不阻挡合法回访写入。SQLite 启用 WAL 和 `busy_timeout`。
- Compose 使用单 Gunicorn worker、多线程，配合 SQLite 写事务串行化并发回填。

## 自测结果

交付前已在 Python 3.11 虚拟环境安装 `Flask 3.1.3`、`gunicorn 27.0.1`，并执行：

```bash
PYTHON=/tmp/gsb-venv/bin/python scripts/self_check.sh
```

结果（时间随机器略有差异）：

```text
Ran 10 tests ...

OK
```

覆盖：

- 样例的精确边界：人民路街道 − 建设路街道为 `-7/15` 到 `4/5`（`-46.67%` 到 `80.00%`）。
- 400 组随机数据与穷举所有逐户补全结果一致，包括分母为零判断。
- 网页首页、样例载入、比较页、零分母警告、无正分母时不报差值。
- 回访 CSV/JSON、同答重报不增计。
- 冲突回访和不相容导入整批回滚，原数据版本和户数不变。
- 批次中一条冲突时，同批另一条合法修改也不部分入库。
- 15 轮双线程并发冲突回填：每轮恰好一个批次提交、另一个因全区核定数冲突退回，库内恰好 1 户新增合资格。
- 额外 HTTP 实测：Gunicorn 下 `/healthz`、样例和比较接口均正常；6000 户数据上用 16 个线程重叠发起 80 次比较和 80 次合法回访，无 5xx，比较快照出现 r5/r6 但每次响应只带一个一致 revision，最终回填恰好生效 1 项。

## 未完成项与限制

- 当前沙箱没有 `docker` / `docker compose` 可执行文件，因此未能在本机实际执行镜像构建和 Compose 启动；已用 PyYAML 解析校验两个 Compose 文件，并用 Gunicorn 直接验证了镜像中的启动对象 `wsgi:app`。
- 未做登录鉴权、操作审计页面和大表分页；当前面向可信民政内网数据员的单机/单卷部署。
- 比较请求是同步返回的精确快速计算，没有单独做“进度条/历史比较任务”页面；一致性通过版本化只读快照保证。
