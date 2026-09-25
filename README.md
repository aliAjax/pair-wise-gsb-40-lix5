# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

仓库内有两套独立入口：

- `app.py`：早期协调原型（事件、资源、线索、离线批次），默认端口 8206。
- `sector_server.py`：**搜救扇区编排台**（本仓库当前主要界面），默认端口 8207，见下文。

## 搜救扇区编排台

值班员登记事件（中心、半径、优先级、最晚完成时刻）后，系统按资源能力、海况、
预计到达时间与剩余航程自动把搜索圆切成扇区并指派船只；两名协调员同时发布时
只保留先到的一次编排；改派或取消会释放原资源并在历史中留下前后指派。

分层维护，互不依赖：

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 规则 | `sector_rules.py` | 纯函数：海况降速与扫宽、能力/海况/航程过滤、ETA 与预计完成时刻、扇区切分与选船 |
| 存储 | `sector_store.py` | SQLite 持久化、编排版本控制（先到先得）、资源占用与释放、变更历史 |
| 页面 | `static/sectors.html` | 编排台界面：扇区图、预演/发布、改派/取消、船位报告、历史时间线 |
| 适配 | `sector_server.py` | HTTP 路由，只组装以上三层 |

### 运行

```bash
python3 sector_server.py --seed   # 首次启动写入演示数据（已有数据时自动跳过）
```

打开 `http://127.0.0.1:8207`。数据库默认为 `sector_console.db`，`--db`、`--host`、
`--port` 可覆盖；重开页面仍能看到当前扇区与历史变更。

### 主要接口

写操作的操作人可放 `X-User` 请求头或 JSON 体的 `by` 字段（中文姓名建议用后者）。

- `GET /api/state`：海况、事件、船只、编排、扇区、历史
- `POST /api/missions`：登记事件（中心、半径、优先级、最晚完成时刻、需求能力）
- `POST /api/vessels`、`POST /api/vessels/{id}/report`：登记船只、录入船位/剩余航程报告
- `POST /api/sea-state`：更新海况
- `GET /api/missions/{id}/plan`：预演编排（不落库）
- `POST /api/missions/{id}/publish`：发布编排，需带 `expected_version`；
  版本不一致返回 409，只保留先到的一次编排
- `POST /api/orchestrations/{id}/reassign`：改派扇区（释放原船，历史记录前后指派）
- `POST /api/orchestrations/{id}/cancel`、`POST /api/sectors/{id}/cancel`：取消并释放资源
- `POST /api/missions/{id}/close`：关闭事件（需先取消生效编排）

## 早期原型（app.py）

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

### 主要接口

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

`test_flow.py` 覆盖早期原型的协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝；
`test_sectors.py` 覆盖扇区规则（海况降速、能力/航程过滤、扇区切分、时限评估）、
发布先到先得、改派释放与前后指派历史、取消释放、持久化重开，以及双协调员并发发布只保留先到者。

## 局限

身份依赖调用方传入的操作人标识；坐标使用球面距离近似；扇区按等角切分、扫宽为经验值，
不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
