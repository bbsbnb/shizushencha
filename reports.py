from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from store import DATA

def build_report(task):
    d=Document();s=d.sections[0]
    s.page_width=Inches(8.27);s.page_height=Inches(11.69)
    s.top_margin=s.bottom_margin=Inches(.7)
    s.left_margin=s.right_margin=Inches(.7)
    for name in ['Normal','Title','Heading 1','Heading 2']:
        st=d.styles[name];st.font.name='Microsoft YaHei';st.font.color.rgb=RGBColor(0,0,0)
        st.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
    d.styles['Normal'].font.size=Pt(10.5)
    for elem in list(d.styles.element.iter(qn('w:pBdr'))):elem.getparent().remove(elem)
    draft=task['state']!='confirmed'
    d.add_heading('施工组织设计内部审核报告',0)
    d.add_paragraph('待人工复核初稿' if draft else '人工确认的内部审核版本')
    project=task['snapshot']['project'];result=task['result']
    for label,value in [('工程名称',project['name']),('审核任务',task['id']),
       ('审核模式','历史复核' if project['mode']=='historical' else '当前报审'),
       ('审核基准日期',project['baseline_date']),('发起人',task['actor']),
       ('生成任务时间',task['created']),('技术复核人员',task['reviewer'] or '待填写'),
       ('复核日期',task['confirmed'] or '未复核')]:d.add_paragraph(f'{label}：{value}')
    d.add_heading('资料及审核范围',1)
    for f in task['snapshot']['files']:d.add_paragraph(f"{f['name']}  版本 {f['version']}  文件标识 {f['id']}")
    d.add_paragraph('确认工程事实：'+task['facts'])
    d.add_paragraph('规则版本：'+task['snapshot']['rule_version']+'；模型：'+task['snapshot']['model']['model'])
    d.add_paragraph('规范快照：'+('；'.join(s['code']+' '+s['version'] for s in task['snapshot']['standards']) or '未配置已核验且适用的规范'))
    d.add_heading('总体意见',1)
    d.add_paragraph(result.get('summary') or '审核尚未完成，暂不形成专业审核结论。')
    d.add_paragraph('内部结论：'+(task['conclusion'] or '待人工复核'))
    if task['review_note']:d.add_paragraph('复核说明：'+task['review_note'])
    d.add_heading('简明检查结果',1)
    t=d.add_table(rows=1, cols=3);t.style='Table Grid'
    for c,text in zip(t.rows[0].cells,['检查项','状态','理由']):c.text=text
    for check in result.get('checks',[]):
        row=t.add_row().cells
        row[0].text=check['rule_id'];row[1].text=check['status'];row[2].text=check['reason']
    if not result.get('checks'):d.add_paragraph('尚未完成全部检查项。')
    d.add_heading('问题与整改要求',1)
    issues=[i for i in task['issues'] if i['decision']!='rejected']
    if not issues:d.add_paragraph('未列出问题不代表全部符合；请结合审核状态、覆盖和限制判断。')
    for issue in issues:
        d.add_heading(issue['code']+' '+issue['title'],2)
        d.add_paragraph(f"性质：{issue['nature']}；风险：{issue['risk']}；人工处置："+{'pending':'待复核','accepted':'采纳','rejected':'驳回'}[issue['decision']])
        d.add_paragraph(issue['description'])
        for e in issue['evidence']:d.add_paragraph(f"原文证据：{e['file_id']} {e['label']} — {e['quote']}")
        for c in issue['citations']:d.add_paragraph(f"依据：{c['name']} {c['code']} {c['version']} {c['clause']}；{c['quote']}；来源：{c['source']}")
        if not issue['citations']:d.add_paragraph('规范依据：未取得可核验条文，不作为确定性违规依据。')
        d.add_paragraph('整改建议：'+issue['recommendation'])
        d.add_paragraph(f"责任人：{issue['owner'] or '待填写'}；期限：{issue['deadline'] or '由人工确定'}")
        if issue['review_note']:d.add_paragraph('人工复核说明：'+issue['review_note'])
    if task['rechecks']:
        d.add_heading('整改复审记录',1)
        for r in task['rechecks']:
            d.add_heading(r['code']+' '+r['title'],2)
            d.add_paragraph(f"AI建议：{r['suggestion']}；人工确认：{r['confirmed'] or '待确认'}")
            d.add_paragraph('判断理由：'+r['reason'])
            for e in r['old_evidence']:d.add_paragraph('整改前：'+e['label']+' '+e['quote'])
            for e in r['evidence']:d.add_paragraph('整改后：'+e['label']+' '+e['quote'])
            d.add_paragraph('确认记录：'+(r['actor'] or '待填写')+'；'+r['note'])
    d.add_heading('限制与未完成事项',1)
    for item in result.get('limitations',[]):d.add_paragraph(str(item))
    if task['state'] not in ['draft','confirmed']:d.add_paragraph('任务未完成：'+task['stage']+'；'+task['error'])
    d.add_paragraph('本报告用于施工企业内部辅助审核。姓名为人工填写记录，不代表账号认证、电子签名或法定审批。')
    footer=s.footer.paragraphs[0]
    footer.text='审核任务 '+task['id'][:8]+'  |  '
    fld=OxmlElement('w:fldSimple');fld.set(qn('w:instr'),'PAGE');footer._p.append(fld)
    path=DATA/'reports'/(task['id']+('-confirmed' if not draft else '-draft')+'.docx')
    d.save(path)
    return path
