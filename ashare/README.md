# OpenPike Stocks

中文看板：<http://127.0.0.1:8765>。独立于现有期货程序；无券商连接或证券委托接口。评分100分是固定研究公式，80分不是80%胜率。

公开版本默认本机运行。可选远程代码使用 `stocks.example.com` 示例域名，未配置、未部署；不是公开体验地址。见 [远程说明](REMOTE_ACCESS.md)。

## 本机软件

提供 macOS 独立窗口应用源码，可自行构建到 `/Applications/OpenPike Stocks.app`，桌面同名入口链接到该应用。可双击使用；窗口内嵌本地看板，独立图标、中文菜单、⌘R刷新、策略档案与运行记录入口。应用自动核实当前工程的后台身份并在未运行时启动股票服务；关闭窗口后后台继续监测。数据和依赖仍位于本工程，不能移动或删除工程目录。构建与说明见 [desktop/README.md](desktop/README.md)。

首页顶部统一显示「当前最推荐买点」「持仓卖出提醒」。买点只采用所有条件通过的第一名；没有合格买点时展示等待与评分观察第一名的阻断原因。每秒在页面检查提示有效期限，过期、断线或交易时段结束时撤销有效状态。卖出按实际登记批次的原失效价、第一压力和15个交易日限制检查，结合T+1、有效报价和买一数量；不会把历史提醒当作当前可卖信号。`GET /api/actions` 提供同一只读汇总，包含在 `/api/dashboard`；不产生证券委托。

## 安装与运行

在工程根目录执行：

```sh
uv venv --python 3.12 .local/ashare/venv
uv pip sync --python .local/ashare/venv/bin/python --require-hashes ashare/requirements.lock
.local/ashare/venv/bin/python -m pytest ashare/tests -q
.local/ashare/venv/bin/python -m ashare.service install
```

后台服务 `local.ashare.monitor` 只监听本机8765，用户登录后启动，退出异常时自动重启。`python -m ashare.service status/stop/restart` 管理该服务；不操作期货服务。独立环境、SQLite WAL、行情和日志在 `.local/ashare/`。停止后再次 install 可恢复。日志为 service.log / service.error.log。

## 行为与证据

- 腾讯主报价，新浪交叉核验及备用。单位统一为股、元；五档只表示公开快照，不是完整Level-2。每5秒重点报价，最多50只。新浪全市场列表每5分钟发现主板候选，ST/退市风险不进入正常候选。
- 交易日历按上交所2026年正式休市安排。未知年份关闭新买，日历需依据新年度交易所通知更新。午休、竞价和休市不发新买提示。
- 日线前复权价格来自腾讯当前公开 newfqkline 接口，同时提供真实成交额（万元转元）和成交量（手转股）；旧接口及东财仅作备用，缺少成交额不估算。日线只用前一已收盘交易日及更早记录。新浪五分钟历史按实际返回数量保存，不固定为七日。分钟时间为结束标记，至少过3秒才确认完成。
- 分类使用单一的东方财富财报行业分类及主板成员名单，明确报告期、来源与核验日期；不是申万分类，也不与新浪旧分类混算。每个行业至少5个有效成员且覆盖80%；首次启动分批采集日线，覆盖未达标不放行。全市场“扫描”是发现候选，完整100分评分仅对最多50只重点股票运行，并显示覆盖情况。
- 已公开利润表、同报告期现金流、年营收由程序自动采集并尽可能用新浪数值交叉核验；未来公开的数据不进入评分。财报预约披露日程及公告全文按优先级补采，减持、冻结、催化等仍由当前聊天依据原文核实登记。公告标题自动取数不等于已审阅；风险清除、催化核查须显式标明完成且24小时内有效。资料不足不产生合格买点。
- 回踩与后一根确认必须连续且已完成，确认最多保持一个五分钟间隔，午休前的确认不沿用到下午。最近压力为两侧各两根完成线确认的高点或涨停价；较近压力不被跳过。
- 持仓退出按原失效价、第一压力、15个交易日判断。手工成交按实际价格登记，不能假设止损价成交。T+1按原买入日期判断，零股一次性卖出。未登记退出价的期初持仓显示缺项，只检查期限。
- 纸面账户默认10万元、5仓、每仓不超过权益20%，按卖一／买一显示数量限制填单。每边0.2%为费用和滑点合计估计，不另伪造填单；跳空按实际盘口，跌停无买一数量时等待。纸面结果不代表实际可成交保证。真实记录与纸面账户隔离。
- 每周最多3个固定权重候选，最多6个同时验证，所有硬风控不变。冻结后15个实测交易日隔离，再需要60日、100笔完整前向交易。部分退出不重复计作多笔。还要求收益置信下限、双倍成本、回撤、基线收益比较和单票／单日20%集中度门槛。保留提案、验证、升级与回滚记录。没有达到门槛不展示胜率。

