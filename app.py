import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import agent
from parsing import ALLOWED, MAX_SIZE, parse_file, render_png, validate_file
from rules import RULE_VERSION, rules_json
from store import DATA, ROOT, all_rows, db, dumps, event, execute, init, now, one, uid

pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='review')
active=set();active_lock=threading.RLock();upload_locks={}

def submit(kind,id):
    key=(kind,id)
    with active_lock:
        if key in active:return False
        active.add(key)
    def work():
        try:
            (parse_file if kind=='parse' else agent.run_task)(id)
        finally:
            with active_lock:active.discard(key)
    pool.submit(work)
    return True

@asynccontextmanager
async def lifespan(app):
    init()
    yield
    pool.shutdown(wait=False,cancel_futures=True)

app=FastAPI(title='建筑项目施组审查助手',lifespan=lifespan)

@app.middleware('http')
async def headers(request,call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control']='no-store'
    return response

def require(table,id):
    if table not in {'projects','files','uploads','tasks','issues','rechecks','standards'}:raise ValueError('table')
    row=one(f'SELECT * FROM {table} WHERE id=?',(id,))
    if row is None:raise HTTPException(404,'记录不存在')
    return row

def editable(task_id):
    task=require('tasks',task_id)
    if task['state']!='draft':raise HTTPException(409,'仅待复核初稿可修改；已确认版本保留快照')
    return task

class ProjectIn(BaseModel):
    name:str=Field(min_length=1,max_length=150)
    location:str=Field(default='陕西省西安市',max_length=200)
    mode:str='historical'
    baseline_date:str
    facts:str=Field(default='',max_length=6000)
    actor:str=Field(min_length=1,max_length=60)

def valid_date(value):
    from datetime import date
    try:return date.fromisoformat(value).isoformat()
    except ValueError:raise HTTPException(422,'日期格式应为 YYYY-MM-DD')

@app.get('/api/health')
def health():return {'status':'ok','model':agent.public_config(),'login_required':False,'storage':'SQLite 单机持久化'}

class ModelSettingsIn(BaseModel):
    base_url:str=Field(min_length=1,max_length=1000)
    model:str=Field(min_length=1,max_length=200)
    api_key:str=Field(default='',max_length=4000)

@app.get('/api/model/config')
def model_config():return agent.public_config()

@app.put('/api/model/config')
def save_model_config(body:ModelSettingsIn):
    with active_lock:
        if one("SELECT id FROM tasks WHERE state IN ('queued','running')"):
            raise HTTPException(409,'有审核任务正在等待或运行，请完成后再修改模型配置')
        try:return agent.save_model_settings(body.base_url,body.model,body.api_key)
        except ValueError as exc:raise HTTPException(422,str(exc))

@app.post('/api/model/test')
def test_model_config(body:ModelSettingsIn):
    try:
        selected=agent.model_settings(body.base_url,body.model,body.api_key)
        return agent.test_model_connection(selected)
    except ValueError as exc:raise HTTPException(422,str(exc))

@app.get('/api/rules')
def rule_list():return {'version':RULE_VERSION,'items':rules_json()}

@app.get('/api/projects')
def projects():
    rows=all_rows('SELECT * FROM projects ORDER BY created DESC')
    for r in rows:
        r['files']=one("SELECT COUNT(*) n FROM files WHERE project_id=? AND status!='deleted'",(r['id'],))['n']
        r['latest']=one('SELECT id,state,stage,created,confirmed FROM tasks WHERE project_id=? ORDER BY created DESC,rowid DESC LIMIT 1',(r['id'],))
        r['open_issues']=one("""SELECT COUNT(*) n FROM issues i JOIN tasks t ON t.id=i.task_id
            WHERE t.project_id=? AND i.decision='accepted' AND i.nature NOT IN ('优化建议','资料不足待核实')
            AND t.state='confirmed' AND COALESCE((SELECT r.confirmed FROM rechecks r JOIN tasks rt ON rt.id=r.task_id
              WHERE r.issue_id=i.id AND rt.state='confirmed' ORDER BY rt.created DESC,rt.rowid DESC LIMIT 1),'')!='已解决'""",(r['id'],))['n']
    return rows

@app.post('/api/projects')
def create_project(body:ProjectIn):
    if not body.name.strip() or not body.actor.strip():raise HTTPException(422,'请填写项目名称和发起人')
    if body.mode not in ['historical','current']:raise HTTPException(422,'审核模式不正确')
    valid_date(body.baseline_date)
    id=uid()
    execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?)',(id,body.name.strip(),body.location,body.mode,body.baseline_date,body.facts,body.actor.strip(),now()))
    event(id,body.actor,'建立项目',{'name':body.name})
    return require('projects',id)

