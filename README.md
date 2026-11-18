# RPA流程自动化中心

面向企业数据采集场景，提供RPA流程编排、元素定位、执行调度和结果校验的自动化运营平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程定义 | `bot_definition` | RPA流程 | 流程名称、目标系统、触发方式 |
| 步骤编排 | `step_editor` | 流程步骤 | 步骤序号、步骤名称、操作类型 |
| 元素定位 | `element_selector` | 定位规则 | 规则名称、选择器类型、定位表达式 |
| 触发配置 | `trigger_config` | 触发器 | 触发器名称、触发类型、调度表达式 |
| 执行记录 | `execution_log` | 执行日志 | 执行编号、所属流程、开始时间 |
| 数据提取 | `data_extract` | 提取规则 | 规则名称、源页面结构、提取字段 |
| 凭据管理 | `credential_store` | 系统凭据 | 凭据名称、目标系统、认证方式 |
| 环境配置 | `env_profile` | 执行环境 | 环境名称、浏览器版本、操作系统 |
| 反封锁 | `anti_block` | 防护策略 | 策略名称、目标站点、请求间隔 |
| 结果校验 | `result_verify` | 校验规则 | 规则名称、校验字段、校验条件 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次编号、触发流程、采集时间 |
| 页面建模 | `page_model` | 页面模型 | 模型名称、目标站点、页面结构 |
| 数据补全 | `data_enrich` | 补全规则 | 规则名称、目标字段、补全来源 |
| 代理路由 | `proxy_router` | 代理节点 | 节点名称、IP地址、出口地域 |
| 频控策略 | `rate_limiter` | 频控规则 | 规则名称、目标站点、每秒上限 |
| 输出格式 | `output_format` | 格式模板 | 模板名称、输出类型、字段映射 |
| 异常日志 | `anomaly_log` | 异常事件 | 事件编号、关联流程、异常类型 |
| 调度计划 | `scheduler` | 采集计划 | 计划名称、绑定流程、cron表达式 |
| 运行概览 | `report_dashboard` | 运行数据 | 统计日期、总执行数、成功数 |
| 任务队列 | `task_queue` | 排队任务 | 任务编号、流程名称、提交时间 |
| 异常恢复追踪 | `anomaly_recovery`（跨模块服务，路由 `/api/anomaly-recovery`） | 异常组 | 异常组编号、执行编号、成员事件数、处理边界、恢复入口 |

## 异常恢复追踪（重复异常聚合）

同一执行对象（按执行编号识别）重试失败时，每次失败仍按当前台账行数生成新的
异常事件编号，但重复异常会聚合进同一个**异常组**：

- 异常组与组内事件同时记录，每条事件都回写异常组编号；
- 同一执行对象同一时刻只允许一个活动异常组，组与恢复入口一一对应
  （重试五次 = 五条事件 / 一个异常组 / 一条恢复待办）；
- 持续故障支持两套处理边界：`并入当前组`（默认）与`关旧开新`，
  两套边界都只保留一个活动恢复入口；
- 恢复入口（重试、确认恢复、忽略）只处理仍未关闭的异常组；
- 确认恢复按异常组编号把终态写回执行记录并联动刷新待办，并发/重复提交幂等只算一次；
- 重试达上限会中断并返回明确原因；全部处理完后工作台显示空态，历史事件仍可查询。

主要接口（均挂在 `/api/anomaly-recovery` 下）：

| 接口 | 说明 |
| --- | --- |
| `GET /groups/active` | 恢复入口工作台，只返回未关闭异常组（空态提示） |
| `GET /groups?exec_no=&status=` | 按执行编号/状态查询异常组，含历史 |
| `GET /groups/{group_no}` | 异常组详情：成员事件与恢复入口 |
| `GET /events`、`GET /ledger` | 异常事件明细、聚合后的异常事件台账 |
| `POST /failures` | 失败上报，自动聚合到异常组 |
| `POST /retry/{exec_no}` | 执行记录重试业务入口，联动刷新待办 |
| `POST /groups/{group_no}/retry` | 恢复入口重试（中断返回原因） |
| `POST /groups/{group_no}/resolve` | 确认恢复并写回执行记录（幂等） |
| `POST /groups/{group_no}/ignore` | 忽略异常组 |
| `POST /groups/{group_no}/boundary` | 切换持续故障处理边界 |
| `GET /reconcile/{exec_no}`、`GET /invariants` | 台账核对与不变量巡检 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
