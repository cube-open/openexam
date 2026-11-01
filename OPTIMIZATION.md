# 优化报告：从单文件到分层结构

原项目是一个 2855 行的 `app.py`，包含 41 个路由、建表迁移、判分、统计、PDF、Excel 导入全部逻辑。
这次做了三件事：**拆结构、修 Bug、压并发**。原来的文件完整保留在 `legacy/app_monolith_20261024.py`，方便对照。

---

## 一、新的目录结构

```
app.py                     24 行   兼容入口（仍可 python app.py）
run.py                     76 行   waitress 上线入口
load_test.py              180 行   并发压测（临时库，不碰正式数据）
examsys/
  __init__.py              58 行   应用工厂：只负责组装
  config.py                81 行   全部可调参数集中在这里
  db.py                   319 行   连接池 / 事务封装 / 建表 / 迁移
  schema.sql              141 行   完整表结构 + 13 个索引
  cache.py                 55 行   TTL 缓存
  utils.py                 83 行   时间 / JSON / 题型常量
  logging_setup.py         53 行   日志（控制台 + 5MB 滚动文件）
  context.py               18 行   模板全局变量注入
  hooks.py                 91 行   请求中间件：同源校验、IP 封禁、安全头、慢请求日志
  errors.py                35 行   403 / 404 / 500 统一错误页
  security/
    __init__.py            25 行   统一对外导出
    decorators.py          47 行   登录态装饰器
    passwords.py           44 行   口令哈希（含批量并发）
    ratelimit.py           61 行   滑动窗口限流
    ipban.py               28 行   IP 封禁名单缓存
  services/
    questions.py          119 行   题目读取 + 缓存 + 分组结构
    grading.py             83 行   判分
    stats.py              220 行   排名 / 知识点 / 讲评统计
    pdf.py                138 行   成绩单 PDF
    importers.py          194 行   JSON / Excel / 学生名单导入
    exams.py               28 行   试卷读取与可见性
  views/
    auth.py                97 行   学生登录、管理员登录
    student.py            100 行   面板、资料、成长
    exam.py               309 行   开考、答题页、交卷、成绩、原卷、反思、PDF
    api.py                180 行   保存、心跳、违规
    health.py              18 行   健康检查
    admin_core.py         143 行   后台首页、整站备份恢复
    admin_accounts.py      98 行   管理员账号
    admin_exams.py        364 行   试卷 CRUD、监考、实时看板、统计、讲评
    admin_questions.py    353 行   题目增删改、导入、批量
    admin_students.py     364 行   学生、分组、知识点、IP、日志
    admin_results.py      297 行   成绩、改分、导出、答卷、反思
tests/smoke_test.py       228 行   69 项端到端冒烟测试
```

拆分方式刻意保守：**每个模块用 `register(app)` 注册路由，endpoint 名称与旧版完全一致**，
所以 33 个模板里的 `url_for('dashboard')` 之类一行都不用改，前端 JS 调用的
`/api/save`、`/api/heartbeat`、`/api/violation` 参数与返回结构也保持不变。

---

## 二、发现的问题

### 安全

| # | 问题 | 位置（旧文件行号） | 影响 | 处理 |
|---|---|---|---|---|
| 1 | `SECRET_KEY` 硬编码在源码里 | 27 | 知道这串字符就能伪造 `session["admin_id"]`，直接进后台 | 改为环境变量 `EXAM_SECRET_KEY`，否则首次启动随机生成并写入 `.secret_key`（权限 0600） |
| 2 | `app.run(debug=True)` | 2855 | 生产环境开调试器 = 远程代码执行入口 | 开发模式改为 `EXAM_DEBUG=1` 显式开启，默认关闭 |
| 3 | **交卷后仍能改答案** | 843-863 | `api_save` 只按 `student_id+exam_id` 更新，不看 `submitted_at`，学生交卷后可以把答案全改对，老师复核时会看到"全对的卷子配上低分" | 已交卷直接返回 409 |
| 4 | 无登录限流 | 681-712 | 可暴力撞库 | 按 `IP+用户名` 滑动窗口，只统计失败次数（避免同一 NAT 下整个班级互相拖累），成功即清零 |
| 5 | 无同源校验 | — | 跨站表单可代学生交卷/改资料 | POST/PUT/DELETE 校验 `Origin`/`Referer`，配合 `SameSite=Lax` |
| 6 | 无上传大小限制 | — | 上传超大文件可打满内存 | `MAX_CONTENT_LENGTH = 16MB` |
| 7 | 默认 `admin/admin123` 且无提醒 | 336-338 | 忘了改就是后门 | 保留默认（首次使用方便），但启动日志与 README 明确提示修改 |

