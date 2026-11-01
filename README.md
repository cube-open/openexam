# 在线考试系统

一个基于 Flask 和 SQLite 的自托管在线考试与学习评测系统，适合课堂测验、训练考试和小型教学团队使用。支持本地部署，也可以放在 Linux/Windows 服务器上，通过 HTTPS 提供访问。

> **安全提示：**首次启动会创建默认管理员 `admin / admin123`。第一次登录后请立即修改密码。演示学生账号为 `student1 / 123456`。不要将含有真实学生信息的数据库、备份或密钥提交到公开仓库。

## 功能

- **考试管理**：创建试卷，设置考试时长、开放时间、成绩/答案/解析公开方式和训练模式。
- **题库管理**：支持单选、多选、判断、填空和组合大题；可单题编辑，也可从 JSON、Excel 或 CSV 批量导入。
- **学生与班级**：管理学生账号、分组、批量导入、禁用账号、重置密码和重新考试。
- **考试体验**：自动保存、断线恢复、服务端计时、切屏记录、交卷后成绩查看。
- **阅卷与分析**：成绩列表、手工改分、答卷查看、统计、错题本和学习成长记录。
- **导出与备份**：PDF 成绩单、Excel 成绩表、数据备份与恢复。
- **组织与权限**：超级管理员、组织管理员、教师和学生角色；服务端执行权限校验。
- **可自定义外观**：配置品牌名称、副标题、Logo、主题色和界面密度。

## 快速开始

需要 Python 3.10 或更高版本。建议在虚拟环境中安装依赖。

### Windows

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

也可以运行 `start.bat` 安装依赖并启动服务。

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

打开：

- 学生入口：<http://127.0.0.1:5000/>
- 管理后台：<http://127.0.0.1:5000/admin/login>

新数据库首次启动时会自动建表、执行迁移并创建演示账号和示例试卷。数据库默认保存在项目根目录 `exam.db`。已有数据库会在启动时升级；升级前会生成迁移快照。

开发时可以运行 `python app.py`。生产环境请使用 `python run.py`，并关闭调试模式。

## 初次部署

1. 设置 `EXAM_SECRET_KEY`，使用至少 32 个随机字符。
2. 启动应用并登录管理后台。
3. 立即修改默认管理员密码，并删除或禁用演示学生和示例试卷。
4. 配置备份目录和保留策略，确认备份可以恢复。
5. 公网部署时使用 Nginx 或 Caddy 反向代理并启用 HTTPS；限制管理后台的网络访问范围。
6. 将 `exam.db`、`*.db-wal`、`*.db-shm`、`.secret_key`、日志和备份保存在安全位置，不要提交到 Git。

## 配置

所有配置通过环境变量设置。Windows PowerShell 示例：

```powershell
$env:EXAM_SECRET_KEY = "请替换为随机生成的长密钥"
$env:EXAM_BRAND_NAME = "我的考试平台"
$env:EXAM_BRAND_TAGLINE = "课程测验与学习反馈"
$env:EXAM_BRAND_COLOR = "#2563eb"
$env:EXAM_UI_DENSITY = "comfortable"
python run.py
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EXAM_SECRET_KEY` | 自动生成并保存在 `.secret_key` | Flask 会话签名密钥；多进程/多实例部署时必须保持一致 |
| `EXAM_PORT` | `5000` | HTTP 监听端口 |
| `EXAM_THREADS` | `32` | Waitress 工作线程数 |
| `EXAM_DB` | `exam.db` | SQLite 数据库路径 |
| `EXAM_DB_POOL` | `32` | 数据库连接池大小 |
| `EXAM_BACKUP_DIR` | `backups/` | 自动备份目录 |
| `EXAM_BACKUP_HOURS` | `24` | 自动备份间隔（小时） |
| `EXAM_BACKUP_KEEP` | `7` | 自动备份保留数量 |
| `EXAM_SINGLE_SESSION` | `1` | 同一账号只保留最新登录；设为 `0` 可允许多端同时登录 |
| `EXAM_BRAND_NAME` | `在线考试系统` | 页面、启动信息和 PDF 中显示的名称 |
| `EXAM_BRAND_TAGLINE` | 空 | 品牌副标题 |
| `EXAM_LOGO_URL` | 空 | 页面 Logo 图片 URL；留空时隐藏图片 |
| `EXAM_BRAND_COLOR` | `#2563eb` | 强调色，格式为 `#RRGGBB` |
| `EXAM_UI_DENSITY` | `comfortable` | 界面密度：`comfortable` 或 `compact` |
| `EXAM_TEMPLATES_RELOAD` | `0` | 设为 `1` 时开发期间自动重新加载模板 |
| `EXAM_DEBUG` | 关闭 | 仅本地开发使用；不要在正式部署中启用 |

