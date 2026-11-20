import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory()
os.environ['REVIEW_DATA_DIR'] = TEMP.name

from fastapi.testclient import TestClient
from pypdf import PdfWriter
import agent
import app as web
from parsing import parse_file
from store import DATA, all_rows, dumps, execute, init, now, one, uid


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init()
        cls.client=TestClient(web.app)

    def setUp(self):
        self.mock_submit=patch('app.submit',return_value=True)
        self.mock_submit.start()
        p=self.client.post('/api/projects',json=dict(name='教学综合楼测试',location='陕西西安',mode='historical',
            baseline_date='2020-11-24',facts='学校教学综合楼',actor='测试技术负责人'))
        self.assertEqual(p.status_code,200,p.text)
        self.project=p.json()['id']

    def tearDown(self):self.mock_submit.stop()

    def upload(self,text='本工程为教学综合楼，暖通说明使用住宅内供暖。',version='V1'):
        writer=PdfWriter();writer.add_blank_page(width=595,height=842);writer.add_blank_page(width=595,height=842)
        stream=io.BytesIO();writer.write(stream);content=stream.getvalue()
        meta=dict(project_id=self.project,name='测试.pdf',role='main',version=version,size=len(content))
        row=self.client.post('/api/uploads',json=meta).json();id=row['id']
        offset=100
        self.assertEqual(self.client.put(f'/api/uploads/{id}?offset=0',content=content[:offset]).status_code,200)
        resumed=self.client.post('/api/uploads',json=meta).json()
        self.assertEqual(resumed['offset'],offset)
        self.assertEqual(self.client.put(f'/api/uploads/{id}?offset=0',content=content[offset:]).status_code,409)
        self.assertEqual(self.client.put(f'/api/uploads/{id}?offset={offset}',content=content[offset:]).status_code,200)
        done=self.client.post(f'/api/uploads/{id}/complete',json={'actor':'测试上传人'})
        self.assertEqual(done.status_code,200,done.text)
        fid=done.json()['file_id']
        with patch('parsing.ocr_page',return_value=('','测试无OCR')):
            parse_file(fid)
        parsed=one('SELECT * FROM files WHERE id=?',(fid,))
        self.assertEqual(parsed['status'],'ready');self.assertEqual(parsed['page_count'],2)
        self.assertEqual(one('SELECT COUNT(*) n FROM pages WHERE file_id=?',(fid,))['n'],2)
        execute("UPDATE pages SET text=?,quality='readable',note='' WHERE file_id=? AND number=1",(text,fid))
        execute("UPDATE pages SET text=?,quality='readable',note='' WHERE file_id=? AND number=2",('施工方案和质量安全管理要求。',fid))
        return fid

    def new_task(self,fid,base=''):
        r=self.client.post(f'/api/projects/{self.project}/tasks',json=dict(main_file=fid,file_ids=[fid],baseline_id=base,
                actor='测试技术负责人',facts='已确认：学校教学综合楼；仅施工组织设计'))
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['id']

    def test_start_task_allows_no_additional_facts_without_claiming_confirmation(self):
        fid=self.upload()
        response=self.client.post(f'/api/projects/{self.project}/tasks',json=dict(
            main_file=fid,file_ids=[fid],actor='测试技术负责人',facts=''))
        self.assertEqual(response.status_code,200,response.text)
        task=one('SELECT facts FROM tasks WHERE id=?',(response.json()['id'],))
        self.assertIn('用户未补充其他工程事实',task['facts'])
        self.assertIn('标记待核实',task['facts'])

    def test_delete_unused_file_cleans_storage_and_records_event(self):
        fid=self.upload()
        original=Path(one('SELECT path FROM files WHERE id=?',(fid,))['path'])
        preview=DATA/'previews'/(fid+'.pdf');preview.write_bytes(b'test preview')
        image=DATA/'previews'/(fid+'-1.png');image.write_bytes(b'test image')
        response=self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试删除人'})
        self.assertEqual(response.status_code,200,response.text)
        for path in [original,preview,image]:self.assertFalse(path.exists())
        for table,column in [('files','id'),('pages','file_id'),('uploads','file_id')]:
            self.assertEqual(one(f'SELECT COUNT(*) n FROM {table} WHERE {column}=?',(fid,))['n'],0)
        project=self.client.get('/api/projects/'+self.project).json()
        self.assertEqual(project['files'],[])
        event=next(e for e in project['events'] if e['action']=='删除资料')
        self.assertEqual(event['actor'],'测试删除人')
        self.assertEqual(json.loads(event['detail'])['file_id'],fid)
        self.assertEqual(self.client.get('/api/files/'+fid+'/download').status_code,404)
        self.assertEqual(self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试'}).status_code,404)

    def test_delete_rejects_parsing_and_active_review(self):
        fid=self.upload();path=Path(one('SELECT path FROM files WHERE id=?',(fid,))['path'])
        for state in ['queued','parsing']:
            execute('UPDATE files SET status=? WHERE id=?',(state,fid))
            self.assertEqual(self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试'}).status_code,409)
            self.assertTrue(path.exists())
        execute("UPDATE files SET status='ready' WHERE id=?",(fid,))
        tid=self.new_task(fid)
        for state in ['queued','running']:
            execute('UPDATE tasks SET state=? WHERE id=?',(state,tid))
            response=self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试'})
            self.assertEqual(response.status_code,409,response.text)
            self.assertTrue(path.exists())

    def test_delete_referenced_files_retains_history_and_blocks_new_review(self):
        fid=self.upload();tid=self.new_task(fid)
        execute("UPDATE tasks SET state='draft' WHERE id=?",(tid,))
        before=self.client.get('/api/tasks/'+tid).json()
        path=Path(one('SELECT path FROM files WHERE id=?',(fid,))['path'])
        response=self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试删除人'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['retained_for_history'])
        self.assertTrue(path.exists())
        project=self.client.get('/api/projects/'+self.project).json()
        self.assertEqual(project['files'],[])
        self.assertEqual(project['archived_files'][0]['id'],fid)
        overview=next(p for p in self.client.get('/api/projects').json() if p['id']==self.project)
        self.assertEqual(overview['files'],0)
        after=self.client.get('/api/tasks/'+tid).json()
        self.assertEqual(before,after)
        self.assertEqual(self.client.get('/api/files/'+fid+'/pages/1').status_code,200)
        self.assertEqual(self.client.get('/api/files/'+fid+'/pages/1/image').status_code,200)
        self.assertEqual(self.client.get('/api/files/'+fid+'/download').status_code,200)
        self.assertEqual(self.client.get('/api/tasks/'+tid+'/report.docx').status_code,200)
        result=self.client.post('/api/projects/'+self.project+'/tasks',json=dict(main_file=fid,file_ids=[fid],actor='测试',facts='工程事实'))
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.client.request('DELETE','/api/files/'+fid,json={'actor':'测试'}).status_code,404)
        # Supplementary files use the same historical retention rule.
        other=self.upload(version='V2')
        execute('UPDATE tasks SET file_ids=? WHERE id=?',(dumps([fid,other]),tid))
        self.assertEqual(self.client.request('DELETE','/api/files/'+other,json={'actor':'测试'}).status_code,200)
        self.assertEqual(self.client.get('/api/files/'+other+'/pages/1').status_code,200)

    def test_no_key_and_parse_resume(self):
        fid=self.upload();tid=self.new_task(fid)
        with patch('agent.config',return_value=dict(model='agnes-3.0-flash',base_url='https://api.agnes-ai.cn/v1',key='')):
            agent.run_task(tid)
        r=self.client.get('/api/tasks/'+tid).json()
        self.assertEqual(r['state'],'waiting_config');self.assertEqual(r['issues'],[])
        page=self.client.get(f'/api/files/{fid}/pages/1/image')
        self.assertEqual(page.status_code,200);self.assertTrue(page.content.startswith(b'\x89PNG'))

    def test_evidence_citations_and_historical_filter(self):
        page={'text':'本工程是教学楼。','label':'PDF第1页','quality':'readable'}
        raw=dict(rule_id='03',title='test',description='有疑点',nature='确认不符合',risk='重大',
             evidence=[{'file_id':'f','page':1,'quote':'伪造原文'}],citations=[{'standard_id':'missing','quote':'不存在的条文'}])
        result=agent.validate_issue(raw,{('f',1):page},[])
        self.assertEqual(result['nature'],'资料不足待核实');self.assertEqual(result['evidence'],[])
        self.assertEqual(result['risk'],'待判定')
        standard=dict(name='测试标准非真实依据',code='TEST',version='v1',category='企业',source='本地测试资料',
            effective='2021-01-01',expires='',conditions='仅测试',content='1.0.1 这是测试条文，不用于工程审核。',state='verified',actor='测试')
        r=self.client.post('/api/standards',json=standard);self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn(r.json()['id'],[s['id'] for s in agent.applicable_standards('2020-11-24')])
        self.assertIn(r.json()['id'],[s['id'] for s in agent.applicable_standards('2021-01-01')])
        standard['effective']='bad'
        self.assertEqual(self.client.post('/api/standards',json=standard).status_code,422)

    def model_response(self,payload):
        if payload['stage']=='局部内容审核':
            page=payload['main_pages'][0]
            return {'issues':[dict(rule_id='03',title='疑似住宅模板套用',description='教学楼暖通说明出现住宅表述，需核对设计。',
                  nature='确认不符合',risk='一般',evidence=[{'file_id':page['file_id'],'page':page['number'],'quote':page['text']}],
                  citations=[],recommendation='核对设计并调整说明。')],
                  'observations':[{'rule_id':'03','finding':'出现住宅说明，需要核实。'}]},{}
        if payload['stage']=='单项整改复审':
            p=payload['new_pages'][0]
            return {'status':'已解决','reason':'需要人工复核新的工程说明。','evidence':[{'file_id':p['file_id'],'page':p['number'],'quote':p['text']}]},{}
        return {'summary':'测试初稿，不是真实专业审核结果。','issues':[],
            'checks':[{'rule_id':r['id'],'status':'待核实','reason':'需技术人员核实'} for r in agent.rules_json()],
            'limitations':['缺少真实规范资料']},{}

    def test_review_confirm_export_and_rectification(self):
        fid=self.upload();config=dict(model='agnes-3.0-flash',base_url='https://api.agnes-ai.cn/v1',key='test-not-real')
        with patch('agent.config',return_value=config),patch('agent.call_model',side_effect=self.model_response):
            tid=self.new_task(fid);agent.run_task(tid)
        t=self.client.get('/api/tasks/'+tid).json();self.assertEqual(t['state'],'draft',t['error'])
        self.assertEqual(len(t['issues']),1);issue=t['issues'][0]
        self.assertEqual(issue['nature'],'资料不足待核实')
        confirm=dict(actor='测试复核人',conclusion='修改后复核',note='测试确认')
        self.assertEqual(self.client.post(f'/api/tasks/{tid}/confirm',json=confirm).status_code,409)
        body={k:issue[k] for k in ['title','nature','risk','description','recommendation','owner','deadline','review_note']}
        body.update(decision='accepted',actor='测试复核人')
        self.assertEqual(self.client.patch('/api/issues/'+issue['id'],json=body).status_code,200)
        self.assertEqual(self.client.post(f'/api/tasks/{tid}/confirm',json={**confirm,'conclusion':'未发现需整改问题'}).status_code,409)
        self.assertEqual(self.client.post(f'/api/tasks/{tid}/confirm',json=confirm).status_code,200)
        self.assertEqual(self.client.patch('/api/issues/'+issue['id'],json=body).status_code,409)
        report=self.client.get(f'/api/tasks/{tid}/report.docx')
        self.assertEqual(report.status_code,200);self.assertTrue(report.content.startswith(b'PK'))
        fid2=self.upload('本工程为学校教学综合楼，采用教室供暖系统。','V2')
        with patch('agent.config',return_value=config),patch('agent.call_model',side_effect=self.model_response):
            t2id=self.new_task(fid2,tid);agent.run_task(t2id)
        t2=self.client.get('/api/tasks/'+t2id).json();self.assertEqual(t2['state'],'draft',t2['error'])
        self.assertEqual(len(t2['rechecks']),1)
        check=t2['rechecks'][0];self.assertEqual(check['issue_id'],issue['id'])
        self.assertEqual(check['confirmed'],'')
        r=self.client.patch('/api/rechecks/'+check['id'],json={'actor':'复审人员','status':'已解决','note':'人工核对后确认，测试记录'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/tasks/'+tid).json()['state'],'confirmed')

    def test_invalid_upload_and_project_isolation(self):
        r=self.client.post('/api/uploads',json=dict(project_id=self.project,name='fake.pdf',role='main',version='V1',size=10))
        id=r.json()['id'];self.client.put(f'/api/uploads/{id}?offset=0',content=b'not-a-pdf!')
        result=self.client.post(f'/api/uploads/{id}/complete',json={'actor':'test'})
        self.assertEqual(result.status_code,422)
        r=self.client.get('/api/projects/not-found');self.assertEqual(r.status_code,404)

    def test_office_source_coordinates_and_formula_preservation(self):
        from docx import Document
        from openpyxl import Workbook
        for extension in ['.docx','.xlsx']:
            file_id=uid();path=DATA/'files'/(file_id+extension)
            if extension=='.docx':
                doc=Document();doc.add_heading('教学综合楼施工方案',0);doc.add_paragraph('主体结构为框架结构。')
                table=doc.add_table(rows=1,cols=2);table.cell(0,0).text='混凝土强度';table.cell(0,1).text='C30'
                doc.save(path)
            else:
                book=Workbook();sheet=book.active;sheet.title='工程量清单'
                sheet.append(['项目名称','数量']);sheet.append(['混凝土',10]);sheet['B3']='=B2*2';book.save(path)
            execute('''INSERT INTO files(id,project_id,name,role,version,path,hash,size,status,created)
              VALUES(?,?,?,?,?,?,?,?,?,?)''',(file_id,self.project,'测试'+extension,'main' if extension=='.docx' else 'bill','V1',str(path),'test',path.stat().st_size,'queued',now()))
            with patch('parsing.office_pdf',return_value=False):parse_file(file_id)
            self.assertEqual(one('SELECT status FROM files WHERE id=?',(file_id,))['status'],'ready')
            pages=all_rows('SELECT * FROM pages WHERE file_id=?',(file_id,))
            if extension=='.docx':
                self.assertTrue(any('原文块' in p['label'] for p in pages))
                self.assertTrue(any('框架结构' in p['text'] for p in pages))
                self.assertEqual(sum(p['text'].count('混凝土强度') for p in pages),1)
                self.assertEqual(sum(p['text'].count('C30') for p in pages),1)
            else:
                self.assertIn('工程量清单',pages[0]['label']);self.assertIn('B3==B2*2',pages[0]['text'])

    def test_restart_recovers_waiting_work_and_retry_is_queued(self):
        fid=self.upload();tid=self.new_task(fid)
        execute("UPDATE files SET status='queued' WHERE id=?",(fid,))
        init()
        self.assertEqual(one('SELECT status FROM files WHERE id=?',(fid,))['status'],'failed')
        self.assertEqual(one('SELECT state FROM tasks WHERE id=?',(tid,))['state'],'failed')
        self.assertEqual(self.client.post('/api/files/'+fid+'/retry').status_code,200)
        self.assertEqual(one('SELECT status FROM files WHERE id=?',(fid,))['status'],'queued')
        self.assertEqual(self.client.post('/api/files/'+fid+'/retry').status_code,409)
        self.assertEqual(self.client.post('/api/tasks/'+tid+'/retry').status_code,200)
        self.assertEqual(one('SELECT state FROM tasks WHERE id=?',(tid,))['state'],'queued')
        self.assertEqual(self.client.post('/api/tasks/'+tid+'/retry').status_code,409)
        with patch('agent.save_model_settings') as save:
            response=self.client.put('/api/model/config',json=dict(base_url='https://example.com/v1',model='test',api_key='test'))
            self.assertEqual(response.status_code,409)
            save.assert_not_called()

    def test_failed_summary_resumes_without_repeating_completed_batch(self):
        fid=self.upload();tid=self.new_task(fid)
        def fail_summary(payload):
            if payload['stage']!='局部内容审核':raise ValueError('测试汇总失败')
            return self.model_response(payload)
        config=dict(model='agnes-3.0-flash',base_url='https://api.agnes-ai.cn/v1',key='test-not-real')
        with patch('agent.config',return_value=config),patch('agent.call_model',side_effect=fail_summary):agent.run_task(tid)
        task=self.client.get('/api/tasks/'+tid).json()
        self.assertEqual(task['state'],'failed');self.assertEqual(len(task['result']['completed']),1)
        with patch('agent.config',return_value=config),patch('agent.call_model',side_effect=self.model_response) as call:
            agent.run_task(tid)
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0]['stage'],'全文检查覆盖与总结')
        task=self.client.get('/api/tasks/'+tid).json()
        self.assertEqual(task['state'],'draft');self.assertEqual(len(task['issues']),1)

    def test_multiple_rechecks_keep_unresolved_ancestor_issues(self):
        config=dict(model='agnes-3.0-flash',base_url='https://api.agnes-ai.cn/v1',key='test-not-real')
        confirm=dict(actor='测试复核人',conclusion='修改后复核',note='逐项复核')
        with patch('agent.config',return_value=config),patch('agent.call_model',side_effect=self.model_response):
            fid=self.upload();first=self.new_task(fid);agent.run_task(first)
            issue=self.client.get('/api/tasks/'+first).json()['issues'][0]
            execute("UPDATE issues SET decision='accepted',nature='完整性不足' WHERE id=?",(issue['id'],))
            self.assertEqual(self.client.post('/api/tasks/'+first+'/confirm',json=confirm).status_code,200)
            fid2=self.upload(version='V2');second=self.new_task(fid2,first);agent.run_task(second)
            second_task=self.client.get('/api/tasks/'+second).json()
            check=second_task['rechecks'][0]
            self.client.patch('/api/rechecks/'+check['id'],json={'actor':'复核人','status':'未解决','note':'本轮未修正'})
            self.assertEqual(self.client.post('/api/tasks/'+second+'/confirm',json=confirm).status_code,200)
            fid3=self.upload(version='V3');third=self.new_task(fid3,second);agent.run_task(third)
        task=self.client.get('/api/tasks/'+third).json()
        self.assertEqual(len(task['rechecks']),1)
        self.assertEqual(task['rechecks'][0]['issue_id'],issue['id'])
        check=task['rechecks'][0]
        self.client.patch('/api/rechecks/'+check['id'],json={'actor':'复核人','status':'已解决','note':'第三轮人工确认'})
        def open_count():return next(p for p in self.client.get('/api/projects').json() if p['id']==self.project)['open_issues']
        self.assertEqual(open_count(),1,'未确认的复审不能提前减少待整改数')
        self.assertEqual(self.client.post('/api/tasks/'+third+'/confirm',json=confirm).status_code,200)
        self.assertEqual(open_count(),0)

    def test_standard_verification_edits_preserve_existing_snapshot(self):
        standard=dict(name='测试标准非真实依据',code='TEST-EDIT',version='v1',category='企业',source='测试来源',
            effective='2020-01-01',expires='',conditions='仅测试',content='1.0.1 测试条文原文，不用于真实审核。',state='pending',actor='测试')
        sid=self.client.post('/api/standards',json=standard).json()['id']
        self.assertNotIn(sid,[s['id'] for s in agent.applicable_standards('2020-11-24')])
        self.assertEqual(self.client.put('/api/standards/'+sid,json={**standard,'state':'verified'}).status_code,200)
        fid=self.upload();tid=self.new_task(fid)
        snapshot=self.client.get('/api/tasks/'+tid).json()['snapshot']
        self.assertIn(sid,[s['id'] for s in snapshot['standards']])
        self.assertEqual(self.client.put('/api/standards/'+sid,json={**standard,'state':'verified','version':'v2'}).status_code,200)
        self.assertEqual(self.client.get('/api/tasks/'+tid).json()['snapshot'],snapshot)
        self.assertEqual(next(s for s in self.client.get('/api/standards').json() if s['id']==sid)['version'],'v2')
        self.assertEqual(self.client.put('/api/standards/'+sid,json={**standard,'source':' '}).status_code,422)

    def test_manual_issue_and_confirmation_validation(self):
        fid=self.upload();tid=self.new_task(fid)
        execute("UPDATE tasks SET state='draft' WHERE id=?",(tid,))
        body=dict(title='人工问题',nature='完整性不足',risk='一般',description='请补充针对性说明',actor='测试工程师',
            file_id=fid,page=1,quote='本工程为教学综合楼',rule_id='03')
        self.assertEqual(self.client.post('/api/tasks/'+tid+'/issues',json=body).status_code,200)
        issue=self.client.get('/api/tasks/'+tid).json()['issues'][0]
        for changes in [{'rule_id':'99'},{'nature':'非法分类'},{'description':' '},{'quote':'不存在的原文证据'}]:
            self.assertEqual(self.client.post('/api/tasks/'+tid+'/issues',json={**body,**changes}).status_code,422)
        update={k:issue[k] for k in ['title','nature','risk','description','recommendation','owner','deadline','review_note']}
        update.update(actor='测试工程师',decision='accepted',nature='确认不符合')
        self.assertEqual(self.client.patch('/api/issues/'+issue['id'],json=update).status_code,422)
        self.assertEqual(self.client.post('/api/tasks/'+tid+'/confirm',json={'actor':' ','conclusion':'修改后复核','note':' '}).status_code,422)

    def test_office_conversion_failure_allows_docx_fallback(self):
        import subprocess
        from parsing import office_pdf
        with patch('parsing.shutil.which',return_value='soffice'),patch('parsing.subprocess.run',side_effect=subprocess.CalledProcessError(1,'soffice')):
            self.assertFalse(office_pdf(Path('test.docx'),Path('test.pdf')))


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        init()
        self.client=TestClient(web.app)
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root_patch=patch('agent.ROOT',Path(self.folder.name))
        root_patch.start();self.addCleanup(root_patch.stop)
        env_patch=patch.dict(os.environ,{},clear=True)
        env_patch.start();self.addCleanup(env_patch.stop)
        self.path=Path(self.folder.name)/'.env'
        self.path.write_text('# local settings\nMODEL_TIMEOUT=120\n',encoding='utf-8')
        self.settings=dict(base_url='https://api.agnes-ai.cn/v1/',model='agnes-3.0-flash',api_key='test-secret-private')

    def test_save_reload_and_no_secret_in_responses(self):
        response=self.client.put('/api/model/config',json=self.settings)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['configured'])
        self.assertEqual(agent.config()['key'],self.settings['api_key'])
        self.assertEqual(agent.config()['base_url'],'https://api.agnes-ai.cn/v1')
        self.assertIn('MODEL_TIMEOUT=120',self.path.read_text(encoding='utf-8'))
        for path in ['/api/model/config','/api/health']:
            self.assertNotIn(self.settings['api_key'],self.client.get(path).text)
        self.assertNotIn(self.settings['api_key'],response.text)
        self.settings['api_key']=''
        self.assertEqual(self.client.put('/api/model/config',json=self.settings).status_code,200)
        self.assertEqual(agent.config()['key'],'test-secret-private')
        self.settings['api_key']='new-secret'
        self.client.put('/api/model/config',json=self.settings)
        self.assertEqual(agent.config()['key'],'new-secret')

    def test_invalid_settings_do_not_overwrite(self):
        original=self.path.read_text(encoding='utf-8')
        for field,value in [('base_url','https://user:secret@example.com/v1'),('base_url','https://example.com/v1?key=secret'),('api_key','secret\nINJECTED=1'),('model','  ')]:
            response=self.client.put('/api/model/config',json={**self.settings,field:value})
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(self.path.read_text(encoding='utf-8'),original)

    def test_connection_success_and_sanitized_failures(self):
        import httpx
        self.client.put('/api/model/config',json=self.settings)
        body={**self.settings,'api_key':''}
        response=httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]})
        with patch('agent.httpx.post',return_value=response) as send:
            result=self.client.post('/api/model/test',json=body)
            self.assertEqual(result.status_code,200,result.text)
            self.assertTrue(result.json()['ok'])
            self.assertEqual(send.call_args.kwargs['headers']['Authorization'],'Bearer test-secret-private')
            self.assertEqual(len(send.call_args.kwargs['json']['messages']),1)
        for response in [httpx.Response(401,text='test-secret-private'),httpx.Response(200,json={}),httpx.Response(200,text='invalid JSON')]:
            with patch('agent.httpx.post',return_value=response):
                result=self.client.post('/api/model/test',json=body)
                self.assertEqual(result.status_code,422)
                self.assertNotIn('test-secret-private',result.text)
        with patch('agent.httpx.post',side_effect=httpx.ConnectError('test-secret-private')):
            result=self.client.post('/api/model/test',json=body)
            self.assertEqual(result.status_code,422)
            self.assertNotIn('test-secret-private',result.text)


if __name__=='__main__':unittest.main()