### 正确性

| # | 问题 | 位置 | 现象 |
|---|---|---|---|
| 8 | **刷新能重置计时** | 774-779 | `start_exam` 用 `INSERT OR IGNORE`；如果记录已存在但 `started_at` 为 NULL（先调过 `/api/save` 就会这样），插入被忽略，`started_at` 一直是空。`exam_page` 里 `elapsed = now - (started_at or now)` 于是每次刷新都从 0 开始算 |
| 9 | **知识点挂错题** | 1546-1556 | 加完题后靠 `SELECT id FROM questions ORDER BY id DESC LIMIT 1` 找"刚插入的题"。并发加题时会挂到别人的题上；校验失败没插入时也会挂到上一题 |
| 10 | **满分与判分口径不一致** | 427-429 vs 388-424 | 判分只累加非 `group` 题；满分却按"没有 parent_id 的题"求和。带子题的大题会让"满分"和"实际可得总分"对不上 |
| 11 | 交卷非幂等 | 1038-1063 | 双击交卷 / 超时自动交卷与手动交卷同时触发时，两次都会执行 UPDATE 判分 | 改为 `WHERE submitted_at IS NULL` + 判断 `rowcount` |
| 12 | 导入失败也提示成功 | 1545-1556 | 题目校验失败走 `flash(error)` 后仍继续执行挂知识点的逻辑 |
| 13 | 异常可能留下未提交事务 | 全局 | 旧代码每请求新建连接，影响有限；连接复用后必须显式管理事务，否则脏事务会污染下一个请求 |

### 性能与并发

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 14 | **每个请求都查一次封禁表** | 658-667 | `before_request` 对所有学生端请求（含 3 秒一次的心跳）执行 `SELECT 1 FROM blocked_ips` |
| 15 | **心跳每次都写库** | 866-905 | 50 人 × 每 3 秒 = 每秒 17 次 `UPDATE results` + `SELECT messages` + `commit`，写锁被打满 |
| 16 | 每请求执行 7 条 PRAGMA | 223-234 | 连接不复用，每次请求都重新设 WAL/busy_timeout/cache_size |
| 17 | 用 `Semaphore(10)` 控制并发 | 206-210、696-709、768-781 | 信号量只限制"同时进入的人数"，和 SQLite 的写锁没有任何关系：10 个线程同时写照样排队超时，而 100 个读请求也被无谓挡住 |
| 18 | N+1 查询 | 725-746 | 学生面板：1 次查考试 + 每场考试各 1 次查成绩 |
| 19 | N+1 查询 | 1559-1570 | 题目管理页：每道题各查一次知识点 |
| 20 | 重复取题 | 388-424、1029-1033 | `grade_and_review` 每次都重新 `SELECT * FROM questions`；重算成绩时每份答卷都取一遍 |
| 21 | 讲评统计 O(N×Q) 次 JSON 解析 | 536-601 | 每个学生 × 每道题都 `json.loads(r["answers"])`，100 人 × 50 题 = 5000 次 |
| 22 | 监控页 O(N) 次 JSON 解析 | 2503-2545 | 每 2 秒轮询一次，每次都把每个学生的 `answers` 整块解析出来只为数个数 |
| 23 | 知识点分析做笛卡尔积 | 456-464 | `results JOIN questions ON q.exam_id=e.id`，然后 Python 里过滤，行数是"答卷数 × 题数" |
| 24 | 缺索引 | schema | `questions(exam_id)` 完全没有索引，`results(exam_id, submitted_at)` 也没有 |
| 25 | 批量插入逐条执行 | 950-955、1944-1953、2006-2015 | 导入 500 个学生 = 500 次 `SELECT` + 500 次 `INSERT`，外加 500 次口令哈希（约 33 秒） |

