# 岸桥观测日表交班

港区检测站 QC-07 的白班登记模拟岸桥位移观测并冻结候选。零点员和量程员分别核验候选里的具名试测行。隔离发布站签发定稿，夜班按站日领取当前件，也可以持原签发号复核历史。

## 运行

使用 Python 3.11 标准库。从项目根目录运行：

```sh
python3 -B check_project.py
python3 -B demo.py
```

`check_project.py` 由运行平台提供并执行完整 unittest 套件。独立检出也可运行 `python3 -B -m unittest discover -s tests -t . -v`。演示在临时目录打开两份 SQLite 文件，完成初始签发、交错投递、换版及断源领取。

## 观测与核验

- `source.database.open_station(path)` 打开检测站。用 `initialize_station`、`initialize_candidate_store`、`initialize_replacement_store`、`initialize_check_standards` 和 `initialize_check_receipts` 初始化各自表。隔离站在另一个文件上用 `open_publication` 和 `initialize_publication` 打开并初始化。重复初始化保留业务记录。`demo.seed_demo` 显式补齐缺失的样例定义，保留已登记的同名定义。
- `StationRegistry` 登记站点、通道和单位。`register_calibration(db, station, channel, calibration_id, scale, offset)` 登记固定校准。`register_revision(db, revision_id, station, day, samples, calibrations, corrects=...)` 接收 `(样本名, 通道, 原值)` 三元组序列和通道到校准号的映射。修订和候选号由调用者给定，在各自命名空间唯一。站日使用 `YYYY-MM-DD`。更正目标必须属于同一站日，已登记的修订可按原号读取。
- `prepare_candidate(db, revision_id, candidate_id)` 固定原序、样本、校准、单位、换算值和日修订。数值输入接受整数、分数字符串及 `Fraction`。缺测用 `None` 表示，换算后仍为缺测。比例 1/2、偏置 1/4 时，3 mV 和 0 mV 分别换算为 7/4 mm 和 1/4 mm。持久化数值用约分字符串表示。定序值摘要允许等值候选做对照，固定输入摘要另绑定候选身份和全部出处。
- `inspect_candidate(db, candidate_id, kind, receipt_id)` 生成固定的 `zero` 或 `span` 回执，同时登记待投递状态。核验口径指定通道、样本名、单位、参照及闭区间。样例区间分别为 `[-1/4, 1/4]` 和 `[3/2, 2]` mm。缺行、缺测、单位不符和超界返回可重读的失败记录。相同候选和类型重读原回执。

## 交班与恢复

- `redeliver_receipt(station_db, receipt_id, port)` 将原回执和固定候选交给本地 `port(receipt, snapshot)`。隔离端口调用 `deliver_receipt(publication_db, receipt, snapshot)`。候选已接收时可以省略来件。结果带原回执号及 `waiting`、`not_selected`、`retired` 或 `issued` 状态。收到结果后检测站确认投递，未获结果可以用 `send_pending_receipts` 或按原号重投。先送达的新版证据可以待换版后重投。端口面向同一模拟站的可信本地调用，隔离站重算携带的核验口径与固定证据的一致性。
- `replace_candidate(station_db, station, day, candidate_id, request_id, port)` 先保存具名待确认请求，经 `port(request, snapshot)` 调用隔离端的 `admit_replacement`，取得持久结果再确认。重复请求读取首次结果。成功换版更新待检候选，原定稿继续可领。已经终止的候选不能重新成为待检候选。旧候选先完成首签时，其历史始终可领。
- 同一候选的两类通过回执齐备后，隔离站原子保存日表、回执及当前指针。提交前异常回滚，提交后丢回包可按原身份重试。端口的 `before_decision` 在申请隔离写事务前运行，`deliver_receipt` 的 `before_commit` 在首次签发的写入之后、提交之前运行。两个钩子供本地中断测试使用。
- `read_report(publication_db, station=..., day=...)` 读当前件，或传 `issue_id=...` 读固定历史。查无成果返回 `None`。`export_report(publication_db, issue_id)` 返回可序列化交班件，`verify_package(package)` 核对固定编码摘要、签发号和换算出处。领件只需要隔离账册。导出摘要用于内容一致性校验，使用者从可信交班渠道取得签发件。

无效输入与身份冲突抛出 `ValueError`。引用的站点、通道、校准、日修订、候选、配置或导出成果不存在时抛出 `KeyError`。SQLite 错误和端口异常向调用者返回，失败事务保持原有定稿。
