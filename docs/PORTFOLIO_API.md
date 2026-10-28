# 组合 API、保存恢复与结束报告（阶段四）

更新：2026-10-27。组合仓储版本 2，账户聚合与固定行情结构版本 1。

阶段四提供完整后端接口；阶段五已增加组合页面和下列只读辅助接口，使用方式见 [PORTFOLIO_UI.md](PORTFOLIO_UI.md)。现有安装目录、原单股数据结构及原单股接口均保留。

## 启动和查看效果

完整自动验收（临时数据、真实本机 HTTP、强制重启、事务中断）：

```powershell
.\.venv\Scripts\python.exe scripts\verify_portfolio_api.py
```

手动调用合成数据接口，用独立目录启动：

```powershell
.\.venv\Scripts\python.exe run_dev.py --portfolio-demo --data-dir .runtime/step4-quotes --users-dir .runtime/step4-users
```

`--portfolio-demo` 或显式环境变量 `KLINE_TRAINER_PORTFOLIO_DEMO=1` 才允许创建 `synthetic-demo` / `synthetic-practice` 训练。前者保留 7 日固定验收样本，后者提供 2024-01-02 至 2024-06-28 的确定性多月练习样本，便于均线和自动播放验收。合成价格、交易日历、停牌及上市退市日期都不是实际证券历史。该开关仅影响新建合成训练；已保存训练的恢复不需要再次开启，也不需要测试夹具或联网。

在另一个 Python 3.12 进程中执行以下示例。首次使用的目录需先创建用户，所有组合请求沿用同一个 `requests.Session`：

```python
from uuid import uuid4
import requests

s = requests.Session()
s.trust_env = False
base = "http://127.0.0.1:5050"

def call(method, path, data=None):
    response = s.request(method, base + path, json=data, timeout=30)
    response.raise_for_status()
    return response.json()

# 独立的新目录执行一次；已存在时直接选择该用户。
call("POST", "/api/users", {"username": "portfolio_demo"})
call("POST", "/api/portfolio/session", {"user": "portfolio_demo"})
state = call("POST", "/api/portfolio/trainings", {
    "request_id": str(uuid4()), "stocks": ["600000", "000001", "600036"],
    "start_date": "2024-01-02", "end_date": "2024-01-10",
    "initial_cash": "100000", "data_source": "synthetic-demo",
})
path = "/api/portfolio/trainings/" + state["training_id"]
state = call("POST", path + "/orders", {
    "request_id": str(uuid4()), "expected_version": state["version"],
    "stock": "600000", "side": "buy", "quantity": 200,
})
advance_request = {
    "request_id": str(uuid4()), "expected_version": state["version"],
    "expected_date": state["current_date"],
}
state = call("POST", path + "/advance", advance_request)
assert call("POST", path + "/advance", advance_request) == state
assert state["account"]["cash"] == "97934.98"
assert state["fills"][0]["price"] == "10.30"
print(state["current_date"], state["account"], state["positions"])
# 关闭并重启服务后，保留会话 Cookie 或重新选择同一用户，再调用：
assert call("POST", path + "/restore", {}) == state
```

示例中的 `state` 包含日期、版本、账户、批次、订单、成交、流水、每日资产、当前报价和训练配置。金额与价格均为十进制字符串，数量为整数股。前端切换股票只查询相应股票的历史，不修改日期、账户或版本。

## 本地用户归属

本程序沿用原项目无密码的本地用户档案。`POST /session` 是选择已有档案，不是远程登录；不能据此部署为公网多租户服务。

后端签发随机的 256 位会话令牌，仅在数据库保存令牌 SHA-256，Cookie 为 `HttpOnly`、`SameSite=Strict`，路径限定 `/api/portfolio`，有效期 30 天。令牌与档案名称及创建标识绑定。切换用户撤销本次浏览器原令牌；删除或重建同名档案不能继承原组合记录。

每个训练使用独立 UUID。查询、恢复、交易、行情、报告及重复请求返回前，均检查会话归属；其他用户得到统一 404。请求不能用 `user/owner` 字段覆盖会话。所有组合接口限制本机 Host、拒绝其他 Origin 及跨站请求；返回 `Cache-Control: no-store`。这些约束只适用于新增组合接口，未改造旧单股的用户机制。

