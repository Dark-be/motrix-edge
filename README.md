# 📋 模板项目目录结构

```
  python-application/
  ├── .coveragerc                             # Coverage.py 配置文件
  ├── .gitignore                              # Git 忽略文件配置
  ├── .gitlab-ci.yml                          # GitLab CI/CD 流水线配置
  ├── .gitlab/                                # GitLab 相关配置目录
  │   ├── CODEOWNERS                          # 代码所有者规则配置
  │   └── issue_templates/                    # Issue 模板目录
  │       ├── bug.md                          # Bug 报告模板
  │       └── default_issue.md                # 功能请求模板
  ├── .npmrc                                  # npm 配置文件
  ├── .prettierignore                         # Prettier 忽略文件配置
  ├── .prettierrc                             # Prettier 代码格式化配置
  ├── .vscode/                                # VS Code IDE 配置目录
  │   ├── extensions.json                     # 推荐插件列表
  │   ├── settings.json                       # VS Code 工作区设置
  │   └── tasks.json                          # VS Code 任务配置
  ├── LICENSE                                 # Apache 2.0 开源许可证
  ├── NOTICE                                  # 法律声明文件
  ├── package-lock.json                       # npm 依赖锁定文件
  ├── package.json                            # Node.js 项目配置（用于 CI 工具）
  ├── pyproject.toml                          # Python 项目核心配置文件
  ├── pytest.ini                              # pytest 测试框架配置
  ├── README.md                               # 项目说明文档
  ├── ruff.toml                               # Ruff 代码检查和格式化配置
  ├── src/                                    # 源代码根目录
  │   └── python_application/                 # 主 Python 包
  │       └── __init__.py                     # 包初始化文件（含版权声明）
  ├── tests/                                  # 测试文件目录
  └── uv.lock                                 # UV 依赖锁定文件

```