@app.get('/api/projects/{id}')
def project(id):
    p=require('projects',id)
    p['files']=all_rows('SELECT id,project_id,name,role,version,hash,size,status,page_count,done,error,created FROM files WHERE project_id=? ORDER BY created DESC',(id,))
    p['archived_files']=[f for f in p['files'] if f['status']=='deleted']
    p['files']=[f for f in p['files'] if f['status']!='deleted']
    for f in p['files']:
        f['coverage']=all_rows('SELECT quality,COUNT(*) n FROM pages WHERE file_id=? GROUP BY quality',(f['id'],))
    p['tasks']=all_rows('SELECT * FROM tasks WHERE project_id=? ORDER BY created DESC,rowid DESC',(id,))
    for t in p['tasks']:
        t.pop('snapshot',None);t.pop('result',None)
    p['events']=all_rows('SELECT * FROM events WHERE project_id=? ORDER BY created DESC,rowid DESC LIMIT 100',(id,))
    return p

class UploadIn(BaseModel):
    project_id:str
    name:str=Field(min_length=1,max_length=240)
    role:str='main'
    version:str=Field(min_length=1,max_length=80)
    size:int=Field(gt=0,le=MAX_SIZE)

@app.post('/api/uploads')
def upload_create(body:UploadIn):
    require('projects',body.project_id)
    if body.role not in ['main','drawing','bill','other']:raise HTTPException(422,'资料类型不正确')
    if Path(body.name).suffix.lower() not in ALLOWED:raise HTTPException(422,'文件格式不支持')
    # A matching incomplete upload can resume without duplicating data.
    row=one('''SELECT * FROM uploads WHERE project_id=? AND name=? AND role=? AND version=? AND size=? AND file_id IS NULL
      ORDER BY created DESC LIMIT 1''',(body.project_id,body.name,body.role,body.version,body.size))
    if row:return row
    id=uid()
    execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,NULL)',(id,body.project_id,Path(body.name.replace('\\','/')).name,body.role,body.version,body.size,0,now()))
    return require('uploads',id)

@app.put('/api/uploads/{id}')
async def upload_chunk(id:str,request:Request,offset:int):
    row=require('uploads',id)
    if row['file_id']:raise HTTPException(409,'该上传已完成')
    body=bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body)>8*1024*1024:raise HTTPException(413,'单个上传分块不能超过8 MiB')
    lock=upload_locks.setdefault(id,threading.Lock())
    with lock:
        row=require('uploads',id)
        if offset!=row['offset']:raise HTTPException(409,'上传位置不匹配，请重新选择同一文件续传')
        if not body or offset+len(body)>row['size']:raise HTTPException(422,'上传数据长度不正确')
        path=DATA/'chunks'/id
        # Recover a partial physical write not yet committed in SQLite.
        with open(path,'r+b' if path.exists() else 'w+b') as f:
            f.truncate(offset);f.seek(offset);f.write(body);f.flush();os.fsync(f.fileno())
        execute('UPDATE uploads SET offset=? WHERE id=?',(offset+len(body),id))
    return {'offset':offset+len(body)}

class Actor(BaseModel):
    actor:str=Field(min_length=1,max_length=60)