---

## 三、做了什么

### 1. 数据库层（`examsys/db.py`）

- **连接池**：`queue.LifoQueue`，默认 32 条连接，PRAGMA 只在建连时执行一次；请求结束归还，
  异常时先 `ROLLBACK` 再归还，脏事务不会串到下一个请求。
- **写事务统一入口** `write_txn()`：
  - 用 `BEGIN IMMEDIATE` 一开始就拿写锁，避免"读锁升级写锁"直接失败（这是 SQLite 最常见的
    `database is locked` 来源）；
  - 遇到 busy/locked 按指数退避重试 6 次；
  - 异常自动回滚。
- **显式事务边界**：连接改为 `isolation_level=None`（自动提交），事务只由 `write_txn()` 开启，
  不再出现"忘了 commit 导致锁不释放"。
- **13 个索引**：`questions(exam_id, id)`、`results(exam_id, submitted_at)`、
  `results(exam_id, last_seen)`、`students(group_id)`、`login_logs(login_at)` 等。
- **迁移**：旧库启动时自动补列 / 补索引 / 回填 `answered_count`，无需手工操作。

### 2. 心跳与高频接口（`examsys/views/api.py`）

心跳是全场最大的写压力来源，改成"读实时、写合并"：

- 违规数没变、且距上次落库不足 3 秒 → 不写 `last_seen`，只读；
- 违规数变了 → 立刻写；
- 未读消息 → 有才写，`UPDATE ... WHERE read_at IS NULL` 一条搞定；
- 剩余时间与考试时长用一次 JOIN 取回，少一次往返。

实测（同一进程内连续 10 次心跳）：

```
实际 UPDATE results 次数: 1   （旧实现 10 次）
单次心跳耗时: 0.3 ms
```

### 3. 缓存（`examsys/cache.py` + `services/questions.py`）

题目是典型的读多写少数据。现在按 `exam_id` 缓存解析后的题目（含 options），
TTL 120 秒，**任何加题 / 改题 / 删题 / 导入 / 删卷都会主动失效**，不会读到脏数据。
`total_score()` 原来每次调用都要查一遍题目表，现在直接命中缓存（0.07ms → 0.004ms）。

IP 封禁名单也做了 5 秒缓存，封禁/解封时主动失效——省掉每个请求一次查询。

### 4. 去掉 N+1

- 学生面板：1 次考试列表 + 1 次 `exam_groups` + 1 次本人成绩（原来 1 + N）。
- 题目管理：1 次题目 + 1 次 `question_kp` 批量取（原来 1 + N）。
- 监控看板：`answered_count` 列直接读，不再解析 `answers` JSON。
- 重算成绩：题目只读一次，所有答卷复用。
- 批量操作一律 `executemany`。

### 5. 统计重写（`services/stats.py`）

- 讲评统计：每个学生的 `answers` 只 `json.loads` 一次（原来每题一次），
  正确答案集合预先算好。
- 知识点分析：从"答卷 × 题目的笛卡尔积"改成"只读 `question_scores` + 按 qid 聚合"。

真实数据（48 名学生 / 5 套卷 / 178 份答卷）对比：

| 操作 | 旧 | 新 |
|---|---|---|
| 讲评统计（40 题） | 11.8 ms | 3.6 ms |
| 知识点分析 | 0.8 ms | 0.4 ms |
| 满分计算 | 0.07 ms | 0.004 ms |

### 6. 并发模型

- 删掉 `Semaphore` 排队机制。它既不解决写锁问题，又会误伤读请求。
  真正的保障是 `BEGIN IMMEDIATE` + `busy_timeout` + 重试。
- waitress 线程数（32）与连接池大小（32）对齐，线程不会互相等连接。
- 口令哈希从 `scrypt` 换成 `pbkdf2:sha256:260000`：两者在本机都是 66~72ms，
  但 scrypt 每次要吃 32MB 内存，120 人同时登录就是 3.8GB 的内存尖峰。
  老库里的 scrypt 哈希在下次登录成功时自动升级，不影响已有账号。

