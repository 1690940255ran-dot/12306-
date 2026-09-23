# 12306抢票（RailAssist）

12306 购票辅助工具：离线模拟、官方页面只读查询、**定点起售预热**、**受控的单次订单提交**。
Python 3.11+，带 Windows 桌面端（GUI）。

> **重要前提**
> - **真实下单默认锁定**：需显式解锁能力 + 登记与任务版本绑定的授权 + 开启自动提交，才会执行一次提交；
> - **不会自动支付**：订单生成后由你本人在 30 分钟内支付或取消；
> - **登录 / 短信 / 滑动核验必须本人完成**：工具不代替你输密码、验证码或扫码；
> - 使用前请务必阅读文末 **《免责声明》**。

---

## 一、桌面端（Windows）如何使用

### 1. 安装（一次性）

```powershell
cd <项目目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[browser,gui]"
.venv\Scripts\python.exe -m playwright install chromium
```

依赖锁定见 `requirements.lock`。

### 2. 启动桌面端

**双击项目根目录的 `启动RailAssist.bat`** 即可；或命令行：

```powershell
.venv\Scripts\python.exe -m railassist --data-dir .runtime\prod gui
```

### 3. 桌面端三个页签

| 页签 | 作用 |
| --- | --- |
| **任务** | 新建/复制任务表单（出发/到达站、乘车日期、车次白名单、席别优先级、乘车人、人数、金额上限、查询间隔、起止时间、自动提交、**抢票模式**）；立即查询一轮；暂停/停止；mock 或 browser 监控（托盘通知） |
| **订单** | 解锁真实下单（显式确认）、登记授权快照、提交一次订单、核对选中尝试、打开官方订单页、恢复核对 |
| **登录与能力** | 官方窗口登录（本人扫码）、检查登录状态、退出登录、能力登记表 |

### 4. 完整抢票流程（定点起售）

以"某乘车日的票在指定时刻起售"为例：

1. **登录**：「登录与能力」页 → 打开官方窗口 → **本人扫码**登录；
2. **登记能力**：「登录与能力」页 → 执行一次真实只读查询 + 起售时间查询（一次即可）；
3. **建任务**：「任务」页 → 新建任务
   - 日期填**乘车日**（抢票模式只支持一个日期、一个席别）；
   - 车次白名单填目标车次（可多个，**按优先顺序**，命中第一个有票的）；
   - 勾选**抢票模式**；勾选**指定开售时间**并填写（如 `2026-09-24 17:00`）；
     > 工具**不推算**开售时间——各车站起售时间不同（可先查：`sale-time fetch`，会给出该站的每日起售时刻，如江都 17:00、北京南 12:45、南京 08:15）；
