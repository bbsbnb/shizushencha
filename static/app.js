const $ = s => document.querySelector(s);
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = {health:null,projects:[],rules:[],project:null,task:null,tab:'files',source:null,filter:'all',search:'',uploading:false};
const states = {queued:'等待处理',parsing:'解析中',ready:'已解析',failed:'处理失败',running:'审核中',waiting_config:'待配置模型',draft:'待人工复核',confirmed:'已复核'};
const roleNames = {main:'施工组织设计／方案',drawing:'PDF图纸',bill:'Excel清单',other:'其他依据'};
const qualityNames = {readable:'文字可读',needs_check:'需原页核对',unreadable:'无法可靠识别'};
const decisions = {pending:'待复核',accepted:'已采纳',rejected:'已驳回'};
const natureOptions = ['确认不符合','明确要求缺项','完整性不足','资料不足待核实','优化建议'];
const riskOptions = ['重大','较大','一般','待判定'];
const concludeOptions = ['修改后复核','资料不足暂不形成结论','重新编制后报审','未发现需整改问题'];
const actor = () => localStorage.getItem('reviewActor') || '';
const rememberActor = value => localStorage.setItem('reviewActor',value);
const options = (values,selected) => values.map(x=>`<option value="${esc(x)}" ${x===selected?'selected':''}>${esc(x)}</option>`).join('');
const fmt = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '—';
const size = n => n>1024*1024 ? (n/1024/1024).toFixed(1)+' MiB' : (n/1024).toFixed(1)+' KiB';
const pill = (text,type='') => `<span class="pill ${type}">${esc(text)}</span>`;
const statusPill = status => pill(states[status] || status,['failed'].includes(status)?'red':['waiting_config','draft'].includes(status)?'warn':status==='confirmed'||status==='ready'?'green':'');
const button = (label,action,extra='',cls='') => `<button class="btn ${cls}" data-action="${action}" ${extra}>${label}</button>`;
const empty = (title,description,action='') => `<div class="empty"><div class="empty-mark">▤</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className=error?'error':'';el.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.style.display='none',6500);}
async function api(url,options={}) {
  const headers={...(options.body && typeof options.body==='string'?{'Content-Type':'application/json'}:{}),...options.headers};
  const r=await fetch('/api'+url,{...options,headers});
  if(!r.ok){let d;try{d=await r.json();}catch{d={detail:`请求失败 ${r.status}`};}throw new Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));}
  return r.json();
}
const post = (url,data) => api(url,{method:'POST',body:JSON.stringify(data)});
function field(label,name,value='',type='text',full=false,required=false){return `<div class="field ${full?'full':''}"><label for="f-${name}">${esc(label)}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${required?'required':''}></div>`;}
function textfield(label,name,value='',rows=3){return `<div class="field full"><label for="f-${name}">${esc(label)}</label><textarea id="f-${name}" name="${name}" rows="${rows}">${esc(value)}</textarea></div>`;}
function selectfield(label,name,values,selected=''){return `<div class="field"><label for="f-${name}">${esc(label)}</label><select id="f-${name}" name="${name}">${options(values,selected)}</select></div>`;}
function modal(title,content,onSubmit=null,wide=false) {
  const dialog=$('#dialog');dialog.style.width=wide?'min(900px,calc(100vw - 35px))':'';
  $('#dialog-content').innerHTML=`<div class="modal-head"><h2>${esc(title)}</h2><button class="close" aria-label="关闭" data-action="close">×</button></div><div class="modal-body">${content}</div>`;
  if(!dialog.open)dialog.showModal();
  const form=dialog.querySelector('form');
  if(form && onSubmit)form.onsubmit=async e=>{e.preventDefault();const submit=form.querySelector('button[type=submit]');submit.disabled=true;
    try{await onSubmit(Object.fromEntries(new FormData(form)),form);dialog.close();}catch(error){toast(error.message,true);}finally{submit.disabled=false;}};
}
function modalFoot(label='保存'){return `<div class="modal-footer"><button type="button" class="btn" data-action="close">取消</button><button type="submit" class="btn primary">${label}</button></div>`;}
async function init(){S.health=await api('/health');S.rules=(await api('/rules')).items;updateHealth();await route();}
function updateHealth(){const h=S.health.model;$('#model-status').className='pill '+(h.configured?'green':'warn');$('#model-status').textContent=h.model+' · '+(h.configured?'密钥已配置':'待配置密钥');}
async function modelSettings(){
 const current=await api('/model/config');
 modal('大模型配置',`<form id="model-form"><div class="field"><label for="model-url">API 地址</label><input id="model-url" type="url" name="base_url" value="${esc(current.base_url)}" required placeholder="https://api.agnes-ai.cn/v1"></div>
 <div class="field"><label for="model-name">模型名称</label><input id="model-name" name="model" value="${esc(current.model)}" required></div>
 <div class="field"><label for="model-key">API 密钥</label><input id="model-key" type="password" name="api_key" autocomplete="new-password" placeholder="${current.configured?'已保存密钥；留空保留，输入新密钥可替换':'请输入 Agnes API Key'}"></div>
 <div class="notice">${current.configured?'已保存密钥，页面不回显。':'尚未配置密钥。'}配置保存在服务器，保存后生效，无需重启。测试连接只发送简短测试消息，不发送工程资料。</div>
 <div id="model-test-result" role="status" class="muted"></div><div class="modal-footer"><button type="button" class="btn" data-action="close">取消</button><button type="button" class="btn" id="model-test">测试连接</button><button type="submit" class="btn primary">保存配置</button></div></form>`,async data=>{
   if(!current.configured && !data.api_key.trim())throw new Error('首次配置请填写 API 密钥');
   S.health.model=await api('/model/config',{method:'PUT',body:JSON.stringify(data)});updateHealth();await route();toast('模型配置已保存，可返回审核任务重试');
 });
 const form=$('#model-form');
 $('#model-test').onclick=async()=>{
  const btn=$('#model-test'),result=$('#model-test-result');btn.disabled=true;result.textContent='正在测试连接…';result.className='muted';
  try{const r=await post('/model/test',Object.fromEntries(new FormData(form)));result.textContent=r.message;result.className='reading-details';}
  catch(error){result.textContent=error.message;result.className='error-text';}
  finally{btn.disabled=false;}
 };
}
function currentHash(){return location.hash.slice(1)||'overview';}
async function route(){
  const [view,id]=currentHash().split('/');
  document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===(['project','task'].includes(view)?'overview':view)));
  try {
    if(view==='project'){S.project=await api('/projects/'+id);renderProject();}
    else if(view==='task'){S.task=await api('/tasks/'+id);S.project=await api('/projects/'+S.task.project_id);renderTask();}
    else if(view==='standards'){await renderStandards();}
    else if(view==='rules'){renderRules();}
    else {S.projects=await api('/projects');renderOverview();}
  }catch(error){$('#app').innerHTML=empty('无法加载',error.message,button('返回总览','overview'));toast(error.message,true);}
}
function setPage(crumb,html){$('#breadcrumb').textContent='工作空间 / '+crumb;$('#app').innerHTML=html;}
function renderOverview(){
  const projects=S.projects;const draft=projects.filter(p=>p.latest?.state==='draft').length;const running=projects.filter(p=>['running','queued'].includes(p.latest?.state)).length;
  setPage('项目总览',`<div class="page-head"><div><div class="eyebrow">PROJECT WORKSPACE</div><h1>项目总览</h1><p>从工程资料到审核意见，让每一个问题都有据可循。</p></div>${button('＋ 新建项目','new-project','','primary')}</div>
  <div class="stats">${[['在审项目',projects.length,'全部共享项目','▦'],['处理中',running,'解析与审核任务以项目内记录为准','↻'],['待技术复核',draft,'项目最新审核初稿','◇'],['待整改问题',projects.reduce((n,p)=>n+p.open_issues,0),'历轮已采纳且未确认解决的问题','☷']].map(([label,n,note,icon])=>`<div class="stat"><span class="stat-icon">${icon}</span><div class="stat-label">${label}</div><div class="stat-number">${n.toString().padStart(2,'0')}</div><small>${note}</small></div>`).join('')}</div>
  ${!S.health.model.configured?`<div class="notice warn">模型待接入：已预设 Agnes / agnes-3.0-flash。<button class="text-button" data-action="model-settings">点击配置 API 密钥 →</button> 可先建立项目并解析资料。</div>`:''}
  <section class="card"><div class="card-head"><h2>全部工程项目 <span class="muted">${projects.length} 项</span></h2><input class="search" id="project-search" placeholder="搜索项目名称或地点" aria-label="搜索项目"></div><div id="project-table">${projectTable(projects)}</div></section>
  <div class="subgrid"><section class="card"><div class="card-head"><h2>审核工作流程</h2><span class="muted">以人工复核为准</span></div><div class="card-body"><div class="steps">${[['01','上传资料','技术负责人发起'],['02','智能审核','原文与规范对照'],['03','技术复核','采纳或修正意见'],['04','整改复审','保留历轮记录']].map(([n,t,s])=>`<div class="step"><span class="step-num">${n}</span><b>${t}</b>${s}</div>`).join('')}</div></div></section>
  <section class="card"><div class="card-head"><h2>当前审核范围</h2>${pill('房建优先')}</div><div class="card-body"><div style="font-size:13px;margin-bottom:10px">陕西省 · 施工组织设计与施工方案</div><div class="muted">八维度检查框架，重点检查漏项、规范适用、工程针对性与内容一致性。支持历史文件复核。</div><div style="margin-top:15px"><a class="text-button" href="#rules">查看 16 项基础检查规则 →</a></div></div></section></div>`);
  $('#project-search').oninput=e=>{$('#project-table').innerHTML=projectTable(projects.filter(p=>(p.name+p.location).includes(e.target.value)));};
}
function projectTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>工程项目</th><th>审核基准</th><th>资料</th><th>最新审核</th><th>待整改</th><th></th></tr></thead><tbody>${rows.map(p=>`<tr><td><a href="#project/${p.id}" class="project-name">${esc(p.name)}</a><span class="muted">${esc(p.location)}</span></td><td>${p.mode==='historical'?'历史复核':'当前报审'}<div class="file-meta">${esc(p.baseline_date)}</div></td><td>${p.files} 份</td><td>${p.latest?statusPill(p.latest.state):pill('尚未发起')}</td><td>${p.open_issues} 项</td><td><a class="table-action" href="#project/${p.id}">进入项目 →</a></td></tr>`).join('')}</tbody></table></div>`:empty('从第一个项目开始','建立项目后上传施工组织设计，确认审核基准和工程事实。',button('新建项目','new-project','','primary'));}
function newProject(){modal('建立审核项目',`<form><div class="form-grid">${field('工程名称','name','','text',true,true)}${field('工程地点','location','陕西省西安市')}
  <div class="field"><label for="f-mode">审核模式</label><select id="f-mode" name="mode"><option value="current">当前项目报审</option><option value="historical">历史文件复核</option></select></div>
  ${field('审核基准日期','baseline_date',new Date().toLocaleDateString('sv-SE'),'date',false,true)}${field('项目技术负责人','actor',actor(),'text',false,true)}${textfield('已知工程事实（可在发起审核时补充确认）','facts','',3)}</div>
  <div class="notice">历史文件应结合当时的规范实施条件审核。姓名为填写记录，无账号身份认证。</div>${modalFoot('建立项目')}</form>`,async data=>{const p=await post('/projects',data);rememberActor(data.actor);S.tab='files';location.hash='project/'+p.id;toast('项目已建立');});}