@app.post('/api/uploads/{id}/complete')
def upload_complete(id:str,body:Actor):
    lock=upload_locks.setdefault(id,threading.Lock())
    with lock:
        row=require('uploads',id)
        if row['file_id']:return {'file_id':row['file_id']}
        if row['offset']!=row['size']:raise HTTPException(409,'上传尚未完成')
        source=DATA/'chunks'/id
        try:ext=validate_file(source,row['name'])
        except Exception as exc:raise HTTPException(422,str(exc))
        with open(source,'rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        file_id=uid();target=DATA/'files'/(file_id+ext)
        source.replace(target)
        try:
            with db() as c:
                c.execute('''INSERT INTO files(id,project_id,name,role,version,path,hash,size,status,created)
                  VALUES(?,?,?,?,?,?,?,?,?,?)''',(file_id,row['project_id'],row['name'],row['role'],row['version'],str(target),digest,row['size'],'queued',now()))
                c.execute('UPDATE uploads SET file_id=? WHERE id=?',(file_id,id))
        except Exception:
            target.replace(source);raise
    event(row['project_id'],body.actor,'上传资料',{'file_id':file_id,'name':row['name'],'version':row['version']})
    submit('parse',file_id)
    return {'file_id':file_id}

@app.post('/api/files/{id}/retry')
def retry_parse(id:str):
    with active_lock:
        row=require('files',id)
        if row['status']!='failed':raise HTTPException(409,'只有失败解析可以重试')
        execute("UPDATE files SET status='queued',error='' WHERE id=?",(id,))
        submit('parse',id);return {'ok':True}

@app.get('/api/files/{id}/pages')
def file_pages(id:str,search:str='',offset:int=0,limit:int=30):
    require('files',id);limit=max(1,min(100,limit));offset=max(0,offset)
    return all_rows('SELECT * FROM pages WHERE file_id=? AND (text LIKE ? OR label LIKE ?) ORDER BY number LIMIT ? OFFSET ?',
                    (id,'%'+search+'%','%'+search+'%',limit,offset))

@app.get('/api/files/{id}/pages/{number}')
def page_text(id:str,number:int):
    row=one('SELECT * FROM pages WHERE file_id=? AND number=?',(id,number))
    if not row:raise HTTPException(404,'页面尚未解析或不存在')
    return row

@app.get('/api/files/{id}/pages/{number}/image')
def page_image(id:str,number:int):
    f=require('files',id)
    path=Path(f['path']) if f['name'].lower().endswith('.pdf') else DATA/'previews'/(id+'.pdf')
    if not path.exists():raise HTTPException(404,'该资料使用文字或单元格定位，无固定版式预览')
    cache=DATA/'previews'/f'{id}-{number}.png'
    if not cache.exists():
        try:cache.write_bytes(render_png(path,number))
        except Exception as exc:raise HTTPException(422,str(exc))
    return FileResponse(cache,media_type='image/png')

@app.get('/api/files/{id}/download')
def original(id:str):
    f=require('files',id)
    return FileResponse(f['path'],filename=f['name'])

@app.delete('/api/files/{id}')
def delete_file(id:str,body:Actor):
    if not body.actor.strip():raise HTTPException(422,'请填写操作人')
    with active_lock,db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM files WHERE id=?',(id,)).fetchone()
        if not row or row['status']=='deleted':raise HTTPException(404,'文件不存在或已删除')
        if row['status'] in ['queued','parsing'] or ('parse',id) in active:
            raise HTTPException(409,'文件正在等待解析或解析中，请完成后再删除')
        tasks=c.execute('SELECT main_file,file_ids,state FROM tasks WHERE project_id=?',(row['project_id'],)).fetchall()
        references=[t for t in tasks if t['main_file']==id or id in json.loads(t['file_ids'])]
        if any(t['state'] in ['queued','running'] for t in references):
            raise HTTPException(409,'文件正在参与审核，请审核结束后再删除')
        if references:
            c.execute("UPDATE files SET status='deleted' WHERE id=?",(id,))
        else:
            paths=[DATA/'previews'/(id+'.pdf'),*list((DATA/'previews').glob(id+'-*.png')),Path(row['path'])]
            if any(not path.resolve().is_relative_to(DATA) for path in paths):
                raise HTTPException(409,'文件路径异常，未执行删除')
            try:
                for path in paths:path.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(409,'文件无法删除，请关闭占用该文件的程序后重试') from exc
            c.execute('DELETE FROM pages WHERE file_id=?',(id,))
            c.execute('DELETE FROM uploads WHERE file_id=?',(id,))
            c.execute('DELETE FROM files WHERE id=?',(id,))
        c.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(uid(),row['project_id'],body.actor.strip(),'删除资料',
            dumps({'file_id':id,'name':row['name'],'version':row['version'],'retained_for_history':bool(references)}),now()))
    return {'ok':True,'retained_for_history':bool(references)}