压测 A：120 人**同一瞬间**登录（最坏情况，雷电群效应）

```
python load_test.py 120 --duration 12 --threads 32

总请求 1200（成功 1200 / 失败 0）
接口         次数    p50     p95     p99
login        120   513ms  1235ms  1889ms
start        120   336ms  1212ms  1516ms
exam_page    120   134ms   190ms   268ms
heartbeat    480    17ms   138ms   265ms
save         120    73ms   125ms   154ms
submit       120    47ms   587ms   773ms
result       120    16ms    51ms    57ms
```

压测 B：120 人，登录分散在 10 秒内（接近真实开考）

```
python load_test.py 120 --duration 12 --threads 32 --stagger 10

总请求 1579（成功 1579 / 失败 0）
接口         次数    p50    p95    p99
login        120   128ms  151ms  164ms
start        120     3ms    4ms    6ms
exam_page    120     2ms    3ms    4ms
heartbeat    740     2ms    4ms   10ms
save         239     2ms    3ms    8ms
submit       120     2ms    6ms   10ms
result       120     2ms    5ms   19ms
```

心跳 p95 = 4ms、p99 = 10ms（120 人同时在线，每 3 秒一次心跳）。
压测 A 里的高延迟全部来自"120 个口令哈希同时抢 CPU"，与数据库无关；
B 才是真实场景的分布。两次压测都是 0 失败。


### 7. 批量导入提速

`hash_many()` 用线程池并发做口令哈希（hashlib 的 KDF 会释放 GIL），
导入 500 个学生从"同步 33 秒"降到几秒；落库改成一次 `executemany`。

### 8. 请求中间件独立成层

Flask 没有 Django 那种 `MIDDLEWARE` 列表，请求前后的通用逻辑就是
`before_request` / `after_request` / `teardown_appcontext` 三组钩子。
原来这些和建应用、日志、错误页一起堆在 `__init__.py` 里，现在各归各位：

| 模块 | 职责 |
|---|---|
| `hooks.py` | 写请求同源校验 → IP 封禁拦截 → 安全响应头 → 慢请求日志 → 归还数据库连接 |
| `errors.py` | 403 / 404 / 500，接口返回 JSON、页面返回模板 |
| `context.py` | 模板全局变量（logo、client_ip、display_name） |
| `logging_setup.py` | 日志：控制台 + `logs/app.log`（5MB 滚动 × 3） |
| `views/health.py` | `/healthz` 探活（顺带检查数据库是否可用） |

`__init__.py` 从 129 行降到 58 行，只剩"读配置 → 装钩子 → 注册路由"。

`security.py` 也拆成了包：`decorators`（登录态）、`passwords`（哈希）、
`ratelimit`（限流）、`ipban`（封禁名单）。视图层的导入语句不用改，
`security/__init__.py` 统一导出。

顺带修掉一个自己埋的坑：`logging_setup` 原本用 `if app.logger.handlers: return` 判断
是否已配置——但第一次访问 `app.logger` 时 Flask 就会塞一个 `default_handler` 进去，
条件永远成立，结果**文件日志静默失效**，所有日志只打到控制台。
现在改成显式摘掉 Flask 的 `default_handler` 再装自己的，文件和控制台各一份，不重复。

### 9. 可选依赖不再炸成 500（`examsys/deps.py`）

`openpyxl` / `reportlab` 只服务于「导出 Excel」「生成 PDF」，不是跑起来的前提。
但原来只有两处（Excel 导入、学生端 PDF）做了 `except ImportError` 兜底，
另外四个入口——后台成绩单导出、PDF 压缩包导出、题目模板下载、单份成绩单 PDF——
在缺依赖时直接 500，页面上只有一个白屏错误页，看不出是缺包。

现在统一收口到 `deps.py`：

```python
if not deps.has("openpyxl"):
    flash(deps.message("openpyxl"), "error")   # 未安装 openpyxl，无法完成该操作。请执行：pip install openpyxl
    return redirect(url_for("admin_results", exam_id=exam_id))
```

用 `importlib.util.find_spec` 探测（不真正 import，避免副作用）并缓存结果。
`/healthz` 顺带返回 `optional_missing`，运维不用等老师点导出才发现缺包：