## 接口约定

以下路径均带 `/api/portfolio` 前缀，除 `/session` 外均须携带已选择档案的 Cookie。

| 方法与路径 | 参数或行为 |
| --- | --- |
| `POST /session` | JSON `user`，选择已有本地用户 |
| `GET /options` | 可选 `q/data_source`；搜索名称或代码，返回核验证据覆盖范围、已启用数据源及费率范围，不返回行情；范围是提示，开始前仍需严格检查 |
| `POST /preview` | `stocks/start_date/end_date`，可选 `initial_cash/data_source`；逐股检查完整请求区间，返回支持与否和中文原因；不创建训练、不返回未来价格 |
| `POST /prepare-data` | JSON `{}`，下载并核验固定首批真实银行股区间；只返回范围、名称和核验时间，不接受客户端证据或返回行情 |
| `GET /trainings` | 仅列出当前用户训练的 UUID、配置、日期、版本及状态 |
| `POST /trainings` | `request_id/stocks/start_date/end_date`；可选 `initial_cash/data_source`，默认 `100000/tencent`；返回 201 |
| `GET /trainings/<id>` | 读取当前完整账户状态 |
| `POST /trainings/<id>/restore` | JSON `{}`；直接从持久化状态恢复，保持版本与日期 |
| `GET /trainings/<id>/history/<stock>` | 查询截至组合日期的日线；可选 `through`、`adjustment=none` |
| `POST /trainings/<id>/orders` | `request_id/expected_version/stock/side/quantity`，可选含费用最大 `budget` |
| `POST /trainings/<id>/estimate` | `stock/side/quantity`；返回当前版本、估算订单和费用。使用独立内存引擎运行与提交相同的规则，不写真实账户或订单；正式提交仍需版本校验 |
| `POST /trainings/<id>/cancel` | `request_id/expected_version/order_id` |
| `POST /trainings/<id>/advance` | `request_id/expected_version/expected_date` |
| `POST /trainings/<id>/end` | `request_id/expected_version`，返回结束状态与报告 |
| `GET /trainings/<id>/report` | 结束前 409；结束后返回报告和完整区间行情 |

股票池为 1 至 20 只已支持的沪深主板股票，代码不重复。训练起止日须在已核验公共日历内。首版方向为 `buy/sell`，数量单位为股；初始资金和指定预算上限一万亿元，最多两位小数。仅接受约定字段，浏览器不能上传覆盖证明、公司行为、行情或“已核实”标记。

所有写入自动保存，无须另调保存接口。首次发送操作前生成 `request_id`（建议 UUID），网络超时后沿用原始完整请求重试。同用户创建请求及同训练写请求分别去重：相同标识与相同参数返回**首次保存的原响应**，即使账户后来推进或结束，仍不重新执行；不同参数或操作复用标识返回 409。旧响应中的版本是当时版本，继续操作前可重新 GET 当前状态。

不同请求标识使用同一 `expected_version`，最多一个成功；另一个得到 409。推进另校验 `expected_date`，不能因重复点击推进两天。参数格式错误返回 400；通过格式校验但因资金、股数、停牌等原因被业务拒绝的订单以 `status=rejected` 保存并返回 200，附中文 `reason`，不成交、不留冻结额度。

错误响应为 `{"code":"...","error":"中文说明"}`：400 参数或不支持的数据区间；401 尚未选择有效本地用户；403 来源不允许；404 不存在或不属于当前用户；409 请求冲突、版本/日期过期或训练状态不允许；503 存储暂不可用、结构版本过新或记录核算不一致。503 写入失败后使用原请求重试；数据损坏或新版本库应先处理原因，不会重建空账户掩盖问题。

## SQLite 与恢复

新增数据库位于指定用户根目录的 `portfolio.sqlite3`，不写入单用户的 `trade_records.db/training_history.db`。备份时停止服务后复制完整数据库；不要只备份行情缓存。运行库、会话和缓存均被 Git 忽略。

