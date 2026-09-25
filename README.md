# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录
- `GET /api/incidents/{id}/timeline`

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝。

# 搜救扇区编排台

面向值班员的扇区编排模块，与上面的协调原型相互独立。规则、存储、页面分层维护：

- `sector_rules.py`：纯函数规则。校验事件（中心、半径、优先级、最晚完成时刻）与资源登记，
  把圆形搜索区按方位等分为扇区，并按资源能力、海况、预计到达时间与剩余航程评估匹配、贪心指派。
- `sector_store.py`：SQLite 存储。`BEGIN IMMEDIATE` 事务加 `status='active'` 部分唯一索引，
  两名协调员同时发布同一事件时只保留先到的一次编排；改派或取消在事务内释放原资源，
  并在 `assignment_log` 留下前后指派记录。
- `sector_server.py`：HTTP 路由，默认 `http://127.0.0.1:8207`，数据库为 `sector_console.db`。
- `static/sector_console.html`：编排台页面，每 5 秒刷新，重开页面即恢复当前扇区与历史变更。

```bash
python3 sector_server.py --init --seed   # 可选：写入演示数据
python3 sector_server.py                 # 打开 http://127.0.0.1:8207/sectors
```

写操作需要 `X-User` 与 `X-Role: coordinator` 请求头。主要接口：

- `POST /api/sector/incidents`、`POST /api/sector/incidents/update`（海况/时限变化，版本校验）
- `POST /api/sector/resources`、`POST /api/sector/resources/update`（更新船位与剩余航程）
- `POST /api/sector/plans/preview`：试算编排，不落库
- `POST /api/sector/plans/publish`：发布编排；已有生效编排时不带 `expected_revision` 一律 409，
  携带当前版本号表示重排并取代旧编排、释放其资源
- `POST /api/sector/plans/cancel`、`POST /api/sector/sectors/reassign`、`POST /api/sector/sectors/cancel`
- `GET /api/sector/state`、`GET /api/sector/incidents/{id}/history`

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。扇区按方位角等分，搜索耗时按固定扫宽估算，未考虑风海流漂移与多船协同扫掠。
