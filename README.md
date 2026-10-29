# Docx 需求 → 产品原型 AI Agent

基于 **LangChain** 搭建的智能体：上传 docx 需求文档，自动生成可点击访问的产品原型（Vue3 + Element Plus 单页应用）。

## 工作流

```
docx 上传 → python-docx 解析为带层级文本
        ↓
   LangChain Tool-Calling Agent
   ├─ Tool 1: extract_pages_spec   抽取页面清单（JSON）
   ├─ Tool 2: generate_page_html   逐页生成 HTML（Vue3 + Element Plus）
   └─ Tool 3: save_prototype       组装为可访问的原型项目
        ↓
   output/<session_id>/index.html   可在浏览器直接打开
```

## 目录结构

```
prototype-agent/
├── app/
│   ├── main.py                 # FastAPI 入口（上传/预览）
│   ├── parsers/docx_parser.py  # docx 解析为层级化文本
│   ├── agent/
│   │   ├── prompts.py           # System Prompt + 子任务 Prompt
│   │   ├── tools.py             # 3 个 Agent 工具
│   │   └── builder.py           # AgentExecutor 构建与运行
│   └── generator/prototype.py   # 原型项目组装器
├── scripts/make_sample_docx.py # 生成测试用 docx
├── uploads/                     # 上传的 docx 临时存储
├── output/                      # 生成的原型项目（静态目录）
├── requirements.txt
├── .env.example
└── run.sh                       # 一键启动脚本
```

## 快速开始

### 1. 配置 LLM Key

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY
# 默认走智谱 GLM-4-Plus，也可改 base_url 换 DeepSeek / 通义
```

### 2. 启动服务

```bash
chmod +x run.sh
./run.sh
```

首次运行会自动创建虚拟环境并安装依赖。

### 3. 生成测试 docx（可选）

```bash
source .venv/bin/activate
python scripts/make_sample_docx.py
# 生成 samples/sample_requirement.docx
```

### 4. 上传并生成原型

浏览器打开 http://localhost:8000

- 拖拽 docx 到上传区
- 点击"开始生成"
- 1-3 分钟后页面返回预览链接，点击在新窗口打开原型

## 接口说明

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 上传前端页面 |
| `/generate` | POST | 上传 docx，触发 Agent，返回 session_id 和预览 URL |
| `/proto/<session_id>/index.html` | GET | 访问生成的原型项目 |
| `/proto/<session_id>/pages/<key>.html` | GET | 单页面 HTML（iframe 内嵌） |
| `/health` | GET | 健康检查 |

## 响应格式（统一 Wrapper）

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "session_id": "a1b2c3d4e5f6",
    "entry": "/abs/path/to/output/a1b2c3d4e5f6/index.html",
    "preview_url": "/proto/a1b2c3d4e5f6/index.html",
    "log": "PROTOTYPE_READY: ..."
  }
}
```

## 技术要点

- **LangChain 0.3** 的 `create_tool_calling_agent` + `AgentExecutor`
- **ChatOpenAI** 兼容接口，支持任意 OpenAI 协议模型（GLM / DeepSeek / 通义）
- **python-docx** 解析：按段落真实顺序保留标题层级、表格、列表
- **Vue3 + Element Plus CDN**：生成的原型无需构建，浏览器直接打开即可
- **iframe 隔离**：每个页面独立 Vue 实例，互不干扰，可单独预览/复用

## 常见问题

**Q: 生成的页面打不开 / 白屏**
检查浏览器控制台，常见原因：
- LLM 输出的 HTML 不完整（尝试调高 `max_tokens` 或换更强模型）
- CDN 加载失败（建议本地搭建 element-plus 镜像）

**Q: Agent 跑了一半就停了**
- 调高 `builder.py` 中的 `max_iterations` 和 `max_execution_time`
- 文档过长：`tools.py` 已截断 doc_text 到 8000 字符，可分章节多次运行

**Q: 想换 LLM**
修改 `.env`：
```
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

## 后续优化方向

- 接入 LangGraph 替代 AgentExecutor，支持更稳定的多步工作流
- 加入"页面校验工具"：自动检查生成的 HTML 是否包含必备字段
- 支持上传 markdown / pdf / txt
- 加入"原型下载"按钮，把整个 session 打包 zip
- 引入 RAG：长文档先向量化，按章节召回再生成