class TaskIn(BaseModel):
    main_file:str
    file_ids:list[str]
    baseline_id:str=''
    actor:str=Field(min_length=1,max_length=60)
    facts:str=Field(min_length=1,max_length=6000)

@app.post('/api/projects/{id}/tasks')
def start_task(id:str,body:TaskIn):
    with active_lock:return create_task(id,body)

def create_task(id,body):
    p=require('projects',id)
    ids=list(dict.fromkeys([body.main_file]+body.file_ids))
    for file_id in ids:
        f=require('files',file_id)
        if f['project_id']!=id or f['status']!='ready':raise HTTPException(422,'请选择本项目已完成解析的资料')
    if require('files',body.main_file)['role']!='main':raise HTTPException(422,'主文件必须是施工组织设计或方案')
    if not body.facts.strip() or not body.actor.strip():raise HTTPException(422,'请确认工程事实并填写发起人')
    if body.baseline_id:
        base=require('tasks',body.baseline_id)
        if base['project_id']!=id or base['state']!='confirmed':raise HTTPException(422,'复审基线必须是本项目已人工复核的任务')
        if base['main_file']==body.main_file:raise HTTPException(422,'整改复审需选择新上传的修改版主文件')
    task_id=uid()
    snapshot={'project':p,'files':[require('files',f) for f in ids],'model':agent.public_config(),
              'rules':rules_json(),'rule_version':RULE_VERSION,'prompt_version':agent.PROMPT_VERSION,
              'standards':agent.applicable_standards(p['baseline_date'])}
    for file in snapshot['files']:file.pop('path',None)
    execute('''INSERT INTO tasks(id,project_id,main_file,file_ids,baseline_id,actor,facts,state,stage,snapshot,result,created,updated)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(task_id,id,body.main_file,dumps(ids),body.baseline_id,body.actor,body.facts,'queued','等待后台审核',dumps(snapshot),'{}',now(),now()))
    event(id,body.actor,'发起整改复审' if body.baseline_id else '发起审核',{'task_id':task_id,'files':ids})
    submit('task',task_id)
    return {'id':task_id}

@app.get('/api/tasks/{id}')
def task_detail(id:str):
    t=require('tasks',id)
    for key in ['file_ids','snapshot','result']:t[key]=json.loads(t[key] or ('[]' if key=='file_ids' else '{}'))
    t['issues']=all_rows('SELECT * FROM issues WHERE task_id=? ORDER BY rowid',(id,))
    for i in t['issues']:
        for k in ['evidence','citations']:i[k]=json.loads(i[k] or '[]')
    t['rechecks']=all_rows('''SELECT r.*,i.code,i.title,i.nature,i.evidence old_evidence,i.recommendation
        FROM rechecks r JOIN issues i ON i.id=r.issue_id WHERE r.task_id=? ORDER BY i.code''',(id,))
    for r in t['rechecks']:
        for k in ['evidence','old_evidence']:r[k]=json.loads(r[k] or '[]')
    eligible=[r for r in t['rechecks'] if r['nature'] not in ['优化建议','资料不足待核实']]
    t['rectification_rate']=round(100*sum(r['confirmed']=='已解决' for r in eligible)/len(eligible)) if eligible else None
    return t

@app.post('/api/tasks/{id}/retry')
def retry_task(id:str):
    with active_lock:
        t=require('tasks',id)
        if t['state'] not in ['failed','waiting_config']:raise HTTPException(409,'当前任务不可重试')
        execute("UPDATE tasks SET state='queued',stage='等待后台审核',error='',updated=? WHERE id=?",(now(),id))
        submit('task',id);return {'ok':True}

class IssueIn(BaseModel):
    title:str=Field(min_length=1,max_length=150)
    nature:str
    risk:str
    description:str=Field(min_length=1,max_length=10000)
    recommendation:str=Field(default='',max_length=6000)
    decision:str='pending'
    review_note:str=Field(default='',max_length=2000)
    owner:str=Field(default='',max_length=80)
    deadline:str=''
    actor:str=Field(min_length=1,max_length=60)

@app.patch('/api/issues/{id}')
def update_issue(id:str,body:IssueIn):
    with active_lock:return edit_issue(id,body)

def edit_issue(id,body):
    issue=require('issues',id);task=editable(issue['task_id'])
    if body.nature not in agent.NATURES or body.risk not in agent.RISKS or body.decision not in ['pending','accepted','rejected']:
        raise HTTPException(422,'问题分类或处置无效')
    if body.decision=='rejected' and not body.review_note.strip():raise HTTPException(422,'驳回需填写原因')
    if not body.title.strip() or not body.description.strip() or not body.actor.strip():raise HTTPException(422,'标题、问题说明及复核人不能为空')
    if body.nature in ['确认不符合','明确要求缺项'] and not json.loads(issue['citations'] or '[]'):
        raise HTTPException(422,'此类结论需有可追溯规范依据；请保留为待核实或完整性不足')
    if body.deadline:valid_date(body.deadline)
    old={k:issue[k] for k in body.model_dump() if k in issue}
    data=body.model_dump();data['updated']=now()
    execute('UPDATE issues SET '+','.join(k+'=?' for k in data)+' WHERE id=?',(*data.values(),id))
    event(task['project_id'],body.actor,'修改或复核问题',{'issue_id':id,'before':old,'after':data})
    return {'ok':True}

class ManualIssue(IssueIn):
    rule_id:str='16'
    file_id:str
    page:int
    quote:str=Field(min_length=4,max_length=4000)

@app.post('/api/tasks/{id}/issues')
def add_issue(id:str,body:ManualIssue):
    with active_lock:return create_issue(id,body)

def create_issue(id,body):
    task=editable(id)
    if not body.title.strip() or not body.description.strip() or not body.actor.strip():raise HTTPException(422,'标题、问题说明及补充人不能为空')
    if body.nature not in agent.NATURES or body.risk not in agent.RISKS or body.rule_id.zfill(2) not in {r['id'] for r in rules_json()}:
        raise HTTPException(422,'问题分类、风险或检查规则无效')
    if body.file_id not in json.loads(task['file_ids']):raise HTTPException(422,'证据资料不属于本次任务')
    page=page_text(body.file_id,body.page)
    raw=body.model_dump();raw['evidence']=[{'file_id':body.file_id,'page':body.page,'quote':body.quote}]
    value=agent.validate_issue(raw,{(body.file_id,body.page):page},json.loads(task['snapshot'])['standards'])
    if not value or not value['evidence']:raise HTTPException(422,'原文摘录未在该页定位，请逐字摘录')
    agent.store_issues(id,[raw],{(body.file_id,body.page):page},json.loads(task['snapshot'])['standards'])
    event(task['project_id'],body.actor,'人工补充问题',{'title':body.title})
    return {'ok':True}

class RecheckIn(BaseModel):
    actor:str=Field(min_length=1,max_length=60)
    status:str
    note:str=Field(min_length=1,max_length=3000)

@app.patch('/api/rechecks/{id}')
def confirm_recheck(id:str,body:RecheckIn):
    with active_lock:return edit_recheck(id,body)

def edit_recheck(id,body):
    r=require('rechecks',id);t=editable(r['task_id'])
    if body.status not in ['已解决','部分解决','未解决','无法判断']:raise HTTPException(422,'复审状态无效')
    if not body.actor.strip() or not body.note.strip():raise HTTPException(422,'复审人及复核依据不能为空')
    execute('UPDATE rechecks SET confirmed=?,actor=?,note=?,updated=? WHERE id=?',(body.status,body.actor,body.note,now(),id))
    event(t['project_id'],body.actor,'确认整改复审',{'recheck_id':id,**body.model_dump()})
    return {'ok':True}

class ConfirmIn(BaseModel):
    actor:str=Field(min_length=1,max_length=60)
    conclusion:str
    note:str=Field(min_length=1,max_length=5000)

@app.post('/api/tasks/{id}/confirm')
def confirm_task(id:str,body:ConfirmIn):
    with active_lock:return finalize_task(id,body)

def finalize_task(id,body):
    t=editable(id)
    if not body.actor.strip() or not body.note.strip():raise HTTPException(422,'技术复核人及复核意见不能为空')
    allowed=['未发现需整改问题','修改后复核','重新编制后报审','资料不足暂不形成结论']
    if body.conclusion not in allowed:raise HTTPException(422,'结论无效')
    if one("SELECT id FROM issues WHERE task_id=? AND decision='pending'",(id,)):
        raise HTTPException(409,'请先逐项采纳或驳回全部问题')
    if one("SELECT id FROM rechecks WHERE task_id=? AND confirmed=''",(id,)):
        raise HTTPException(409,'请先确认全部整改复审记录')
    if body.conclusion==allowed[0]:
        blockers=one("SELECT id FROM issues WHERE task_id=? AND decision='accepted' AND nature!='优化建议'",(id,))
        open_rechecks=one("SELECT id FROM rechecks WHERE task_id=? AND confirmed!='已解决'",(id,))
        limits=json.loads(t['result'] or '{}').get('limitations',[])
        if blockers or open_rechecks or limits:raise HTTPException(409,'存在未解决事项或审核限制，不能形成无保留结论')
    execute("UPDATE tasks SET state='confirmed',reviewer=?,conclusion=?,review_note=?,confirmed=?,updated=? WHERE id=?",(body.actor,body.conclusion,body.note,now(),now(),id))
    event(t['project_id'],body.actor,'确认内部审核报告',{'task_id':id,'conclusion':body.conclusion,'note':body.note})
    return {'ok':True}

class StandardIn(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    code:str=Field(min_length=1,max_length=100)
    version:str=Field(min_length=1,max_length=80)
    category:str='国家'
    source:str=Field(min_length=1,max_length=1000)
    effective:str
    expires:str=''
    conditions:str=Field(min_length=1,max_length=4000)
    content:str=Field(min_length=10,max_length=200000)
    state:str='pending'
    actor:str=Field(min_length=1,max_length=60)

@app.get('/api/standards')
def standard_list():return all_rows('SELECT * FROM standards ORDER BY created DESC')

@app.post('/api/standards')
def add_standard(body:StandardIn):
    validate_standard(body)
    id=uid();data=body.model_dump()
    execute('INSERT INTO standards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
       (id,data['name'],data['code'],data['version'],data['category'],data['source'],data['effective'],data['expires'],data['conditions'],data['content'],data['state'],data['actor'],now(),now()))
    return {'id':id}

def validate_standard(body):
    if any(not getattr(body,key).strip() for key in ['name','code','version','source','conditions','content','actor']):
        raise HTTPException(422,'请填写规范名称、编号、版本、来源、适用条件、原文和核验人员')
    valid_date(body.effective)
    if body.expires:
        valid_date(body.expires)
        if body.expires<=body.effective:raise HTTPException(422,'失效日期应晚于实施日期')
    if body.state not in ['pending','verified']:raise HTTPException(422,'核验状态无效')

@app.put('/api/standards/{id}')
def update_standard(id:str,body:StandardIn):
    require('standards',id);validate_standard(body)
    data=body.model_dump();data['updated']=now()
    execute('UPDATE standards SET '+','.join(k+'=?' for k in data)+' WHERE id=?',(*data.values(),id))
    return {'id':id}

@app.get('/api/tasks/{id}/report.docx')
def report(id:str):
    from reports import build_report
    t=task_detail(id);path=build_report(t)
    name=('内部审核报告' if t['state']=='confirmed' else '待复核初稿')+'-'+id[:8]+'.docx'
    return FileResponse(path,filename=name,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

app.mount('/',StaticFiles(directory=ROOT/'static',html=True),name='web')