日历来源：<https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml>。

## 当前聊天通知协议

现有提醒通过每分钟心跳访问本地事件队列。事件仅包括新买点、持仓退出条件、买点失效、新公告、首次故障和经验证的版本变化。重复事件有持久化键，消费者领取3分钟租约，处理后确认；未确认可在租约过期重试。投递与确认之间进程崩溃仍可能出现重复，因此属于至少一次处理，不能承诺跨聊天系统严格一次送达。

`local_detection_delay_seconds` 是确认线结束至本地检测的时间，包含等待完成与取数。确认记录 `chat_delay_seconds` 是本地事件入库至聊天处理的时间；二者不等于交易所源头到用户的纯网络时延。电脑、应用及服务都需运行。

## 接口

接口结构说明：<http://127.0.0.1:8765/openapi.json>。

GET `/api/bars/{code}?frame=5m|day` 返回真实K线、完成状态和复权类型；图表可展示形成中的线，信号仅使用已完成线。

GET `/api/data-coverage` 区分真实资料缺失和买点条件未触发；`/api/financial` 返回自动财报证据；`/api/disclosures/{id}` 返回已补采的公告正文（下载不代表核查完成）。

GET `/api/archive` 返回统一档案索引；`/api/archive/versions/{id}` 返回规则与源码；`/api/archive/decisions/{id}` 返回观察判断；`/api/archive/decisions/{id}/inputs` 无损还原当时完整输入。

GET `/api/health`、`/api/quotes`、`/api/rankings`、`/api/signals`、`/api/holdings`、`/api/evidence`、`/api/research`、`/api/research/queue`、`/api/learning`、`/api/events?pending=true`、`/api/dashboard`。

POST `/api/trades`、`/api/opening-balances`、`/api/research` 均携带唯一 `request_id`：相同内容重试返回原结果，编号相同但内容不同拒绝。POST `/api/events/claim` 返回事件及token；POST `/api/events/{id}/ack` 携带token、disposition（notified/silent）、explanation。POST `/api/learning/rollback` 用于恢复上一正式版本。

研究 facts 只能提交原始净利润、同比增长（小数比例）、经营现金流、公告事件及核查状态，不提供直接覆盖评分、止损距离或盈亏比接口。公告催化必须有公开时间、来源和订单/年营收或实际新增产能比例。所有数据声明需有来源，分析者负责原文核验。

## 验收

`ashare/tests` 覆盖正例、固定分数、缺项不补分、近压力、未完成线、旧报价、封板、日历、ST排除、未来数据、冻结阻断、T+1、跳空真实价格、纸面与真实分离、重启去重、事件租约、接口验证及研究不能改写风控。公开仓库不包含个人环境的行情和后台验收记录；需在安装者自己的环境中复验。

## 统一策略档案与补采

看板新增“策略档案”，唯一保存根目录为 `.local/ashare/strategy_archive/`，入口说明为该目录 README.md。策略规则、权重、费用、硬条件、源码和SHA256保存在 versions/；连续竞价时最多每分钟一份观察判断，没有买点也保留 WAIT；新买点另存即时输入快照。休市时判断未变不重复写。

完整输入按内容哈希和128根区块压缩复用，保留每次取数时间，查询接口还原原始结构。evidence/保存自动财报原始值、公告全文、预约日程和人工复核；events/逐条保存提示；outcomes/保存真实/纸面成交及完成交易；validation/保留候选方案及验证结果。已存档文件不可覆盖，旧记录迁入不补造当时源码。真实持仓仍由手工登记，不自动委托。

日线补采先处理重点股、沪深300及相关行业成员，然后全市场；失败标的指数退避，不再反复占据队首。缺少成交额的记录仍排入重采。资料覆盖率与评分输入完整度分开显示：没有突破或回踩属于等待条件，不是假缺失，也不能被补成分数。分数和100%关键输入门槛保持不变。

股票数据库K线无损压缩存储，支持旧格式读取；离线迁移先保存压缩备份并逐组校验，报告在 `.local/ashare/compression-acceptance.json`。磁盘低于128MiB暂停补采与新买提示、暂停原始报价历史追加，但继续更新最后报价供已登记持仓风险观察；不会删除个人文件。需要为长期档案预留足够磁盘空间。

接口适配依据：AKShare的公开[腾讯日线实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py)、[三大财务报表实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_three_report_em.py)与[预约披露实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_yjyg_em.py)。免费公开接口仍可能限流、延迟或改版；真实测试通过不代表未来稳定性保证。
