"""FastAPI Web 入口。

用法：
  ./run.sh

接口：
  POST /generate        上传 docx（可选）+ 站点地址/账号 + 补充需求，
                        SSE 流式推送浏览器进度、Agent 工具调用、模型实时输出
  GET  /proto/{sid}/    访问生成的原型项目（静态资源，含截图）
  GET  /                前端上传页
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent.builder import OUTPUT_ROOT, run_pipeline_streaming
from app.parsers.docx_parser import parse_docx
from app.parsers.zip_parser import parse_zip

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Docx/网站 → 原型 AI Agent")

# 静态挂载生成的原型（截图也在其中）
app.mount("/proto", StaticFiles(directory=str(OUTPUT_ROOT)), name="proto")


INDEX_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>系统调研 → 产品原型 AI Agent</title>
  <link rel="stylesheet" href="https://unpkg.com/element-plus/dist/index.css">
  <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
  <script src="https://unpkg.com/element-plus"></script>
  <script src="https://unpkg.com/@element-plus/icons-vue"></script>
  <style>
    body { margin:0; padding:32px 16px; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
           background:#f3f5f9; color:#303133; }
    .box { max-width:960px; margin:0 auto; }
    .tip { color:#909399; font-size:12.5px; line-height:1.8; }
    .source-row { display:flex; align-items:center; gap:8px; margin-bottom:4px; }
    .source-num { width:22px; height:22px; border-radius:50%; background:#1890ff; color:#fff;
                  font-size:12px; display:inline-flex; align-items:center; justify-content:center; flex:none; }
    .source-title { font-weight:600; font-size:14px; }
    .source-optional { font-size:12px; color:#bbb; font-weight:normal; }
    .acct-row { display:flex; gap:12px; margin-top:10px; }
    .acct-row .el-input { flex:1; }
    /* 实时面板 */
    .run-card { margin-top:16px; }
    .phase-hero { background:linear-gradient(135deg,#e6f4ff,#f0f7ff); border:1px solid #bae0ff;
                  border-radius:8px; padding:14px 18px; margin-bottom:14px; }
    .phase-hero .label { font-size:12px; color:#1890ff; font-weight:600; }
    .phase-hero .text { font-size:16px; font-weight:600; margin-top:4px; color:#1d2b3a; }
    .phase-hero .sub { font-size:12px; color:#889; margin-top:2px; }
    .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:#1890ff;
             margin-right:8px; animation:pulse 1s infinite; }
    @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(24,144,255,.5);} 70%{box-shadow:0 0 0 8px rgba(24,144,255,0);} 100%{box-shadow:0 0 0 0 rgba(24,144,255,0);} }
    .panel-section-title { font-size:13px; font-weight:600; color:#606266; margin:14px 0 8px;
                           display:flex; align-items:center; gap:6px; }
    .stream-box { background:#1e1e23; border-radius:8px; padding:12px 14px; max-height:340px;
                  overflow-y:auto; font-size:12.5px; line-height:1.7; }
    .act { display:flex; gap:8px; padding:3px 0; color:#cfd3dc; }
    .act .ico { flex:none; width:52px; font-size:11px; padding-top:1px; }
    .act .ico.ev-progress { color:#7aa2f7; }
    .act .ico.ev-tool_call { color:#73daca; }
    .act .ico.ev-tool_result { color:#e0af68; }
    .act .ico.ev-browser { color:#bb9af7; }
    .act .ico.ev-error { color:#f7768e; }
    .act .ico.ev-done { color:#9ece6a; }
    .act .msg { word-break:break-word; }
    .act .meta { color:#6b7089; font-size:11px; margin-left:6px; }
    .shot-thumb { display:block; margin-top:6px; width:240px; border:2px solid #333; border-radius:4px;
                  cursor:pointer; transition:transform .15s; }
    .shot-thumb:hover { transform:scale(1.02); border-color:#1890ff; }
    /* 思考区 */
    .thought { border:1px solid #e4e7ed; border-radius:8px; margin-bottom:8px; overflow:hidden; background:#fff; }
    .thought-head { display:flex; align-items:center; gap:8px; padding:8px 12px; cursor:pointer;
                    user-select:none; background:#fafbfc; }
    .thought-head:hover { background:#f0f5ff; }
    .thought-kind { font-size:10.5px; padding:1px 7px; border-radius:10px; color:#fff; flex:none; }
    .kind-text { background:#1890ff; }
    .kind-json { background:#13c2c2; }
    .kind-html { background:#8c8c8c; }
    .kind-reasoning { background:#722ed1; }
    .thought-label { flex:1; font-size:12.5px; color:#303133; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .thought-status { font-size:11px; color:#bbb; flex:none; }
    .thought-arrow { color:#bbb; font-size:11px; flex:none; transition:transform .2s; }
    .thought.open .thought-arrow { transform:rotate(90deg); }
    .thought-body { display:none; border-top:1px solid #f0f0f0; background:#fbfcfe;
                    max-height:320px; overflow:auto; padding:10px 14px; }
    .thought.open .thought-body { display:block; }
    .thought-body pre { margin:0; white-space:pre-wrap; word-break:break-word;
                        font-family:Menlo,Monaco,Consolas,monospace; font-size:12px; line-height:1.65; color:#475569; }
    .thought-body.kind-reasoning pre { color:#722ed1; font-style:italic; }
    .caret { display:inline-block; width:7px; height:14px; background:#1890ff; vertical-align:-2px;
             animation:blink 1s steps(1) infinite; margin-left:1px; }
    @keyframes blink { 50% { opacity:0; } }
  </style>
</head>
<body>
  <div id="app" class="box">
    <el-card>
      <template #header>
        <strong>系统调研 → 产品原型（AI 智能体）</strong>
        <span class="tip" style="margin-left:8px;">提供现有系统地址让 Agent 自动浏览识别功能，再补充你的想法，一键生成可点击原型</span>
      </template>

      <!-- 来源 1：现有系统网址 -->
      <div class="source-row"><span class="source-num">1</span>
        <span class="source-title">现有系统地址</span>
        <span class="source-optional">（推荐填写，Agent 将自动登录并遍历菜单识别功能）</span>
      </div>
      <el-input v-model="form.siteUrl" placeholder="例如 https://crm.example.com/admin" clearable></el-input>
      <div class="acct-row">
        <el-input v-model="form.siteUsername" placeholder="登录账号（选填）" clearable></el-input>
        <el-input v-model="form.sitePassword" placeholder="登录密码（选填，仅用于本次自动登录，不会被保存）"
                  show-password clearable></el-input>
      </div>
      <div class="tip" style="margin:6px 0 18px 30px;">
        若系统有图形验证码/短信验证码/SSO，建议使用免验证码的测试账号；登录失败时 Agent 会继续采集登录后可见页面。
      </div>

      <!-- 来源 2：需求文档 -->
      <div class="source-row" style="margin-top:8px;"><span class="source-num">2</span>
        <span class="source-title">需求文档</span>
        <span class="source-optional">（选填，支持 .docx / .zip，zip 内可含多个 docx/txt/md）</span>
      </div>
      <el-upload drag :auto-upload="false" :limit="1" accept=".docx,.zip" :on-change="onChange"
                 style="margin:0 0 18px 30px;">
        <el-icon class="el-icon--upload"><upload-filled /></el-icon>
        <div class="el-upload__text">将 docx 或 zip 拖拽到此处，或<em>点击上传</em></div>
      </el-upload>

      <!-- 来源 3：补充想法 -->
      <div class="source-row"><span class="source-num">3</span>
        <span class="source-title">你的想法 / 下一步需求</span>
        <span class="source-optional">（选填，描述希望新增、改进、调整的点）</span>
      </div>
      <el-input v-model="form.extra" type="textarea" :rows="3"
                placeholder="例如：希望在销售中心增加一个『AI 跟进建议』入口；客户详情页太乱，希望重新规划信息层级；审批流程要支持加签转办…"
                style="margin-left:30px; max-width:calc(100% - 30px);"></el-input>

      <div style="margin-top:18px;">
        <el-button type="primary" size="large" :loading="loading" @click="onGenerate">
          {{ loading ? '生成中…' : '开始生成原型' }}
        </el-button>
        <el-button v-if="loading" @click="onStop">中断</el-button>
        <el-button v-if="hasRun && !loading" @click="onReset">重新生成</el-button>
        <span class="tip" style="margin-left:10px;">
          三种来源至少提供一种；站点浏览+生成约需 3-6 分钟，过程可实时查看
        </span>
      </div>
    </el-card>

    <!-- 实时过程面板 -->
    <el-card v-if="hasRun" class="run-card" body-style="padding:16px;">
      <el-steps :active="activeStep" align-center :process-status="errorStep ? 'error' : 'process'"
                finish-status="success" style="margin-bottom:4px;">
        <el-step title="采集信息"></el-step>
        <el-step title="功能分析与规划"></el-step>
        <el-step title="逐页生成"></el-step>
        <el-step title="组装原型"></el-step>
      </el-steps>

      <div class="phase-hero">
        <div class="label"><span class="pulse" v-if="loading"></span>{{ loading ? '正在执行' : (errorStep ? '执行中断' : '执行完成') }}</div>
        <div class="text">{{ phaseText || '准备中…' }}</div>
        <div class="sub" v-if="phaseSub">{{ phaseSub }}</div>
      </div>

      <div class="panel-section-title">🤖 模型实时输出</div>
      <div>
        <div v-for="t in thoughtList" :key="t.id" class="thought" :class="{open: t.open}">
          <div class="thought-head" @click="t.open = !t.open">
            <span class="thought-arrow">▶</span>
            <span class="thought-kind" :class="'kind-' + t.kind">{{ kindLabel(t.kind) }}</span>
            <span class="thought-label">{{ t.label }}</span>
            <span class="thought-status">{{ t.done ? '已完成' : '输出中…' }}</span>
          </div>
          <div class="thought-body" :class="'kind-' + t.kind">
            <pre>{{ t.text }}<span v-if="!t.done" class="caret"></span></pre>
          </div>
        </div>
        <div v-if="!thoughtList.length" class="tip">等待模型开始输出…</div>
      </div>

      <div class="panel-section-title">📋 执行流水
        <el-button link size="small" @click="toggleThoughts" style="margin-left:auto;">
          {{ allThoughtsOpen ? '全部折叠' : '全部展开' }}
        </el-button>
      </div>
      <div ref="streamBox" class="stream-box">
        <div v-for="(a, i) in activities" :key="i" class="act">
          <span class="ico" :class="'ev-' + a.kind">{{ actIcon(a.kind) }}</span>
          <span class="msg">
            {{ a.msg }}
            <a v-if="a.shot" :href="a.shot" target="_blank"><img :src="a.shot" class="shot-thumb"></a>
          </span>
        </div>
        <div v-if="!activities.length" style="color:#6b7089;">等待开始…</div>
      </div>
    </el-card>

    <el-alert v-if="result" title="生成成功" type="success" show-icon style="margin-top:16px;"
      :closable="false">
      <template #default>
        <a :href="result.preview_url" target="_blank" style="font-size:15px; font-weight:600;">
          点击预览生成的原型 →
        </a>
        <div class="tip">会话 ID：{{ result.session_id }}</div>
      </template>
    </el-alert>
    <el-alert v-if="errorMsg" :title="errorMsg" type="error" show-icon :closable="false"
              style="margin-top:16px;" />
  </div>

  <script>
    const { createApp, ref, reactive, computed, nextTick } = Vue;
    const app = createApp({
      setup() {
        const form = reactive({ siteUrl:'', siteUsername:'', sitePassword:'', extra:'' });
        const file = ref(null);
        const loading = ref(false);
        const hasRun = ref(false);
        const result = ref(null);
        const errorMsg = ref('');
        const errorStep = ref(false);

        // 数据态：单调事件时间线
        const activities = ref([]);   // 执行流水
        const thoughts = ref({});     // id -> {id,label,kind,text,done,open}
        const thoughtOrder = ref([]); // 保持插入顺序
        const phaseText = ref('');
        const phaseSub = ref('');
        const activeStep = ref(0);
        const allThoughtsOpen = ref(false);
        const streamBox = ref(null);
        let controller = null;

        const thoughtList = computed(() => thoughtOrder.value.map(id => thoughts.value[id]));

        const kindLabel = (k) => ({
          text:'分析', json:'规划', html:'页面HTML', reasoning:'推理'
        }[k] || '输出');

        const actIcon = (k) => ({
          progress:'进度', tool_call:'调工具', tool_result:'返回',
          browser:'浏览器', error:'错误', done:'完成'
        }[k] || k);

        const scrollStream = () => {
          nextTick(() => {
            if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight;
          });
        };

        const pushActivity = (kind, msg, extra) => {
          activities.value.push(Object.assign({ kind, msg, t: Date.now() }, extra || {}));
          scrollStream();
        };

        const setStep = (n) => {
          if (n >= 1 && n <= 4) activeStep.value = n - 1;
        };

        const toggleThoughts = () => {
          allThoughtsOpen.value = !allThoughtsOpen.value;
          thoughtOrder.value.forEach(id => { thoughts.value[id].open = allThoughtsOpen.value; });
        };

        // 数据侧事件 → 状态更新（单向推进，UI 只读渲染）
        const handleEvent = (ev) => {
          switch (ev.type) {
            case 'phase': {
              if (ev.label) phaseText.value = ev.label;
              if (ev.step) setStep(ev.step);
              if (ev.index && ev.total) {
                phaseSub.value = `进度：${ev.index} / ${ev.total} 个页面`;
              } else {
                phaseSub.value = '';
              }
              pushActivity('progress', '【阶段】' + ev.label);
              break;
            }
            case 'progress':
              pushActivity('progress', ev.msg || '');
              break;
            case 'browser': {
              const iconMap = { start:'打开站点', login:'登录', login_failed:'登录异常',
                                page:'采集页面', nav:'发现菜单', skip:'跳过', done:'浏览完成' };
              pushActivity('browser', (iconMap[ev.action] || '浏览器') + '：' + (ev.msg || ''),
                           { shot: ev.shot || '' });
              break;
            }
            case 'tool_call':
              pushActivity('tool_call', `调用工具 ${ev.tool}` + (ev.input ? `｜入参：${ev.input}` : ''));
              break;
            case 'tool_result':
              pushActivity('tool_result', '工具返回：' + (ev.output || ''));
              break;
            case 'thought_start': {
              if (!thoughts.value[ev.id]) {
                // html 段默认折叠，其余默认展开
                thoughts.value[ev.id] = {
                  id: ev.id, label: ev.label || '模型输出', kind: ev.kind || 'text',
                  text: '', done: false, open: ev.kind !== 'html',
                };
                thoughtOrder.value.push(ev.id);
              }
              break;
            }
            case 'thought_delta': {
              const t = thoughts.value[ev.id];
              if (t) {
                t.text += ev.delta || '';
                // 限制单段最大渲染字符，源数据体验保护（超长仅折叠显示尾部）
                if (t.text.length > 60000) t.text = t.text.slice(-60000);
              }
              break;
            }
            case 'thought_end': {
              const t = thoughts.value[ev.id];
              if (t) t.done = true;
              break;
            }
            case 'error':
              errorMsg.value = ev.msg || '未知错误';
              errorStep.value = true;
              pushActivity('error', ev.msg || '');
              break;
            case 'done':
              result.value = {
                session_id: ev.session_id,
                entry: ev.entry,
                preview_url: ev.preview_url,
              };
              activeStep.value = 4;
              phaseText.value = '全部完成，原型已就绪 🎉';
              phaseSub.value = '';
              pushActivity('done', '预览地址：' + ev.preview_url);
              break;
          }
        };

        const onChange = (uploadFile) => {
          file.value = uploadFile.raw;
          errorMsg.value = '';
        };

        const onStop = () => {
          if (controller) { try { controller.abort(); } catch(e) {} controller = null; }
          loading.value = false;
          errorStep.value = true;
          phaseText.value = '已中断';
          pushActivity('error', '用户主动取消生成');
        };

        const onReset = () => {
          hasRun.value = false;
          result.value = null;
          errorMsg.value = '';
          errorStep.value = false;
          activities.value = [];
          thoughts.value = {};
          thoughtOrder.value = [];
          activeStep.value = 0;
          phaseText.value = '';
          phaseSub.value = '';
        };

        const onGenerate = async () => {
          if (!file.value && !form.siteUrl.trim() && !form.extra.trim()) {
            ElementPlus.ElMessage.warning('请至少提供：系统网址 / 需求文档 / 补充想法 中的一项');
            return;
          }
          onReset();
          loading.value = true;
          hasRun.value = true;
          controller = new AbortController();

          try {
            const fd = new FormData();
            if (file.value) fd.append('file', file.value);
            fd.append('site_url', form.siteUrl.trim());
            fd.append('site_username', form.siteUsername.trim());
            fd.append('site_password', form.sitePassword);
            fd.append('extra_requirements', form.extra.trim());

            const resp = await fetch('/generate', { method:'POST', body:fd, signal:controller.signal });
            if (!resp.ok) {
              const txt = await resp.text();
              let msg = '请求失败：' + resp.status;
              try { const j = JSON.parse(txt); if (j.msg) msg = j.msg; } catch(e) { msg += ' ' + txt.slice(0,200); }
              errorMsg.value = msg;
              errorStep.value = true;
              return;
            }
            const reader = resp.body.getReader();
            const dec = new TextDecoder('utf-8');
            let buffer = '';
            while (true) {
              const { value, done } = await reader.read();
              if (done) break;
              buffer += dec.decode(value, { stream:true });
              let idx;
              while ((idx = buffer.indexOf('\n\n')) >= 0) {
                const chunk = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                for (const ln of chunk.split('\n')) {
                  if (!ln.startsWith('data:')) continue;
                  const payload = ln.slice(5).trim();
                  if (!payload) continue;
                  try { handleEvent(JSON.parse(payload)); } catch(e) {}
                }
              }
            }
          } catch (e) {
            if (e && e.name === 'AbortError') {
              pushActivity('error', '请求已取消');
            } else {
              errorMsg.value = String(e);
              errorStep.value = true;
            }
          } finally {
            loading.value = false;
            controller = null;
          }
        };

        return {
          form, loading, hasRun, result, errorMsg, errorStep,
          activities, thoughtList, activeStep, phaseText, phaseSub,
          allThoughtsOpen, streamBox,
          kindLabel, actIcon, toggleThoughts,
          onChange, onGenerate, onStop, onReset,
        };
      }
    });
    // 全局注册 Element Plus 图标（上传区 UploadFilled 等）
    if (window.ElementPlusIconsVue) {
      Object.entries(ElementPlusIconsVue).forEach(([k, v]) => app.component(k, v));
    }
    app.use(ElementPlus).mount('#app');
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_PAGE


@app.get("/crm", response_class=HTMLResponse)
async def crm_prototype():
    # CRM 二期-企业知识库 可点击原型（static/crm-kb/index.html）
    return FileResponse(str(BASE_DIR / "static" / "crm-kb" / "index.html"))


@app.post("/generate")
async def generate(
    file: Optional[UploadFile] = File(None),
    site_url: str = Form(""),
    site_username: str = Form(""),
    site_password: str = Form(""),
    extra_requirements: str = Form(""),
):
    # ---------- 1. 入参校验：三种来源至少一种 ----------
    has_file = bool(file and file.filename)
    doc_parser = None
    doc_kind = ""
    if has_file:
        ext = file.filename.lower()
        if ext.endswith(".docx"):
            doc_parser, doc_kind = parse_docx, "docx"
        elif ext.endswith(".zip"):
            doc_parser, doc_kind = parse_zip, "zip"
        else:
            raise HTTPException(400, "仅支持 .docx / .zip 文件")
    if not (has_file or site_url.strip() or extra_requirements.strip()):
        return JSONResponse(
            {"code": 400, "msg": "请至少提供：系统网址 / 需求文档 / 补充想法 中的一项",
             "data": None}
        )

    # ---------- 2. 解析需求文档（若有） ----------
    doc_text = ""
    if has_file:
        # 只取文件名，避免上传名中带路径造成目录穿越
        safe_name = Path(file.filename).name
        file_path = UPLOAD_DIR / safe_name
        with open(file_path, "wb") as f:
            f.write(await file.read())
        try:
            parsed = doc_parser(file_path)
        except Exception as e:
            return JSONResponse(
                {"code": 500, "msg": f"{doc_kind} 解析失败：{e}", "data": None}
            )
        if parsed.char_count < 100 and not (site_url.strip() or extra_requirements.strip()):
            return JSONResponse(
                {"code": 400, "msg": "文档内容过短，无法识别需求", "data": None}
            )
        doc_text = parsed.text

    # ---------- 3. SSE：后台线程跑 pipeline，主协程消费事件 ----------
    event_q: "queue.Queue[dict | None]" = queue.Queue()

    def _runner():
        def on_event(ev: dict) -> None:
            event_q.put(ev)

        try:
            run_pipeline_streaming(
                on_event=on_event,
                doc_text=doc_text,
                site_url=site_url,
                site_username=site_username,
                site_password=site_password,
                extra_requirements=extra_requirements,
            )
        except Exception as e:
            event_q.put({"type": "error", "msg": str(e)})
        finally:
            event_q.put(None)

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()

    async def event_gen():
        while True:
            ev = await asyncio.to_thread(event_q.get)
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        worker.join(timeout=1.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "output_root": str(OUTPUT_ROOT)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("APP_PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