```json
{"ok": true, "optional_missing": ["openpyxl", "reportlab"],
 "optional_hint": {"openpyxl": "pip install openpyxl", "reportlab": "pip install reportlab"}}
```

冒烟测试也跟着改成"缺依赖就跳过"而不是"算失败"——同一份测试在装了依赖的
环境跑 73 项全过，在没装的环境跑 68 过 5 跳过、0 失败。

### 10. flash 消息在三个页面上是静默丢失的

查上一节时顺手发现的：`get_flashed_messages` 那段 3 行代码被复制到了 17 个模板里，
而**成绩相关页面一个都没有**。于是这些提示全部石沉大海：

| 操作 | 提示 | 落到哪 | 原来 |
|---|---|---|---|
| 改分 | 成绩已修改。 | `admin/results.html` | 看不见 |
| 评分 | 改分已保存。 | `admin/results.html` | 看不见 |
| 清除记录 | 已清除该学生的作答记录 | `admin/results.html` | 看不见 |
| 导出 PDF 压缩包 | 暂无已交卷学生可导出。 | `admin/results.html` | 看不见 |
| 学生端下载 PDF | 未安装 reportlab… | `result.html` | 看不见 |
| 后台看单份答卷 | — | `admin/result_detail.html` | 看不见 |

老师点"导出"什么都没发生、点"清除"也不确认，就是这么来的。

修法不是再复制三份，而是抽成 `templates/_flash.html`，21 个页面统一
`{% include '_flash.html' %}`（跟已有的 `_nav.html` / `_subnav.html` 一个套路），
17 处重复代码同时消失。

另外给冒烟测试加了 `check_contains`：改分后跟随跳转，断言正文里真的出现"成绩已修改"。
这类"模板漏渲染"的 bug 不会报错、不会 500，只有断言正文才抓得住。

---

## 四、兼容性

- **模板零改动**：所有 endpoint 名称、模板变量名保持不变。
- **前端零改动**：`/api/save`、`/api/heartbeat`、`/api/violation` 的入参与返回字段一致。
- **数据库自动升级**：直接跑就行，旧 `exam.db` 会被补列补索引；
  迁移前建议先备份（后台"数据管理"可导出 JSON）。
- **入口不变**：`python run.py`（上线）/ `python app.py`（开发）/ 双击 `start.bat`。

有一处**行为变化**是有意的：满分现在按"参与判分的题"求和，与判分口径一致。
如果你的某套卷子用过大题容器题、且容器题分值 ≠ 子题之和，页面上的满分数字会变——
变的是原来算错的那个数。

---

## 五、验证

```bash
python tests/smoke_test.py      # 73 项端到端检查（临时库，不碰正式数据）
python load_test.py 100 --duration 30 --threads 32            # 并发压测
python load_test.py 100 --duration 30 --stagger 15            # 分散登录（更接近真实）
```

`smoke_test.py` 覆盖：学生登录 → 开考 → 保存 → 心跳 → 违规 → 交卷 → 成绩 → 原卷 → PDF，
后台全部 21 个页面、加题 / 编辑 / JSON 导入 / 批量改分 / 删除、批量建号、分组、
IP 封禁解封、发消息、调时间、重算、导出 Excel / PDF 压缩包、整站备份，
并断言"得分 == 满分""满分与判分口径一致"。

---

## 六、还可以继续做（这次没动）

1. **CSRF token**：目前是同源校验 + `SameSite=Lax`，够用但不彻底；要彻底需要改 33 个模板的表单。
2. **多进程部署**：题目缓存、限流、心跳节流都是进程内的，多进程下会各算各的。
   当前架构（单进程 32 线程）足够支撑几百人同时考试；真要横向扩展，得换 PostgreSQL 或加 Redis。
3. **批量 PDF 导出是同步的**：200 份成绩单会阻塞一个工作线程十几秒，可以改成后台任务 + 下载链接。
4. **HTTPS**：建议用 Nginx/Caddy 反代，顺便开 gzip 和 `Strict-Transport-Security`。
5. **审计日志**：谁改了谁的分，目前只记登录日志。