| 表 | 保存内容 |
| --- | --- |
| `engine_state` | 结构版本、账户版本及聚合：日期、账户、批次、订单、成交、流水、权益、资产曲线、结束状态 |
| `engine_operation` | 引擎推进标识、预期日期和原结果 |
| `portfolio_training` | UUID、用户归属、配置、创建时间、完整固定行情/日历/历史状态/公司行为与 SHA-256 |
| `portfolio_request` | 按用户、作用域、请求标识保存参数指纹和原响应 |
| `portfolio_session` | 令牌摘要、档案归属、失效时间 |

使用 `PRAGMA user_version` 管理仓储迁移：0→1 增加训练元数据和 HTTP 去重表；1→2 增加会话表。迁移置于事务内，新于版本 2 的库在修改表前被拒绝。阶段三旧 `engine_state` 结构版本仍为 1，新增 `status/ended_at` 有兼容默认值；已有无归属引擎记录保留原值，不擅自分配给任何用户。原单股库不参与迁移。

创建时固定公共日历、逐日行情和状态、公司行为、来源及更新时间、内容版本、费率表和市场规则。恢复读取这些固定内容，校验 SHA-256、结构版本、日期和账本一致性，不联网、不读取可能更新的外部缓存、不重新执行买卖或分红，不添加第二笔开账。进程内没有权威账户缓存，每次调用从数据库恢复，多个连接以同一数据库为准。

写操作在 `BEGIN IMMEDIATE` 内校验归属、去重结果、账户版本，执行引擎、保存聚合及请求原结果后统一提交。SQLite 使用完整同步；异常统一回滚。服务强制中断或操作异常不会留下半笔交易；请求结果也不会先于账目提交。数据下载和准入准备在创建写事务前完成，不因网络等待长期占用写锁。

## 结束报告与未来边界

结束不会推进日期或强制平仓。待成交订单全部撤销并释放冻结额度，状态持久化为 `ended`，之后不能继续交易或推进。报告按结束日收盘估值，提供初始资金、总资产、收益金额/比例、已实现与未实现盈亏、总费用、成交数、最大回撤，以及完整账户账本和资产曲线。

仅已结束训练的专用 `/report` 返回股票池完整区间日线，供复盘对照。普通 `/history` 即使结束后仍限制截至账户日期；`full=true`、未来 `through` 和复权请求会被拒绝。组合 UUID 不注册到旧 `active_trainings`，旧单股 `full_data` 无法读取组合数据。结束不会给任何其他用户开放报告。

## 真实数据准入

默认 `tencent` 由后端从行情目录的 `portfolio_reviewed/history.json` 导入已经人工核验的历史状态和公司行为完整性证明，再接入已有公共日历及腾讯原始日线。文件结构为 `schema_version=1`、以股票代码为键的 `securities/actions`；证券字段对应 `SecurityHistory`（含 `stock/listed_on/evidence` 等），行为字段对应 `ActionHistory`（含 `evidence/events`），事件字段对应 `CorporateAction`。各 `evidence` 必须为 `scope=reviewed`，覆盖全部请求日期并有来源、版本、核验依据和带时区的核验时间，不能复制合成证明充数。

这个文件是受信任的后端导入物，不提供浏览器上传入口；程序不能验证任意人工证据声明本身是否真实，部署者需先完成来源与完整性审查。文件不存在、范围不足、证券不匹配、证明为合成或事件不受支持时明确拒绝。

新增 `data_source=sina` 接入已逐项复核的三只银行股和 2024-01-02 至 2024-06-12 固定窗口。`POST /prepare-data` 或 `python scripts/prepare_real_portfolio.py --data-dir <行情目录>` 下载原始资料并检查公司身份、完整分红和配股表、曾用名、股本变动、公共日历及逐日真实行情后，原子发布上述准入文件。真实回执保存来源和 SHA256；新浪回放只读取核验的真实价格，缓存版本包括回执摘要。更新失败不替换已有准入；不扩大到任意股票或事件区间。API 缺省仍为 `tencent`，新版页面主动选择 `sina`。来源及在线验收见 [REAL_DATA_VERIFICATION.md](REAL_DATA_VERIFICATION.md)。