4. **解锁 + 授权**：「订单」页 → 解锁真实下单 → 登记授权（乘车人 + 金额上限）；
5. **开始抢票**：选中任务 → 开始抢票。工具会自动：
   恢复加密会话 → 未开售则**本地挂机并保活**（不访问查询接口）→ **开售前 5 分钟**打开官方结果页占位
   → **到明确开售时刻才发出第一次刷新**（2 秒一轮）→ 命中后**重新导航**进入官方确认页核对
   （车次/日期/区间/乘车人/**页面实际票价**）→ 金额复检授权上限 → **单次提交** → 托盘通知你去支付。

### 5. 命令行等价用法

```powershell
$env:Path = "$PWD\.venv\Scripts;$env:Path"

# 登录（本人扫码；--remember 会话经 Windows DPAPI 加密保存）
railassist --data-dir .runtime\prod --remember login
railassist --data-dir .runtime\prod --remember whoami

# 保活（会话为滑动过期；挂机期间每 3 分钟续期一次并重新加密落盘）
railassist --data-dir .runtime\prod --remember keepalive

# 只读查询 / 起售时间
railassist --data-dir .runtime\prod --remember query --from 北京南 --to 上海虹桥 --date 2026-10-07 --adapter browser
railassist --data-dir .runtime\prod --remember sale-time fetch --station 江都 --date 2026-10-08 --adapter browser

# 任务 / 订单
railassist --data-dir .runtime\prod task create --config config/task.example.json
railassist --data-dir .runtime\prod order unlock --confirm
railassist --data-dir .runtime\prod order authorize --task <任务ID> --passengers <乘车人> --max-amount 120000 --confirm
railassist --data-dir .runtime\prod --remember order submit --task <任务ID> --train G547 --seat 二等座 --date 2026-10-07 --adapter browser
railassist --data-dir .runtime\prod --remember order status
railassist --data-dir .runtime\prod --remember order cancel --attempt <尝试ID>   # 放弃未提交过的尝试
```

---

## 二、能力与边界

- **真实只读接入**：官方余票查询、起售时间查询、官方登录窗口、DPAPI 加密会话；页面结构变化自动熔断；模糊余票值不当作无票。
- **受控下单**：授权快照（绑定任务配置版本哈希）→ 预检（能力/登录/授权/金额）→ 幂等落库 → 单次提交 → 结果分类（接受/排队/拒绝/需人工/结果不明）→ 官方核对恢复；同一购票目标普通/候补互斥（数据库唯一索引）；重启只核对、不重放。
- **保活**：Cookie、localStorage 与 12306 sessionStorage 加密保存；挂机期间每 3 分钟调一次官方 `checkUser` 并**重新加密落盘**（官方在活动时可能轮换 Cookie）。
- **监控**：查询合并、公平轮转、同键间隔 ≥30 秒 + 抖动、429 冷却半开、单键熔断、退避重试。
- **通知**：outbox 持久化、按渠道+事件去重、重试；本地日志 + Windows 桌面 toast。
- **基础设施**：SQLite 迁移、JSONL 白名单日志、单实例锁。

## 三、已知限制（诚实清单）

- **速度**：下单链路走浏览器，地板约 **5~8 秒**（其中大部分是 12306 自身的页面加载）。
  对"1 秒售罄"的热门车次，硬抢不是最优解——**建议优先使用官方「候补」**（排队机制，成功率高得多，且手机即可操作）。
- **会话时效**：12306 网页会话为**滑动过期**，实测**空闲约 10 分钟**即失效；保活可续期（实测连续保活 5 小时无异常）。
  会话**活不过夜**，因此"设定时任务、全程无人扫码"无法实现——**扫码必须本人**。
- **必须重新导航下单**：预热页面（放票前加载）的"预订"凭证已过期，点击无效；故下单一律重新导航，
  这会比复用页面多约 5 秒，但这是"能下单"与"必然失败"的区别。
- **真实候补提交**未实现（仅支持普通订单）；结果页票价不解析（确认页票价可用）。
- 打包为单文件 exe（PyInstaller）未做，当前用 `启动RailAssist.bat` 启动。
- 登录态可能因官方强制下线、风险核验、多设备登录等被中断，工具会停下转交本人处理。

## 四、测试与审计

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe scripts\audit_regressions.py
```

覆盖配置/状态机/调度/限频/outbox/起售/订单/候补/页面解析/抢票全流程等；页面解析测试使用**脱敏样本**
（`tests/fixtures/`），不含账号信息。

## 五、更多文档

- 《12306购票辅助工具-开发设计文档.md》——设计文档
- `docs/capability-matrix.md`、`docs/acceptance.md`
- `docs/` 下的现场验证与问题定位记录（含真实环境实测数据）

---

## 免责声明

1. **本项目仅供个人学习与技术研究使用**，不得用于任何商业用途或牟利行为，包括但不限于代他人抢票、
   加价倒卖、黄牛等。
2. **使用风险完全由你自行承担。** 因使用本工具导致的任何后果——包括但不限于账号被风控或限制、
   订单错误、资金损失、行程受影响、违反中国铁路 12306 服务条款等——作者不承担任何责任。
3. 本工具**不会**代替你完成登录、短信/滑动核验、支付等须本人操作的行为，**不会自动支付**，
   **不会存储你的 12306 密码**。
4. 12306 的页面结构、接口与风控策略随时可能变化，本工具**不保证任何时刻可用，也不保证能抢到票**。
5. 请遵守中国铁路 12306 用户协议及相关法律法规。**若你不接受以上任何一条，请立即停止使用并删除本项目。**
