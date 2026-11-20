# 光伏电站限电事件与损失电量复算

给新能源场站运营分析员使用。仅用有效对照逆变器和相同辐照区间拟合基线；重叠限电事件先求并集再积分。

七个业务模块：遥测采信、设备映射、事件切片、基线拟合、损失核算、能量对账、运行归档。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18244 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。完整对照批INV-A重叠事件取并集后30分钟，基线100kW、实际60kW，损失20kWh；无合格对照或遥测冲突必须保持未定，不能填0。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text`、`overrides` 与 `rule_version` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`，历史版详情加 `?seq=N`。

## 签章批次与同批连续性

批号只绑定不可变输入（源字节+上下文）的哈希，与规则版本脱钩。同输入重算永远落在同一 `run_id`，证据变化以版号 `seq` 递增，不另起批号。每个版本四种角色：`official` 正式待复核、`sealed` 正式已签章、`candidate` 候选待审、`superseded` 被顶替的历史版（签章行永久保留）。部分唯一索引保证同一时刻最多一个正式版。

- `POST /api/runs/{run_id}/rerun`：读该批不可变输入快照重算，批号不变。同证据哈希命中既有版本直接复用，重试不重复生成候选；新证据使旧候选作废并生成下一版候选，候选不顶替正式结果。
- `GET /api/runs/{run_id}/versions`：版本链与待审候选清单。
- `POST /api/runs/{run_id}/seal`： body 含当前 `revision` 与 `reviewer`，只能签正式版。签章依据分别固化采信、基线、损失三域证据哈希及总证据哈希；陈旧版号 409，重复签章 409，候选不能直接签章。
- `POST /api/runs/{run_id}/promote`： body 含 `revision` 与 `candidate_seq`。先用候选自带快照在服务端重放引擎并比对证据哈希，任一不符返回 422 `EVIDENCE_MISALIGNED` 且事务回滚；通过后旧正式版（含签章）转 `superseded`，候选转正式。切换失败旧版签章原样保留。
- `GET /api/export/{run_id}?seq=N`：下载指定版本凭证包，含角色、签章人与三域签章依据；旧签章版本可随时还原下载。

归档页显示当前版本、版本链、候选与正式的三域差异和指标变化，以及签章依据；签章/切换操作带乐观并发控制。旧库首次启动自动迁移为 seq=1 正式版，历史不丢失。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，批号保持不变，证据变化只递增版号并作为候选待审，不会覆盖正式或已签章结果。切换另一示例则创建或复用另一批次。