function renderProject(){
 const p=S.project;const files=p.files;
 setPage('项目资料',`<div class="page-head"><div><div class="eyebrow">PROJECT / ${p.id.slice(0,8)}</div><h1>${esc(p.name)}</h1><div class="detail-meta"><span>${esc(p.location)}</span><span>${p.mode==='historical'?'历史复核':'当前报审'} · ${esc(p.baseline_date)}</span><span>项目技术负责人：${esc(p.actor)}</span></div></div><div class="button-row"><a href="#overview" class="btn">返回总览</a>${button('发起审核','launch','','primary')}</div></div>
 <div class="tabs">${[['files','项目资料'],['tasks','审核与复审'],['events','操作记录']].map(([id,label])=>`<button class="tab ${S.tab===id?'active':''}" data-action="tab" data-tab="${id}">${label}${id==='files'?' · '+files.length:''}</button>`).join('')}</div>
 ${S.tab==='files'?renderFiles(p):S.tab==='tasks'?renderTasks(p):renderEvents(p)}`);
 if(S.tab==='files'){
  $('#upload-file').onchange=e=>{if(e.target.files[0])uploadFile(e.target.files[0]);};
  const zone=$('#drop-zone');zone.ondragover=e=>{e.preventDefault();};zone.ondrop=e=>{e.preventDefault();if(e.dataTransfer.files[0])uploadFile(e.dataTransfer.files[0]);};
 }
}
function renderFiles(p){return `<section class="card"><div class="card-head"><h2>上传工程资料</h2><span class="muted">最大 300 MiB / 文件</span></div><div class="card-body">
 <div class="upload-options"><label>资料类型<select id="upload-role">${Object.entries(roleNames).map(([id,label])=>`<option value="${id}">${label}</option>`).join('')}</select></label><label>文件版本<input id="upload-version" value="V${p.files.filter(f=>f.role==='main').length+1}" maxlength="80"></label><label>上传人<input id="upload-actor" value="${esc(actor()||p.actor)}" maxlength="60"></label></div>
 <div class="drop-zone" id="drop-zone" data-action="choose-file"><div class="upload-symbol">⇧</div><div><h3>点击选择文件，或将文件拖放至此</h3><p>PDF · DOCX · DOC · XLSX · XLS　分块上传，可选择同一文件恢复未完成上传</p></div><input type="file" id="upload-file" hidden accept=".pdf,.docx,.doc,.xlsx,.xls"></div><div id="upload-progress" class="upload-progress"></div></div></section>
 <section class="card"><div class="card-head"><h2>项目文件 <span class="muted">${p.files.length} 份</span></h2><span class="muted">每次上传保留独立版本</span></div>${p.files.length?`<div class="table-wrap"><table><thead><tr><th>文件名称</th><th>类型 / 版本</th><th>解析覆盖</th><th>状态</th><th>操作</th></tr></thead><tbody>${p.files.map(f=>`<tr><td><div class="file-name">${esc(f.name)}</div><div class="file-meta">${size(f.size)} · ${fmt(f.created)}</div></td><td>${esc(roleNames[f.role])}<div class="file-meta">${esc(f.version)}</div></td><td>${f.done} / ${f.page_count||'—'} 单元<div class="file-meta">${(f.coverage||[]).map(c=>`${qualityNames[c.quality]} ${c.n}`).join(' · ')}</div></td><td>${statusPill(f.status)}${f.error?`<div class="error-text">${esc(f.error)}</div>`:''}</td><td><div class="button-row">${f.done?button('查看原文','preview-file',`data-file="${f.id}"`,'small'):''}<a class="text-button" href="/api/files/${f.id}/download">下载</a>${f.status==='failed'?button('重试','retry-file',`data-file="${f.id}"`,'small'):''}${button('删除','delete-file',`data-file="${f.id}"`,'small danger')}</div></td></tr>`).join('')}</tbody></table></div>`:empty('尚未上传资料','先上传施工组织设计。图纸和清单可随后加入。')}</section>`;}