环境变量需在启动进程前设置。`.env` 文件目前不会自动加载。

## 账号与权限

新数据库默认包含以下演示账号：

| 类型 | 用户名 | 初始密码 |
|---|---|---|
| 超级管理员 | `admin` | `admin123` |
| 学生 | `student1` | `123456` |

演示学生和默认管理员为方便试用，不会强制改密。投入使用前请在后台修改管理员密码，并删除演示数据。后台新建或批量导入的账号会要求首次登录修改密码。

| 角色 | 权限范围 |
|---|---|
| 超级管理员 | 全局设置、组织、账号、权限与数据管理 |
| 组织管理员 | 管理本组织的教师、学生、班级、考试、成绩与审计 |
| 教师 | 管理题目、考试、监考、阅卷和成绩导出 |
| 学生 | 参加考试、查看已开放成绩、使用错题本 |

## 题目导入

后台题目管理支持下载 Excel 模板。标准列为：

`题型 | 题干 | 选项A | 选项B | 选项C | 选项D | 选项E | 选项F | 选项G | 选项H | 答案 | 分值 | 解析`

- 题型可填单选、多选、判断、填空；组合大题可包含子题。
- 单选答案填写选项字母，例如 `B`；多选答案例如 `ABD`。
- 判断答案可填写 `A/B` 或 `正确/错误`。
- 填空题可填写多个可接受答案，用 `|` 分隔。
- 上传后先预览和检查错误，确认后才会写入数据库。

也可以使用 JSON 导入。建议从后台导出的模板开始，并先在测试数据库验证批量数据。

## 数据库迁移与备份

```bash
python -m examsys.migrate status  # 查看版本和待执行迁移
python -m examsys.migrate up      # 执行待迁移（迁移前自动快照）
python -m examsys.migrate backup  # 手动创建快照
```

迁移脚本位于 `examsys/migrations/`。迁移前生成的 `premigrate_*.db` 快照不会纳入自动轮换清理。备份恢复前请停止应用并保留原数据库副本。

## 可选功能依赖

基础运行依赖列在 `requirements.txt`。PDF 和 Excel 功能分别需要 `reportlab`、`openpyxl`；缺少时相应导出功能会提示安装命令，核心考试流程仍可运行。

```bash
python -m pip install -r requirements-optional.txt
```

健康检查接口：`GET /healthz`。接口会报告应用状态及可选依赖缺失情况。

## 开发与测试

```bash
python tests/smoke_test.py       # 页面、考试流程、导入和导出冒烟检查
python tests/test_deadline.py   # 考试截止时间与自动交卷
python tests/test_phase1.py     # 权限、迁移和会话检查
python tests/test_import.py     # Excel/CSV/JSON 导入
python load_test.py 60          # 使用临时数据库进行并发压测
```

测试脚本使用临时数据库。请在运行前确认相关依赖已安装；并发压测会在本机启动测试服务。

## 项目结构

```text
app.py / run.py          开发与生产入口
examsys/
  config.py              环境变量配置
  db.py                  SQLite 连接、建表、历史升级与初始化
  migrations/            版本化数据库迁移
  security/              登录态、限流、IP 封禁和口令哈希
  services/              判分、统计、PDF、导入、备份等业务服务
  views/                 学生、考试、接口和管理后台路由
templates/               Jinja 页面模板
static/                  CSS、JavaScript、图表和本地字体资源
tests/                   冒烟、权限、时限与导入检查
```

## 开源与贡献

提交问题时请提供复现步骤、预期行为、实际结果和相关日志。请先删除日志中的姓名、学号、IP、Cookie、数据库内容和密钥。涉及安全漏洞时，请不要在公开 Issue 中粘贴真实凭据或学生数据。

提交代码前建议运行相关测试，并确认变更没有包含数据库、备份、日志、密钥或真实用户数据。新增功能请同步更新 README 和必要的配置说明。

**许可证：**本项目采用 [GNU General Public License v3.0](LICENSE)。分发本项目或其衍生作品时，请遵守 GPL-3.0 的条款并一并提供许可证文本。

## 相关文档

- [功能盘点与路线](FEATURES.md)
- [性能优化记录](OPTIMIZATION.md)
- [项目审计记录](AUDIT.md)