async function uploadFile(file){
 if(S.uploading)return;
 const who=$('#upload-actor').value.trim(),version=$('#upload-version').value.trim(),role=$('#upload-role').value;
 if(!who||!version){toast('请填写上传人和版本',true);return;}
 if(file.size>300*1024*1024){toast('文件超过300 MiB，请分册上传',true);return;}
 S.uploading=true;rememberActor(who);const progress=$('#upload-progress');
 try {
  const session=await post('/uploads',{project_id:S.project.id,name:file.name,role,version,size:file.size});let offset=session.offset;
  while(offset<file.size){
   const chunk=file.slice(offset,Math.min(offset+5*1024*1024,file.size));
   const r=await api(`/uploads/${session.id}?offset=${offset}`,{method:'PUT',body:chunk,headers:{'Content-Type':'application/octet-stream'}});offset=r.offset;
   progress.innerHTML=`正在上传 ${esc(file.name)} · ${Math.round(100*offset/file.size)}%<div class="progress"><span style="width:${100*offset/file.size}%"></span></div>`;
  }
  await post(`/uploads/${session.id}/complete`,{actor:who});toast('资料上传完成，后台开始解析');await route();
 }catch(error){toast(error.message,true);if(progress)progress.textContent='上传未完成，可重新选择同一文件续传。';}finally{S.uploading=false;}
}
function renderTasks(p){return `<section class="card"><div class="card-head"><h2>审核历史</h2>${button('新建审核 / 复审','launch','','primary small')}</div>${p.tasks.length?p.tasks.map(t=>`<div class="task-row"><div><div class="task-label">${t.baseline_id?'整改复审':'初次审核'} <small class="muted">${t.id.slice(0,8)}</small></div><div class="file-meta">${fmt(t.created)} · ${esc(t.actor)} · ${esc(t.stage)}</div>${t.error?`<div class="error-text">${esc(t.error)}</div>`:''}</div><div class="button-row">${statusPill(t.state)}<a href="#task/${t.id}" class="btn small">查看结果 →</a></div></div>`).join(''):empty('尚未发起审核','完成资料解析后，确认工程事实并启动审核。')}</section>`;}
function renderEvents(p){return `<section class="card"><div class="card-head"><h2>项目操作记录</h2><span class="muted">姓名为填写信息</span></div><div class="card-body activity">${p.events.map(e=>`<div class="activity-item">${esc(e.actor)} · ${esc(e.action)}<small>${fmt(e.created)}</small><details><summary>查看记录内容</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify(JSON.parse(e.detail),null,2))}</pre></details></div>`).join('')||'暂无记录'}</div></section>`;}
function launch(){
 const p=S.project;const ready=p.files.filter(f=>f.status==='ready');const main=ready.filter(f=>f.role==='main');
 if(!main.length){toast('请先上传并完成施工组织设计解析',true);return;}
 modal('发起审核或整改复审',`<form><div class="field"><label>主文件</label><select name="main_file">${main.map(f=>`<option value="${f.id}">${esc(f.version+' · '+f.name)}</option>`).join('')}</select></div>
 <div class="field"><label>本轮其他依据资料</label>${ready.filter(f=>f.role!=='main').map(f=>`<label><input type="checkbox" name="support" value="${f.id}"> ${esc(f.version+' · '+f.name)}</label>`).join('')||'<span class="muted">当前无图纸、清单等依据，将仅审核主文件。</span>'}</div>
 <div class="field"><label>整改复审基线</label><select name="baseline_id"><option value="">不关联基线，建立新审核</option>${p.tasks.filter(t=>t.state==='confirmed').map(t=>`<option value="${t.id}">${esc(t.id.slice(0,8)+' · '+fmt(t.created))}</option>`).join('')}</select></div>
 ${textfield('确认工程事实及本轮整改说明','facts',p.facts,4)}${field('项目技术负责人','actor',actor()||p.actor,'text',true,true)}
 <div class="notice warn">审核基准：${esc(p.baseline_date)}。逐页AI审核将调用 Agnes 服务，调用量取决于文件长度。尚未核验的规范不用于确定性合规判断。</div>
 <label style="font-size:12px"><input type="checkbox" required> 我已核对工程事实、资料版本和审核基准，同意开始本轮审核。</label>${modalFoot('开始审核')}</form>`,async(data,form)=>{
   const file_ids=[...form.querySelectorAll('[name=support]:checked')].map(x=>x.value);
   const t=await post('/projects/'+p.id+'/tasks',{...data,file_ids});rememberActor(data.actor);S.source=null;location.hash='task/'+t.id;
 });
}
function renderTask(){
 const t=S.task,p=S.project,result=t.result;const confirmed=t.state==='confirmed';
 setPage('审核工作台',`<div class="page-head"><div><div class="eyebrow">REVIEW / ${t.id.slice(0,8)}</div><h1>${t.baseline_id?'整改复审工作台':'审核工作台'}</h1><p>${esc(p.name)} · ${esc(t.snapshot.project.baseline_date)} ${t.snapshot.project.mode==='historical'?'历史基准':'审核基准'}</p></div><div class="button-row"><a class="btn" href="#project/${p.id}">返回项目</a><a class="btn" href="/api/tasks/${t.id}/report.docx">导出${confirmed?'报告':'初稿'}</a>${t.state==='draft'?button('确认内部报告','confirm','','primary'):''}</div></div>
 <section class="card"><div class="card-body"><div class="button-row" style="justify-content:space-between"><div>${statusPill(t.state)} <span class="muted">${esc(t.stage)} · 发起人 ${esc(t.actor)}</span></div>${['failed','waiting_config'].includes(t.state)?button('重试审核','retry-task','','small'):''}</div>
 ${t.total?`<div class="progress"><span style="width:${100*t.done/t.total}%"></span></div><div class="file-meta">已完成 ${t.done} / ${t.total} 批次</div>`:''}
 ${t.error?`<div class="error-text">${esc(t.error)}</div>`:''}${result.summary?`<p style="font-size:13px">${esc(result.summary)}</p>`:''}
 ${t.state==='waiting_config'?'<div class="notice warn" style="margin:16px 0 0">尚未执行 AI 审核。请先 <button class="text-button" data-action="model-settings">配置模型密钥</button>，保存后点击重试审核。</div>':''}
 ${t.snapshot.standards.length===0?'<div class="reading-details">本轮未配置适用的已核验规范，规范合规结论受限。补充规范后请新建审核任务。</div>':''}
 ${confirmed?`<div class="notice" style="margin-bottom:0">${esc(t.conclusion)} · ${esc(t.reviewer)} · ${fmt(t.confirmed)}<br>${esc(t.review_note)}</div>`:''}
 ${(result.limitations||[]).length?`<details><summary>查看审核限制与未完成事项</summary>${result.limitations.map(x=>`<p>${esc(x)}</p>`).join('')}</details>`:''}
 </div></section>
 ${result.checks?`<section class="card"><details><summary style="padding:16px 22px">16项检查结果与覆盖说明</summary><table class="checks"><thead><tr><th>检查项</th><th>结果</th><th>理由</th></tr></thead><tbody>${result.checks.map(c=>`<tr><td>${esc(c.rule_id+' '+(S.rules.find(r=>r.id===c.rule_id)?.name||''))}</td><td>${pill(c.status,c.status==='待核实'?'warn':'')}</td><td>${esc(c.reason)}</td></tr>`).join('')}</tbody></table></details></section>`:''}
 ${t.rechecks.length?renderRechecks(t):''}
 <div class="workbench"><section class="card source-panel"><div class="card-head"><h2>原文对照</h2><span class="muted">点击问题证据可定位</span></div><div id="source-pane"></div></section>
 <div><div class="filters"><select id="issue-filter" aria-label="筛选问题"><option value="all">全部问题 (${t.issues.length})</option>${Object.entries(decisions).map(([key,val])=>`<option value="${key}" ${key===S.filter?'selected':''}>${val}</option>`).join('')}</select>${button('人工补充问题','manual-issue',t.state!=='draft'?'disabled':'','small')}<span class="muted">${t.issues.filter(i=>i.decision==='accepted').length} 已采纳</span></div><div id="issues" class="issue-list">${renderIssues()}</div></div></div>`);
 $('#issue-filter').value=S.filter;$('#issue-filter').onchange=e=>{S.filter=e.target.value;$('#issues').innerHTML=renderIssues();};
 const fileId=S.source?.file_id && t.file_ids.includes(S.source.file_id)?S.source.file_id:t.main_file;
 const number=S.source?.file_id===fileId?S.source.page:1;
 loadSource(fileId,number);
}
function renderIssues(){const t=S.task;const rows=t.issues.filter(i=>S.filter==='all'||i.decision===S.filter);return rows.length?rows.map(i=>`<article class="issue-card"><div class="issue-top"><span class="issue-code">${esc(i.code)} / ${esc(i.rule_id)}</span>${pill(i.nature,i.nature==='资料不足待核实'?'warn':'')}${pill(i.risk,['重大','较大'].includes(i.risk)?'red':'')}${pill(decisions[i.decision],i.decision==='accepted'?'green':'')}</div><h3>${esc(i.title)}</h3><p>${esc(i.description)}</p>${i.evidence.map(e=>evidence(e)).join('')}${i.citations.length?`<details><summary>规范依据 (${i.citations.length})</summary>${i.citations.map(c=>`<p>${esc(c.name+' '+c.code+' '+c.version+' '+c.clause)}</p><p>${esc(c.quote)}</p><div class="muted">来源：${esc(c.source)}</div>`).join('')}</details>`:'<div class="checkpoint">尚无可核验的规范条文引用</div>'}<p><b>整改建议</b>　${esc(i.recommendation)}</p>${i.review_note?`<p>复核说明：${esc(i.review_note)}</p>`:''}<div class="issue-bottom"><span class="muted">${esc(i.owner||'责任人待填写')} ${esc(i.deadline)}</span>${t.state==='draft'?button('编辑与复核','edit-issue',`data-id="${i.id}"`,'small'):pill('已留存快照')}</div></article>`).join(''):empty('暂无问题记录',t.state==='draft'||t.state==='confirmed'?'未列出问题不等同于全部符合，请结合检查覆盖和限制判断。':'审核完成后，问题将在这里展示。');}
function evidence(e){return `<div class="quote" data-action="locate" data-file="${esc(e.file_id)}" data-page="${e.page}"><b>${esc(e.label||'来源第'+e.page+'单元')}</b> ↗<br>${esc(e.quote)}</div>`;}
async function loadSource(fileId,page){
 const pane=($('#dialog').open && $('#dialog').querySelector('#source-pane')) || $('#app').querySelector('#source-pane');if(!pane)return;
 S.source={file_id:fileId,page:Math.max(1,Number(page)||1)};
 try{
  const row=await api(`/files/${fileId}/pages/${S.source.page}`);const t=S.task;
  const available=[...S.project.files,...(S.project.archived_files||[])];
  const ids=$('#dialog').open?[...new Set([fileId,...S.project.files.map(f=>f.id)])]:t?.file_ids||S.project.files.map(f=>f.id);
  const files=ids.map(id=>available.find(f=>f.id===id)).filter(Boolean);
  const file=files.find(f=>f.id===fileId);const hasImage=file?.name.toLowerCase().match(/\.(pdf|docx|doc)$/);
  pane.innerHTML=`<div class="source-toolbar"><select id="source-file" aria-label="选择原文文件">${files.map(f=>`<option value="${f.id}" ${f.id===fileId?'selected':''}>${esc(f.version+' '+f.name)}</option>`).join('')}</select><button class="btn small" data-action="prev-page" ${row.number<=1?'disabled':''}>←</button><input type="number" id="source-page" min="1" max="${file?.page_count||1}" value="${row.number}" aria-label="原文页序"><span class="muted">/ ${file?.page_count||'—'}</span><button class="btn small" data-action="next-page" ${row.number>=file?.page_count?'disabled':''}>→</button></div><div class="source-label">${esc(row.label)} · ${esc(qualityNames[row.quality])} ${esc(row.note)}</div><div class="source-view">${hasImage?`<img src="/api/files/${fileId}/pages/${row.number}/image" alt="${esc(row.label)}原图" id="source-image">`:''}<details ${!hasImage?'open':''}><summary>提取文字（可复制原文）</summary><pre>${esc(row.text||'本页未提取到可靠文字，请查看原图。')}</pre></details></div>`;
  const img=pane.querySelector('#source-image');if(img)img.onerror=()=>{img.remove();pane.querySelector('details').open=true;};
  pane.querySelector('#source-file').onchange=e=>loadSource(e.target.value,1);pane.querySelector('#source-page').onchange=e=>loadSource(fileId,Math.min(file.page_count,Math.max(1,+e.target.value)));
 }catch(error){pane.innerHTML=empty('原文尚不可用',error.message);}
}
function renderRechecks(t){return `<section class="card"><div class="card-head"><h2>整改复审记录</h2><span class="muted">人工确认完成率：${t.rectification_rate===null?'无纳入整改问题':t.rectification_rate+'%'}</span></div><div class="card-body">${t.rechecks.map(r=>`<div class="issue-card" style="margin-bottom:12px"><div class="issue-top">${pill(r.code)}${pill('AI建议：'+r.suggestion,'warn')}${pill('人工：'+(r.confirmed||'待确认'),r.confirmed==='已解决'?'green':'')}</div><h3>${esc(r.title)}</h3><p>${esc(r.reason)}</p><details><summary>对照整改前后证据</summary><b class="muted">整改前</b>${r.old_evidence.map(evidence).join('')}<b class="muted">整改后</b>${r.evidence.map(evidence).join('')}</details>${r.note?`<p>${esc(r.actor)}：${esc(r.note)}</p>`:''}${t.state==='draft'?button('确认复审结果','edit-recheck',`data-id="${r.id}"`,'small'):''}</div>`).join('')}</div></section>`;}
function editIssue(id){
 const i=S.task.issues.find(x=>x.id===id);
 modal(i.code+' 编辑与技术复核',`<form><div class="form-grid">${field('问题标题','title',i.title,'text',true,true)}${selectfield('问题性质','nature',natureOptions,i.nature)}${selectfield('风险程度','risk',riskOptions,i.risk)}${textfield('问题说明','description',i.description,4)}${textfield('整改建议','recommendation',i.recommendation)}
 <div class="field"><label>人工处置</label><select name="decision">${Object.entries(decisions).map(([key,label])=>`<option value="${key}" ${key===i.decision?'selected':''}>${label}</option>`).join('')}</select></div>${field('复核人','actor',actor(),'text',false,true)}${field('整改责任人','owner',i.owner)}${field('整改期限（人工确定）','deadline',i.deadline,'date')}${textfield('复核说明（驳回时必填）','review_note',i.review_note)}</div>${modalFoot('保存复核记录')}</form>`,async data=>{await api('/issues/'+id,{method:'PATCH',body:JSON.stringify(data)});rememberActor(data.actor);await route();toast('复核记录已保存');});
}
function manualIssue(){
 if(S.task.state!=='draft'){toast('待人工复核的初稿才能补充问题',true);return;}
 modal('人工补充审核问题',`<form><div class="form-grid">${field('问题标题','title','','text',true,true)}${selectfield('问题性质','nature',natureOptions,'完整性不足')}${selectfield('风险程度','risk',riskOptions,'待判定')}${field('规则编号（01至16）','rule_id','16')}${field('原文页序／单元号','page',S.source?.page||1,'number',false,true)}
 <input type="hidden" name="file_id" value="${esc(S.source?.file_id||S.task.main_file)}">${textfield('原文摘录（须与当前文件该页逐字一致）','quote','',3)}${textfield('问题说明','description')}${textfield('整改建议','recommendation')}${field('补充人','actor',actor(),'text',true,true)}</div><div class="notice">证据对应左侧当前资料。未提供核验条文的合规判断将记为待核实。</div>${modalFoot('补充问题')}</form>`,async data=>{await post('/tasks/'+S.task.id+'/issues',{...data,page:+data.page});rememberActor(data.actor);await route();});
}
function confirm(){modal('确认内部审核报告',`<form>${field('技术复核人员','actor',actor(),'text',true,true)}${selectfield('内部审核结论','conclusion',concludeOptions)}${textfield('复核意见及审核限制','note','',4)}<div class="notice warn">请先逐项处置问题并确认整改复审记录。确认后本轮报告不可修改；后续修改建立新轮次。姓名为填写记录。</div>${modalFoot('确认并留存报告')}</form>`,async data=>{await post('/tasks/'+S.task.id+'/confirm',data);rememberActor(data.actor);await route();toast('内部报告已确认');});}
function editRecheck(id){const r=S.task.rechecks.find(x=>x.id===id);modal('确认整改复审 '+r.code,`<form>${selectfield('人工确认状态','status',['已解决','部分解决','未解决','无法判断'],r.confirmed||r.suggestion)}${textfield('复核依据与说明','note',r.note||'',4)}${field('复审人','actor',actor(),'text',true,true)}${modalFoot('保存复审确认')}</form>`,async data=>{await api('/rechecks/'+id,{method:'PATCH',body:JSON.stringify(data)});rememberActor(data.actor);await route();});}
async function renderStandards(){
 const rows=await api('/standards');S.standards=rows;
 setPage('受控规范库',`<div class="page-head"><div><div class="eyebrow">CONTROLLED KNOWLEDGE</div><h1>受控规范库</h1><p>保留版本、来源和实施条件，让每一条依据可核验。</p></div>${button('＋ 录入规范条文','add-standard','','primary')}</div>
 <div class="notice">只有人工标记为已核验，且在任务基准日期适用的规范会进入确定性审核检索。旧任务保留原规范快照；新增规范后请新建任务。</div>
 <section class="card"><div class="card-head"><h2>规范资料 <span class="muted">${rows.length} 份</span></h2><span class="muted">国家 · 行业 · 陕西地方</span></div>${rows.length?`<div class="table-wrap"><table><thead><tr><th>规范名称 / 编号</th><th>版本</th><th>适用日期</th><th>核验状态</th><th></th></tr></thead><tbody>${rows.map(s=>`<tr><td><span class="project-name">${esc(s.name)}</span><span class="muted">${esc(s.code)} · ${esc(s.category)}</span></td><td>${esc(s.version)}</td><td>${esc(s.effective)}<div class="file-meta">${s.expires?'失效 '+esc(s.expires):'失效信息尚未填写'}</div></td><td>${pill(s.state==='verified'?'人工已核验':'待核验',s.state==='verified'?'green':'warn')}<div class="file-meta">${esc(s.actor)}</div></td><td>${button('查看来源','view-standard',`data-id="${s.id}"`,'small')} ${button('编辑 / 核验','edit-standard',`data-id="${s.id}"`,'small')}</td></tr>`).join('')}</tbody></table></div>`:empty('尚未录入核验规范','先录入有可靠来源的规范原文、版本和实施条件。系统不会自动编造条文。',button('录入第一份规范','add-standard','','primary'))}</section>`);
}
function addStandard(id=''){const current=S.standards?.find(s=>s.id===id);modal(id?'编辑与核验规范':'录入受控规范条文',`<form><div class="form-grid">${field('规范名称','name','','text',true,true)}${field('编号','code','','text',false,true)}${field('版本 / 年份','version','','text',false,true)}${selectfield('类别','category',['国家','行业','陕西地方','其他地方','企业'])}${field('实施日期','effective','','date',false,true)}${field('失效日期（未知留空）','expires','','date')}${field('来源网址或文件定位','source','','text',false,true)}${textfield('适用范围与过渡条件（请注明历史适用条件）','conditions')}${textfield('条文原文（含条文编号；可录入多个完整条文）','content','',7)}${field('核验 / 录入人员','actor',actor(),'text',false,true)}<div class="field"><label>核验状态</label><select name="state"><option value="pending">待核验，不用于确定性审核</option><option value="verified">已人工核验来源、版本和实施条件</option></select></div></div>${modalFoot('保存规范资料')}</form>`,async data=>{if(id)await api('/standards/'+id,{method:'PUT',body:JSON.stringify(data)});else await post('/standards',data);rememberActor(data.actor);await renderStandards();toast('规范资料已保存；既有审核快照保持不变');},true);
 const form=$('#dialog form');
 if(current)for(const [key,value] of Object.entries(current)){const input=form.elements.namedItem(key);if(input)input.value=value;}
 for(const name of ['conditions','content'])form.elements.namedItem(name).required=true;
}
function viewStandard(id){const s=S.standards.find(x=>x.id===id);modal(s.name,`<div class="detail-meta"><span>${esc(s.code+' '+s.version)}</span><span>${esc(s.effective)} 实施</span></div><p class="muted">来源：${esc(s.source)}</p><p>${esc(s.conditions)}</p><div class="standard-content">${esc(s.content)}</div><div class="standard-state">${pill(s.state==='verified'?'已人工核验':'待核验')} <span class="muted">${esc(s.actor)} · ${fmt(s.created)}</span></div>`,null,true);}
function renderRules(){setPage('审核规则',`<div class="page-head"><div><div class="eyebrow">REVIEW RULES</div><h1>审核规则</h1><p>八维度基础清单 · 先判断适用性，再判断问题性质。</p></div>${pill('规则版本 '+S.health.model.rule_version)}</div><div class="notice">基础检查项不是规范条文。实际合规结论须引用受控规范库中的适用依据，资料不足标记待核实。</div><section class="card"><div class="table-wrap"><table><thead><tr><th>编号</th><th>维度</th><th>检查项</th><th>判断要求</th></tr></thead><tbody>${S.rules.map(r=>`<tr><td>${r.id}</td><td>${r.dimension}</td><td>${r.name}</td><td>${r.requirement}</td></tr>`).join('')}</tbody></table></div></section>`);}
async function previewFile(id){S.source={file_id:id,page:1};modal('项目原文预览','<div id="source-pane"></div>',null,true);await loadSource(id,1);}
function deleteFile(id){
 const file=S.project.files.find(f=>f.id===id);if(!file)return;
 modal('删除项目文件',`<form><p>确定删除 <strong>${esc(file.name)}</strong>（${esc(file.version)}）？</p><div class="notice warn">删除后不再显示在项目资料列表，也不能用于新审核。已被审核引用的原件及解析内容保留供历史记录查阅；未被引用的原件及解析内容永久清理。正在解析或参与运行中审核的文件须待处理结束后删除。</div>${field('操作人','actor',actor()||S.project.actor,'text',true,true)}${modalFoot('确认删除')}</form>`,async data=>{
  const result=await api('/files/'+id,{method:'DELETE',body:JSON.stringify(data)});rememberActor(data.actor);
  if(S.source?.file_id===id)S.source=null;
  await route();toast(result.retained_for_history?'文件已从项目资料删除，历史审核依据已保留':'文件已删除');
 });
}
document.addEventListener('click',async e=>{
 const el=e.target.closest('[data-action]');if(!el)return;const a=el.dataset.action;
 try{
  if(a==='close')$('#dialog').close();
  else if(a==='model-settings')await modelSettings();
  else if(a==='overview')location.hash='overview';
  else if(a==='new-project')newProject();
  else if(a==='tab'){S.tab=el.dataset.tab;renderProject();}
  else if(a==='choose-file' && e.target.id!=='upload-file'){$('#upload-file').click();}
  else if(a==='launch')launch();
  else if(a==='preview-file')await previewFile(el.dataset.file);
  else if(a==='delete-file')deleteFile(el.dataset.file);
  else if(a==='retry-file'){await post('/files/'+el.dataset.file+'/retry',{});toast('已重新提交解析');await route();}
  else if(a==='retry-task'){await post('/tasks/'+S.task.id+'/retry',{});toast('已重新提交审核');await route();}
  else if(a==='edit-issue')editIssue(el.dataset.id);
  else if(a==='manual-issue')manualIssue();
  else if(a==='confirm')confirm();
  else if(a==='edit-recheck')editRecheck(el.dataset.id);
  else if(a==='locate'){if(S.task?.file_ids.includes(el.dataset.file)){await loadSource(el.dataset.file,+el.dataset.page);}else{await previewFile(el.dataset.file);await loadSource(el.dataset.file,+el.dataset.page);}}
  else if(a==='prev-page')await loadSource(S.source.file_id,S.source.page-1);
  else if(a==='next-page')await loadSource(S.source.file_id,S.source.page+1);
  else if(a==='add-standard')addStandard();
  else if(a==='view-standard')viewStandard(el.dataset.id);
  else if(a==='edit-standard')addStandard(el.dataset.id);
  else if(a==='reload')location.reload();
 }catch(error){toast(error.message,true);}
});
$('#refresh').onclick=async()=>{S.health=await api('/health');updateHealth();await route();};
window.addEventListener('hashchange',()=>{if($('#dialog').open)$('#dialog').close();route();});
setInterval(async()=>{if($('#dialog').open||S.uploading||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;
 const [view]=currentHash().split('/');
 if(view==='project' && S.project?.files.some(f=>['queued','parsing'].includes(f.status)))await route();
 else if(view==='project' && S.tab==='tasks' && S.project?.tasks.some(t=>['queued','running'].includes(t.state)))await route();
 else if(view==='task' && ['queued','running'].includes(S.task?.state))await route();
},4000);
init().catch(error=>{$('#app').innerHTML=empty('启动连接失败',error.message,button('刷新','reload'));toast(error.message,true);});
